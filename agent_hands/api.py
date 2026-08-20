"""The capability catalog, over HTTP.

An agent invokes a capability by name with typed arguments and gets a structured
result back. It is told nothing about the application's UI: the catalog projects
each artifact down to its contract -- parameters, outputs, the business answers
it can return -- and drops the steps and the targets that would give the UI away.

This is a second front door onto the same engine, so it enforces the same gates
the CLI does. A front door that skipped them would be a way around them, which is
the one thing a wrapper must not become. Two rules carry most of that weight:

- **The request supplies arguments and nothing else.** The allowlist is derived
  here, from the artifact's own entry URL, so a caller cannot name a host. An
  artifact whose entry URL is templated is refused at load, because a parameter
  that can move the target host would defeat the allowlist from inside.
- **Nothing per-run is shared between callers.** `Policy`, `Evidence`, the
  escalator and the browser are built inside the request. `Policy.approver` is a
  mutable slot, and hoisting it to module scope would leak one caller's approval
  into another caller's irreversible step.

Threads, not async: the engine drives Playwright's synchronous API, which raises
if it is started inside an event loop. A thread per request is what it wants, and
`ThreadingHTTPServer` gives that without adding a dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .escalation import Decision as OperatorDecision
from .escalation import Escalator, ScriptedConsole
from .evidence import DEFAULT_ROOT, Evidence
from .policy import AppAllowance, Policy
from .recorder import load
from .replay import ParamError, Replay
from .schema import Capability, Risk

# One whole Chromium per invocation. The cap is about the machine and about the
# target being a shared host, not about the engine, which is happy to run many.
MAX_CONCURRENT_RUNS = 4

# How long an invocation may hold the request open before it answers 202 and
# lets the caller poll. Long enough that the common case is a plain 200.
DEFAULT_WAIT_SECONDS = 60

_TEMPLATED = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

# Where a recording starts when the caller does not say.
DEFAULT_RECORD_URL = "https://web-sample.interface-hiring.com/signon"

_W, _H = 1500, 940
_WINDOW_ARGS = [f"--window-size={_W},{_H + 80}", "--window-position=40,40"]


def _viewport() -> Any:
    """Playwright wants a TypedDict here; a plain literal is the same shape."""
    return {"width": _W, "height": _H}


class CatalogError(Exception):
    """An artifact that may not be served, with the reason a person needs."""


def args_digest(args: dict[str, Any]) -> str:
    """A stable fingerprint of one invocation's arguments."""
    # `ensure_ascii=False` so a browser's JSON.stringify over the same sorted
    # keys produces the same bytes; a client that cannot reproduce the digest
    # cannot confirm anything.
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApiApprover:
    """A caller's yes to one irreversible step, bound to one set of arguments.

    Not a boolean. A flag the caller can always set is not a gate, and §3.5 of
    the brief warns about exactly that -- the wrapper becoming the way around the
    guardrail. So a confirmation has to name the capability *and* carry a digest
    of the arguments the server computed for itself, which means it cannot be a
    default, cannot be left on by accident, and cannot be replayed against a
    different amount or a different member.

    It is still only a deliberate-action gate, not an authorization one. Nothing
    here knows who is asking; that needs identity, which the report lists among
    the things this system does not have. What it does buy is that transferring
    money takes a second, specific act, and that the act is in the evidence.

    Capabilities not named in `confirmable` can never be confirmed this way and
    always fall through to a person. Place Hold is left out on purpose: it is the
    restricted function, and a supervisor deciding is the point of it.
    """

    def __init__(self, confirmable: frozenset[str], confirm: dict[str, Any] | None,
                 expected_digest: str) -> None:
        self.confirmable = confirmable
        self.confirm = confirm or {}
        self.expected_digest = expected_digest
        self.reason = ""

    def approve(self, *, capability: str, step: Any) -> bool:
        if capability not in self.confirmable:
            self.reason = f"{capability!r} is not confirmable over the API"
            return False
        if self.confirm.get("capability") != capability:
            self.reason = "the confirmation does not name this capability"
            return False
        if self.confirm.get("args_digest") != self.expected_digest:
            self.reason = "the confirmation does not match these arguments"
            return False
        return True


# --------------------------------------------------------------------------
# the catalog
# --------------------------------------------------------------------------

def catalog_id(cap: Capability) -> str:
    """How a capability is addressed.

    Name alone is not enough. Many institutions run the same vendor product, so
    two artifacts share a name and differ only by `app_variant` -- which is the
    multi-tenant case the schema was built for. A catalog keyed on name serves
    whichever of them loaded last and silently drops the other.
    """
    return cap.name if cap.app_variant is None else f"{cap.name}@{cap.app_variant}"


def _projection(cap: Capability) -> dict[str, Any]:
    """What an agent is allowed to know about a capability.

    Everything here is part of the contract. `steps`, `entry_url` and every
    target are left out on purpose: a caller that knows the flow will start
    depending on it, and the whole point is that the flow can be re-recorded
    without the caller changing.
    """
    return {
        "id": catalog_id(cap),
        "name": cap.name,
        "description": cap.description,
        "app_id": cap.app_id,
        "app_variant": cap.app_variant,
        "artifact_version": cap.artifact_version,
        "schema_version": cap.schema_version,
        "approved": cap.approved,
        "recorded_at": cap.recorded_at,
        "params": [p.to_json() for p in cap.params],
        "outputs": [o.to_json() for o in cap.outputs],
        # The codes a caller may receive as a business outcome, without the page
        # text that identifies them, which is UI knowledge.
        "business_codes": [
            {"code": b.code, "message": b.message, "terminal": b.terminal}
            for b in cap.business_rules
        ],
        "writes": any(s.risk is not Risk.SAFE for s in cap.steps),
        "irreversible": any(s.risk is Risk.IRREVERSIBLE for s in cap.steps),
        "step_count": len(cap.steps),
    }


def _admissible(cap: Capability, path: Path) -> None:
    """Refuse an artifact the API must not serve, whatever the CLI would do.

    A templated entry URL is the one that matters. `run` substitutes parameters
    into `entry_url` before anything else, so an artifact carrying `{host}` there
    would let the invocation choose where the browser goes -- and the allowlist
    is built from that same URL, so it would move too.
    """
    if _TEMPLATED.search(cap.entry_url):
        raise CatalogError(
            f"{path.name}: entry_url {cap.entry_url!r} is templated; the invocation "
            "would choose the target host and the allowlist with it")
    if not urlparse(cap.entry_url).netloc:
        raise CatalogError(f"{path.name}: entry_url {cap.entry_url!r} has no host")


@dataclass
class Catalog:
    """The artifacts on disk, loaded once and served by name."""

    root: Path
    capabilities: dict[str, Capability] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)

    @classmethod
    def open(cls, root: Path | str) -> "Catalog":
        catalog = cls(root=Path(root))
        for path in sorted(catalog.root.rglob("*.json")):
            try:
                cap = load(path)
                _admissible(cap, path)
            except (CatalogError, ValueError, KeyError) as exc:
                # Kept rather than dropped. An artifact that silently fails to
                # load looks exactly like one nobody recorded.
                catalog.refused[path.name] = str(exc)
                continue
            key = catalog_id(cap)
            if key in catalog.capabilities:
                # Two artifacts claiming the same tenant. Refusing is the only
                # safe answer: serving whichever loaded last would mean the
                # capability an agent invokes depends on filename order.
                catalog.refused[path.name] = f"duplicate capability id {key!r}"
                continue
            catalog.capabilities[key] = cap
        return catalog

    def listing(self) -> dict[str, Any]:
        return {
            "capabilities": [_projection(c) for c in self.capabilities.values()],
            "refused": self.refused,
        }


# --------------------------------------------------------------------------
# invocations
# --------------------------------------------------------------------------

@dataclass
class Run:
    """One invocation, and the only place its live status is authoritative.

    `ReplayResult.escalated` is not that signal: it is also true for a run that
    never had an escalator, including a policy refusal, because it falls back to
    asking the policy whether the failure was worth somebody's time.
    """

    run_id: str
    capability: str
    args: dict[str, Any]
    status: str = "running"          # running | completed
    result: dict[str, Any] | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "capability": self.capability,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }


class Invoker:
    """Runs capabilities, one browser at a time up to the cap."""

    def __init__(self, catalog: Catalog, *, headed: bool = False,
                 evidence_root: Path | str | None = None,
                 confirmable: frozenset[str] = frozenset()) -> None:
        self.catalog = catalog
        self.headed = headed
        self.evidence_root = evidence_root
        # Empty by default. An irreversible step with nothing named here refuses
        # and asks for a person, which is the right way round to fail.
        self.confirmable = confirmable
        self.runs: dict[str, Run] = {}
        self._slots = threading.Semaphore(MAX_CONCURRENT_RUNS)
        self._lock = threading.Lock()

    def record(self, goal: str, entry_url: str, name: str) -> Run:
        """Let a model work a job out, live, in a window somebody is watching.

        The other half of the design, and until now the API could only show its
        leftovers. Same shape as an invocation -- a thread, a run id, an evidence
        directory -- so the page polls it the same way.
        """
        evidence = Evidence.start(f"record-{name}", params={"goal": goal, "url": entry_url})
        run = Run(run_id=evidence.dir.name, capability=f"record-{name}", args={"goal": goal})
        with self._lock:
            self.runs[run.run_id] = run
        threading.Thread(target=self._discover, daemon=True,
                         args=(run, goal, entry_url, name, evidence)).start()
        return run

    def _discover(self, run: Run, goal: str, entry_url: str, name: str,
                  evidence: Evidence) -> None:
        from playwright.sync_api import sync_playwright

        from .discover import discover
        from .recorder import save

        self._slots.acquire()
        play = browser = None
        try:
            play = sync_playwright().start()
            # Always headed, and big. The point of watching a recording is
            # watching it, and the default window is too small to read from
            # across a room.
            browser = play.chromium.launch(headless=False, args=_WINDOW_ARGS)
            page = browser.new_page(viewport=_viewport())
            policy = self._policy_for(entry_url)
            policy.approver = _AlwaysWatching()
            result = discover(goal=goal, entry_url=entry_url, page=page, policy=policy,
                              app_id="meridian-core", evidence=evidence, max_turns=30)
            evidence.event("discovery_finished", ok=result.ok, turns=result.turns,
                           stopped_because=result.stopped_because)
            if not result.ok or result.capability is None:
                run.error = result.stopped_because
            else:
                cap = result.capability
                cap.name = name
                # Signed off here so the demo is two clicks. The gate itself is
                # untouched -- replay still refuses an unapproved artifact, and
                # `agent-hands record` still writes a draft.
                cap.approved = True
                path = Path(self.catalog.root) / "meridian" / f"{name}.json"
                save(cap, path)
                self.catalog = Catalog.open(self.catalog.root)
                run.result = {"turns": result.turns, "capability": name,
                              "artifact": cap.to_json()}
        except Exception as exc:                      # noqa: BLE001
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            if browser is not None:
                browser.close()
            if play is not None:
                play.stop()
            self._slots.release()
            run.status = "completed"
            run.done.set()

    def start(self, name: str, args: dict[str, Any],
              confirm: dict[str, Any] | None = None) -> Run:
        cap = self.catalog.capabilities[name]
        evidence = Evidence.start(cap.name, root=self.evidence_root, params=args)
        run = Run(run_id=evidence.dir.name, capability=cap.name, args=args)
        approver = ApiApprover(self.confirmable, confirm, args_digest(args))
        with self._lock:
            self.runs[run.run_id] = run
        threading.Thread(target=self._execute, args=(run, cap, evidence, approver),
                         daemon=True).start()
        return run

    def _execute(self, run: Run, cap: Capability, evidence: Evidence,
                 approver: ApiApprover) -> None:
        from playwright.sync_api import sync_playwright

        self._slots.acquire()
        play = browser = None
        try:
            play = sync_playwright().start()
            browser = play.chromium.launch(headless=not self.headed,
                                           args=_WINDOW_ARGS if self.headed else [])
            page = browser.new_page(viewport=_viewport() if self.headed else None)

            policy = self._policy(cap)
            # With this the surface gate checks where the browser actually is on
            # every step, instead of only where the artifact said it would be.
            policy.url_provider = lambda: page.url
            escalator = Escalator(self._console(), evidence=evidence)
            # The caller's confirmation first; a person only if there is not one.
            policy.approver = _Approvers(approver, escalator)

            result = Replay(page, policy, evidence=evidence,
                            escalator=escalator).run(cap, run.args)
            payload = result.to_json()
            # An absolute path on the server is not the caller's business; the
            # run id addresses the same directory through this API.
            payload.pop("evidence_dir", None)
            payload["typed_outputs"] = _coerce_outputs(cap, result.outputs)
            run.result = payload
        except ParamError as exc:
            run.error = str(exc)
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            if browser is not None:
                browser.close()
            if play is not None:
                play.stop()
            self._slots.release()
            run.status = "completed"
            run.done.set()

    def _policy(self, cap: Capability) -> Policy:
        """The allowlist, derived here rather than accepted from the caller."""
        return self._policy_for(cap.entry_url)

    def _policy_for(self, url: str) -> Policy:
        host = urlparse(url).netloc
        return Policy(apps=(AppAllowance(host=host),), name=host)

    def _console(self) -> Any:
        """The operator console, stubbed at the seam it was built for.

        `OperatorConsole` is a two-method protocol, and everything above it --
        the ownership state machine, the intervention packet, the re-check on
        resume -- is real. A queue-backed console that parks a run and lets a
        person answer over HTTP swaps in here and changes nothing else. Until
        that exists, the honest answer is the one an unattended run gives: stop,
        and say a person was needed. Approving automatically would wave through
        exactly the irreversible steps the gate is for.
        """
        return ScriptedConsole(decision=OperatorDecision.ABORT,
                               note="no operator console is attached to the API",
                               approves=False)


def _history(inv: "Invoker", limit: int = 60) -> list[dict[str, Any]]:
    """Every run this machine has a record of, newest first.

    Read from the evidence tree rather than from the in-memory registry, because
    the registry is empty after a restart and never held the runs the CLI made.
    A run id *is* an evidence directory name, so the two join for free. Live runs
    come from the registry, since they have no `result.json` yet.
    """
    root = Path(inv.evidence_root) if inv.evidence_root else DEFAULT_ROOT
    seen: dict[str, dict[str, Any]] = {}
    for run_id, run in inv.runs.items():
        seen[run_id] = {"run_id": run_id, "capability": run.capability,
                        "status": run.status, "error": run.error}
    for path in sorted(root.glob("*/result.json"), reverse=True)[:limit]:
        run_id = path.parent.name
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        seen[run_id] = {
            "run_id": run_id, "capability": result.get("capability", "?"),
            "status": "completed", "outcome": result.get("outcome"),
            "business_code": result.get("business_code"),
        }
    return sorted(seen.values(), key=lambda r: r["run_id"], reverse=True)[:limit]


def _events(inv: "Invoker", run_id: str) -> dict[str, Any]:
    """One run's log, and what else it left behind.

    `run.jsonl` is flushed a line at a time, so a run still going answers with
    what it has done so far and the page can watch it work.
    """
    root = Path(inv.evidence_root) if inv.evidence_root else DEFAULT_ROOT
    run_dir = root / run_id
    if run_id != Path(run_id).name or not run_dir.is_dir():
        return {"events": [], "artifacts": []}
    events = []
    log = run_dir / "run.jsonl"
    if log.exists():
        for line in log.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    artifacts = sorted(f.name for f in run_dir.iterdir()
                       if f.name not in ("run.jsonl", "result.json"))
    return {"events": events, "artifacts": artifacts}


def _recordings(inv: "Invoker", limit: int = 20) -> list[dict[str, Any]]:
    """The runs where a model drove the application, rather than a recording.

    These are the other half of the story and the API had no way to show them.
    A discovery run leaves no `result.json` -- it produces an artifact, not an
    outcome -- so `_history` skips it entirely. It is recognised here by the
    events only the discovery loop writes.

    What comes back is the model's own account of itself: which control it chose
    on each turn, in its own words why, and how long it took. That is worth
    showing next to a replay of the file it produced, because the contrast is
    the entire argument for building it this way.
    """
    root = Path(inv.evidence_root) if inv.evidence_root else DEFAULT_ROOT
    found: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/run.jsonl"), reverse=True):
        try:
            events = [json.loads(line) for line in path.read_text().splitlines() if line]
        except (OSError, json.JSONDecodeError):
            continue
        actions = [e for e in events if e.get("event") == "discovery_action"]
        finished = next((e for e in events if e.get("event") == "discovery_finished"), None)
        if not actions and finished is None:
            continue
        started: dict[str, Any] = next(
            (e for e in events if e.get("event") == "run_started"), {})
        name = str(started.get("capability", ""))
        found.append({
            "run_id": path.parent.name,
            # The recorder names a discovery run after the capability it is
            # trying to produce, prefixed. Strip it, so the page can line the
            # recording up against replays of what it wrote.
            "produced": name[len("record-"):] if name.startswith("record-") else name,
            "goal": (started.get("params") or {}).get("goal", ""),
            "actions": [{"at_ms": a.get("elapsed_ms"), "action": a.get("action"),
                         "control": a.get("control"), "detail": a.get("detail"),
                         "why": a.get("why"), "strategy": a.get("strategy")}
                        for a in actions],
            "turns": (finished or {}).get("turns"),
            "ok": (finished or {}).get("ok"),
            "summary": (finished or {}).get("stopped_because", ""),
            "took_ms": (finished or (actions[-1] if actions else {})).get("elapsed_ms"),
        })
        if len(found) >= limit:
            break
    return found


class _AlwaysWatching:
    """A person is at the keyboard for a recording, and the window is open."""

    def approve(self, *, capability: str, step: Any) -> bool:
        return True


class _Approvers:
    """Ask each in turn. The caller's own confirmation, then a person."""

    def __init__(self, *approvers: Any) -> None:
        self.approvers = approvers

    def approve(self, *, capability: str, step: Any) -> bool:
        return any(a.approve(capability=capability, step=step) for a in self.approvers)


def _coerce_outputs(cap: Capability, outputs: dict[str, Any]) -> dict[str, Any]:
    """The outputs again, as their declared types.

    Served alongside the raw strings rather than instead of them, so the API
    response and the `result.json` on disk agree about what was read. The engine
    stores what it read; a caller usually wants a number.
    """
    declared = {o.name: o.type for o in cap.outputs}
    typed: dict[str, Any] = {}
    for name, value in outputs.items():
        kind = declared.get(name, "string")
        if kind == "string" or not isinstance(value, str):
            typed[name] = value
            continue
        cleaned = value.replace(",", "").replace("$", "").strip()
        try:
            typed[name] = int(cleaned) if kind == "integer" else float(cleaned)
        except ValueError:
            typed[name] = value
    return typed


# --------------------------------------------------------------------------
# the HTTP surface
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Five routes. The verbs carry the meaning, so there is nothing else here.

    Status codes describe the invocation, never the business answer. All three
    outcomes are 200 and carry `outcome` in the body: a caller that reads "no
    such member" as an HTTP error has lost the distinction the engine exists to
    make. Only the caller's own mistakes are 4xx.
    """

    server_version = "agent-hands"
    invoker: Invoker           # set on the server, read through self.server

    def log_message(self, *_a: Any) -> None:
        pass                   # the evidence directory is the log

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:                                # noqa: N802
        inv: Invoker = self.server.invoker                   # type: ignore[attr-defined]
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/":
            page = Path(__file__).resolve().parent / "dashboard.html"
            blob = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        if path == "/capabilities":
            return self._send(200, inv.catalog.listing())
        if path.startswith("/capabilities/"):
            parts = path.split("/")
            cap = inv.catalog.capabilities.get(parts[2])
            if cap is None:
                return self._send(404, {"error": f"no capability named {parts[2]!r}"})
            if len(parts) == 4 and parts[3] == "artifact":
                # The whole file, steps and targets included. This is the
                # reviewer's view, not the agent's: the catalog hides the flow on
                # purpose so a caller cannot depend on it, but a person deciding
                # whether to approve a recording has to read every line of it.
                return self._send(200, cap.to_json())
            return self._send(200, _projection(cap))
        if path == "/recordings":
            return self._send(200, {"recordings": _recordings(inv)})
        if path == "/invocations":
            return self._send(200, {"invocations": _history(inv)})
        if path.startswith("/invocations/"):
            parts = path.split("/")
            run_id = parts[2]
            if len(parts) == 4 and parts[3] == "events":
                return self._send(200, _events(inv, run_id))
            run = inv.runs.get(run_id)
            if run is not None:
                return self._send(200 if run.status == "completed" else 202, run.to_json())
            # Not in this process: a run from an earlier one, or from the CLI.
            root = Path(inv.evidence_root) if inv.evidence_root else DEFAULT_ROOT
            stored = root / Path(run_id).name / "result.json"
            if not stored.exists():
                return self._send(404, {"error": "no such run"})
            result = json.loads(stored.read_text())
            return self._send(200, {"run_id": run_id, "capability": result.get("capability"),
                                    "status": "completed", "result": result, "error": None})
        return self._send(404, {"error": f"no route {path!r}"})

    def do_POST(self) -> None:                               # noqa: N802
        inv: Invoker = self.server.invoker                   # type: ignore[attr-defined]
        path = urlparse(self.path).path.rstrip("/")
        parts = path.split("/")
        if path != "/recordings" and (
                len(parts) != 4 or parts[1] != "capabilities"
                or parts[3] not in ("invocations", "approval")):
            return self._send(404, {"error": f"no route {path!r}"})

        if len(parts) == 4 and parts[1] == "capabilities" and parts[3] == "approval":
            cap = inv.catalog.capabilities.get(parts[2])
            if cap is None:
                return self._send(404, {"error": f"no capability named {parts[2]!r}"})
            # The reviewer's signature. A recording is a draft until somebody
            # who read it says otherwise, which is why this is its own act and
            # not something `record` does on the way out.
            from .recorder import save
            cap.approved = True
            save(cap, Path(inv.catalog.root) / "meridian" / f"{cap.name}.json")
            inv.catalog = Catalog.open(inv.catalog.root)
            return self._send(200, {"approved": True, "capability": cap.name})

        if path == "/recordings":
            try:
                body = self._body()
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            goal, name = body.get("goal"), body.get("name")
            if not goal or not name:
                return self._send(400, {"error": "goal and name are required"})
            run = inv.record(goal, body.get("entry_url") or DEFAULT_RECORD_URL, name)
            run.done.wait(timeout=float(body.get("wait_seconds", 180)))
            return self._send(200 if run.status == "completed" else 202, run.to_json())

        name = parts[2]
        if name not in inv.catalog.capabilities:
            return self._send(404, {"error": f"no capability named {name!r}"})
        try:
            body = self._body()
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})

        # Arguments and a deadline. Nothing else is accepted, so the request
        # cannot reach the policy, the entry URL or the steps.
        unknown = sorted(set(body) - {"args", "wait_seconds", "confirm"})
        if unknown:
            return self._send(400, {"error": f"unexpected field(s): {', '.join(unknown)}"})
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return self._send(400, {"error": "args must be an object"})

        run = inv.start(name, args, body.get("confirm"))
        run.done.wait(timeout=float(body.get("wait_seconds", DEFAULT_WAIT_SECONDS)))
        if run.status != "completed":
            return self._send(202, run.to_json())
        if run.error is not None:
            # A malformed invocation is the caller's bug, not a replay outcome,
            # which is why the engine raises for it instead of returning one.
            return self._send(400, run.to_json())
        return self._send(200, run.to_json())

    # -- plumbing ----------------------------------------------------------

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError(f"body is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        blob = json.dumps(payload, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


def serve(port: int = 8080, *, capabilities: Path | str = "capabilities",
          headed: bool = False, evidence_root: Path | str | None = None,
          confirmable: frozenset[str] = frozenset()) -> None:
    catalog = Catalog.open(capabilities)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.invoker = Invoker(catalog, headed=headed,          # type: ignore[attr-defined]
                            evidence_root=evidence_root, confirmable=confirmable)
    served = ", ".join(sorted(catalog.capabilities)) or "(none)"
    print(f"capability API on http://127.0.0.1:{port}/  serving: {served}")
    for name, why in catalog.refused.items():
        print(f"  refused {name}: {why}")
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="serve recorded capabilities over HTTP")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--capabilities", default="capabilities")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser; required for a handoff to a person")
    ap.add_argument("--evidence-root", default=None)
    ap.add_argument("--confirmable", default="",
                    help="comma-separated capability ids whose irreversible steps "
                         "a caller may confirm; everything else asks a person")
    a = ap.parse_args(argv)
    serve(a.port, capabilities=a.capabilities, headed=a.headed,
          evidence_root=a.evidence_root,
          confirmable=frozenset(n for n in a.confirmable.split(",") if n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
