"""Per-run evidence: what was attempted, what resolved it, what it saw.

A reviewer who was not there must be able to answer "why did this stop?" from
the run directory alone, without re-running anything against production. A bank
will not run automation that cannot explain a failure.

Three files, split on purpose:

- `run.jsonl` is append-only and flushed per line, so a process killed mid-step
  still leaves the steps before it. A single JSON document would be truncated
  in exactly the case you most need it.
- `result.json` is the ReplayResult the caller received. It is written last, so
  it is the only file that means "this run is complete".
- the failure files are written once, at the point of failure, and pair a
  screenshot with the accessibility snapshot. The screenshot is what a person
  sees, the snapshot is what the targeting layer saw. Most confusing failures
  are a disagreement between the two, which neither file shows alone.

Text leaving this module goes through `policy.redact`. Screenshots cannot, so
the run directory is sensitive by construction and treated that way.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .schema import ReplayResult, Step, Target, TargetSet

# The evidence root sits beside the package, not inside it. This is run output,
# and someone will eventually want to delete all of it in one go.
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "evidence"

# Capture runs after something has already gone wrong, with a person or a
# caller's deadline waiting on the answer. At the default 30s, one failed step
# becomes a timed-out run, so the snapshot gets its own short budget and is
# allowed to come back empty.
SNAPSHOT_TIMEOUT_MS = 2000


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

def _load_redactor() -> Callable[[Any], Any]:
    """Bind to policy.redact, or fall back to a cruder local rule.

    Evidence is worth writing even in a checkout where the policy layer will not
    import, so a missing policy over-redacts instead of crashing mid-run.
    """
    try:
        from .policy import redact  # type: ignore[attr-defined]
    except Exception:
        return _fallback_redact
    return redact


def _fallback_redact(value: Any) -> Any:
    import re

    # Same replacement tokens as the policy layer, so evidence reads the same
    # whichever redactor was in force.
    patterns = (
        (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
        (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED:PAN]"),
        (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[REDACTED:EMAIL]"),
    )
    if isinstance(value, str):
        for pattern, replacement in patterns:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {k: _fallback_redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fallback_redact(v) for v in value]
    return value


def _apply(redactor: Callable[[Any], Any], payload: Any) -> Any:
    """Redact a whole structure, whatever shape of redactor we were given.

    The policy layer belongs to someone else and may take a structure or only a
    string. So hand it the structure, and if that fails, walk the structure here
    and hand it strings. A string-only redactor rejects a dict with TypeError,
    or with AttributeError when it reaches for a str method, hence both.

    Nothing broader is caught. A redactor that fails on the strings themselves
    still raises out of `_walk`, because evidence must never be written
    unredacted and unnoticed.
    """
    try:
        return redactor(payload)
    except (TypeError, AttributeError):
        return _walk(redactor, payload)


def _walk(redactor: Callable[[Any], Any], payload: Any) -> Any:
    if isinstance(payload, str):
        return redactor(payload)
    if isinstance(payload, dict):
        return {k: _walk(redactor, v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_walk(redactor, v) for v in payload]
    return payload


# --------------------------------------------------------------------------
# one step's record
# --------------------------------------------------------------------------

@dataclass
class StepRecord:
    """The mutable half of a step line, filled in while the step runs.

    Replay learns things at different moments: the winning strategy once the
    element is found, the checkpoint only after the action. The caller mutates
    this and the recorder writes whatever is set, so a step that raises still
    leaves behind what was known before it did.
    """

    index: int
    action: str
    resolved: Target | None = None
    attempted: list[Target] = field(default_factory=list)
    checkpoint_ok: bool | None = None
    checkpoint: dict[str, Any] | None = None
    observed: str | None = None
    text: str | None = None
    url: str | None = None
    risk: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def resolved_by(self, target: Target, *, after: list[Target] | None = None) -> None:
        """Record which strategy found the element, and which ones did not.

        The failures are the early warning. A flow that has quietly slipped from
        role+name down to a DOM path still passes, but it is one release from
        not passing, and only the evidence says so.
        """
        self.resolved = target
        self.attempted = list(after or [])

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "text": self.text,
            "url": self.url,
            "risk": self.risk,
            "resolved_by": self.resolved.to_json() if self.resolved else None,
            "failed_strategies": [t.to_json() for t in self.attempted],
            "checkpoint": self.checkpoint,
            "checkpoint_ok": self.checkpoint_ok,
            "observed": self.observed,
            "error": self.error,
            **self.extra,
        }


# --------------------------------------------------------------------------
# the recorder
# --------------------------------------------------------------------------

class Evidence:
    """One directory per run. Cheap to create, safe to leave open."""

    def __init__(self, run_dir: Path, redactor: Callable[[Any], Any] | None = None) -> None:
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._redact = redactor or _load_redactor()
        self._log = self.dir / "run.jsonl"
        self._t0 = time.monotonic()
        self._artefacts: list[str] = []

    @classmethod
    def start(
        cls,
        capability: str,
        *,
        root: Path | str | None = None,
        params: dict[str, Any] | None = None,
        redactor: Callable[[Any], Any] | None = None,
    ) -> "Evidence":
        """Open a run directory named so that `ls` sorts it chronologically."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = _slug(capability)
        base = Path(root) if root else DEFAULT_ROOT
        # The random suffix is for two replays of the same capability in the
        # same second, which is normal once anything schedules this.
        run_dir = base / f"{stamp}_{slug}_{uuid.uuid4().hex[:6]}"
        ev = cls(run_dir, redactor)
        ev.event("run_started", capability=capability, params=params or {})
        return ev

    # -- writing ----------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        """Append one line. Anything not a step goes through here."""
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": int((time.monotonic() - self._t0) * 1000),
            "event": kind,
            **fields,
        }
        payload = _apply(self._redact, line)
        with self._log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            fh.flush()

    @contextmanager
    def step(self, step: Step) -> Iterator[StepRecord]:
        """Time one step and write its line whatever happens to it.

        Exceptions are recorded and re-raised unchanged. The recorder observes
        the run and must not alter its control flow.
        """
        record = StepRecord(
            index=step.index,
            action=step.action.value,
            text=step.text,
            url=step.url,
            risk=step.risk.value,
            checkpoint=step.checkpoint.to_json() if step.checkpoint else None,
        )
        started = time.monotonic()
        try:
            yield record
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.event(
                "step",
                duration_ms=int((time.monotonic() - started) * 1000),
                **record.to_json(),
            )

    def recovery(self, step_index: int, tactic: str, detail: str = "") -> None:
        """Record a handled recoverable condition, so no recovery goes unlogged."""
        self.event("recovery", index=step_index, tactic=tactic, detail=detail)

    def escalation(self, reason: str, question: str, answer: str | None = None) -> None:
        self.event("escalation", reason=reason, question=question, answer=answer)

    # -- the failure artefact ---------------------------------------------

    def capture_failure(
        self,
        page: Any,
        *,
        step_index: int,
        reason: str = "",
        targets: TargetSet | None = None,
    ) -> dict[str, str]:
        """Screenshot plus accessibility snapshot at the point of failure.

        Both are best effort and neither may raise. The page has already
        misbehaved, and losing the rest of the evidence because the screenshot
        failed is the wrong trade.
        """
        stem = f"step-{step_index:02d}-failure"
        written: dict[str, str] = {}

        shot = self.dir / f"{stem}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
            written["screenshot"] = shot.name
        except Exception as exc:
            written["screenshot_error"] = f"{type(exc).__name__}: {exc}"

        snapshot = {
            "reason": reason,
            "step": step_index,
            "targets": targets.to_json() if targets else None,
            "frames": _snapshot_frames(page),
        }
        path = self.dir / f"{stem}-a11y.json"
        path.write_text(
            json.dumps(_apply(self._redact, snapshot), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        written["accessibility"] = path.name

        self._artefacts.extend(written.values())
        self.event("failure_artifact", index=step_index, reason=reason, files=written)
        return written

    # -- closing ----------------------------------------------------------

    def finish(self, result: ReplayResult) -> ReplayResult:
        """Write result.json and stamp the run directory onto the result.

        The path comes back on the object the caller already holds, so a failure
        handed to an agent carries its own pointer to the evidence.
        """
        result.evidence_dir = str(self.dir)
        payload = _apply(self._redact, result.to_json())
        (self.dir / "result.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        self.event("run_finished", outcome=result.outcome.value, artifacts=self._artefacts)
        return result


# --------------------------------------------------------------------------
# playwright interop
# --------------------------------------------------------------------------

def _snapshot_frames(page: Any) -> list[dict[str, Any]]:
    """One accessibility tree per frame, because the target app is a frameset.

    Snapshot only the top document of a frameset and you get two empty frames,
    which tells the reviewer nothing. Playwright is duck-typed rather than
    imported, so the recorder can be tested without a browser.
    """
    frames = list(getattr(page, "frames", None) or [page])
    out: list[dict[str, Any]] = []
    for frame in frames:
        entry: dict[str, Any] = {
            "url": _attr(frame, "url"),
            "name": _attr(frame, "name"),
        }
        tree, source = _tree(frame, page)
        entry["source"] = source
        entry["tree"] = tree
        out.append(entry)
    return out


def _tree(frame: Any, page: Any) -> tuple[Any, str]:
    """ARIA snapshot first, the older accessibility API only as a fallback.

    `html` is tried after `body` because a frameset document has no body: the
    app's top document waits out the full timeout on `body` and then reports
    nothing, and that is the first frame a reviewer opens.
    """
    errors: dict[str, str] = {}
    for selector in ("body", "html"):
        try:
            return _aria(frame, selector), f"aria_snapshot:{selector}"
        except Exception as exc:
            errors[selector] = f"{type(exc).__name__}: {exc}".splitlines()[0]
    try:
        return page.accessibility.snapshot(), "accessibility.snapshot"
    except Exception as exc:
        errors["accessibility"] = f"{type(exc).__name__}: {exc}".splitlines()[0]
    return {"errors": errors}, "unavailable"


def _aria(frame: Any, selector: str) -> Any:
    locator = frame.locator(selector)
    try:
        return locator.aria_snapshot(timeout=SNAPSHOT_TIMEOUT_MS)
    except TypeError:
        # A stand-in locator in tests takes no timeout; the real one does.
        return locator.aria_snapshot()


def _attr(obj: Any, name: str) -> Any:
    value = getattr(obj, name, None)
    return value() if callable(value) else value


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text.strip()]
    return "".join(keep).strip("-").replace("--", "-")[:48] or "run"
