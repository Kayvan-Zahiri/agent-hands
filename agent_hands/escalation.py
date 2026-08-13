"""Handing a live session to a person, and taking it back safely.

Escalation is usually treated as the error path. It is not. It is the normal path
for the long tail, and how well it works decides whether anyone trusts the system
with the other ninety per cent.

What is real here, and what is mocked, because it is the first thing a reviewer
will want to know:

**Real.** The ownership state machine and its refusals. The intervention packet
and the evidence written with it, all of it through `policy.redact`. The handoff
of the *same* Page object rather than a fresh browser. The capture of what the
operator did. The checkpoint re-verification on resume.

**Mocked.** The console, and only the console. `ConsoleOwner` prints to a
terminal and blocks on stdin. In a real deployment that is a queue, a review UI,
an authenticated operator and an attributable audit trail, and the operator
drives the browser through a shared session rather than by reading a screenshot.
`OperatorConsole` is the seam where that gets swapped in; nothing above it
changes when it does, which is why the interface is this small.

Three properties the design is arranged around:

**The session is not thrown away.** The expensive part of a back-office task is
the state: logged in, the right member open, three screens deep. An escalation
that ends the session makes the person redo all of it, so they stop escalating
and start doing the whole task by hand. Here the browser stays exactly where it
is and the person is handed the same window.

**The handoff carries what the automation knew.** "Something went wrong" costs
the reviewer the entire investigation. The packet carries the capability, the
step, what was expected, what was on screen, the URL and a screenshot, so the
first thing they see is a diagnosis rather than a blank terminal.

**Exactly one writer at a time.** If replay keeps running while a person is
fixing the page, two writers race on one session and the evidence stops
describing what happened. So ownership is explicit, transitions are legal or they
raise, and acting out of turn raises rather than logs.
"""

from __future__ import annotations

import enum
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .policy import redact, redact_json
from .schema import Capability, Checkpoint, Step


# --------------------------------------------------------------------------
# the page seam
# --------------------------------------------------------------------------

class PageLike(Protocol):
    """The slice of playwright's sync `Page` this module needs.

    Narrow on purpose: it keeps the handoff testable without a browser, and it
    documents exactly how much power giving a page away actually requires.
    """

    @property
    def url(self) -> str: ...
    def content(self) -> str: ...
    def screenshot(self, *, path: str) -> Any: ...


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------

class OwnershipError(RuntimeError):
    """Raised when something tries to drive the page it does not own."""


class TransitionError(RuntimeError):
    """Raised on an illegal handoff, e.g. OPERATOR straight back to AUTOMATION."""


class Owner(str, enum.Enum):
    AUTOMATION = "automation"
    OPERATOR = "operator"
    # A distinct state, not a flavour of AUTOMATION: between the handback and a
    # verified checkpoint, nobody may act, because neither party yet knows where
    # the page is. RESUMING is the window in which that gets established.
    RESUMING = "resuming"


_LEGAL: dict[Owner, frozenset[Owner]] = {
    Owner.AUTOMATION: frozenset({Owner.OPERATOR}),
    Owner.OPERATOR: frozenset({Owner.RESUMING}),
    Owner.RESUMING: frozenset({Owner.AUTOMATION, Owner.OPERATOR}),
}


@dataclass
class SessionOwner:
    """Who may write to the session right now, plus the trail of handoffs."""

    state: Owner = Owner.AUTOMATION
    history: list[tuple[float, Owner, str]] = field(default_factory=list)

    def _transition(self, to: Owner, why: str) -> None:
        if to not in _LEGAL[self.state]:
            raise TransitionError(f"illegal transition {self.state.value} -> {to.value}")
        self.state = to
        self.history.append((time.time(), to, why))

    def to_operator(self, why: str = "intervention raised") -> None:
        self._transition(Owner.OPERATOR, why)

    def to_resuming(self, why: str = "operator handed back") -> None:
        self._transition(Owner.RESUMING, why)

    def to_automation(self, why: str = "checkpoint re-verified") -> None:
        self._transition(Owner.AUTOMATION, why)

    def guard(self, actor: Owner) -> None:
        """Call before every write. The one line that makes ownership real."""
        if actor is not self.state:
            raise OwnershipError(
                f"{actor.value} tried to act while the session is owned by {self.state.value}")

    def trail(self) -> list[str]:
        return [f"{state.value}: {why}" for _, state, why in self.history]


class OwnedPage:
    """A page that refuses every call unless its holder is the current owner.

    Convention would do here, and convention is what fails at 3am. The wrapper is
    handed to whichever party has control, so a stale reference held by the
    automation raises instead of quietly typing into a screen a person is halfway
    through fixing.
    """

    def __init__(self, page: PageLike, actor: Owner, owner: SessionOwner) -> None:
        self.__page = page
        self.__actor = actor
        self.__owner = owner

    def __getattr__(self, name: str) -> Any:
        self.__owner.guard(self.__actor)
        return getattr(self.__page, name)

    @property
    def url(self) -> str:
        self.__owner.guard(self.__actor)
        return self.__page.url

    def unwrap(self) -> PageLike:
        """The raw page, for the one caller that legitimately needs it: evidence."""
        return self.__page


# --------------------------------------------------------------------------
# the packet
# --------------------------------------------------------------------------

class Reason(str, enum.Enum):
    """Why control is being handed over. Drives routing, not just wording."""

    UNRESOLVED = "unresolved_element"      # no strategy matched
    CHECKPOINT = "checkpoint_failed"       # the page is not where it should be
    APPROVAL = "approval_required"         # policy wants a human yes
    UNEXPECTED = "unexpected_state"        # a screen the recording never saw
    AUTH = "authentication_required"       # credentials, which we never handle
    LIMIT = "attempt_limit"                # recovery gave up


class Decision(str, enum.Enum):
    """What the person decided. `RESUME` is the only one that re-verifies."""

    RESUME = "resume"        # they fixed it; continue, after re-checking position
    SKIP = "skip"            # this step is not needed on this instance
    ABORT = "abort"          # stop; the run is recorded as a failure
    COMPLETED = "completed"  # they finished the task by hand


@dataclass
class Response:
    decision: Decision
    note: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class InterventionRequest:
    """The packet a person receives. Written to be read under time pressure.

    A request that says only "step 4 failed" costs a round trip to the scarcest
    resource in the system, so this carries the capability, the step and its
    checkpoint, what was expected against what was seen, where the page actually
    is, and a picture of it.
    """

    id: str
    reason: Reason
    capability: str
    step_index: int | None
    question: str
    step_action: str = ""
    expected: str | None = None
    observed: str | None = None
    url: str = ""
    screenshot: str | None = None
    checkpoint: dict[str, str] | None = None
    evidence_dir: str | None = None
    options: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    raised_at: float = field(default_factory=time.time)

    def render(self) -> str:
        lines = [
            "",
            "=" * 68,
            f"  HANDOFF {self.id}  ({self.reason.value})",
            "=" * 68,
            f"  capability : {self.capability}",
        ]
        if self.step_index is not None:
            action = f" ({self.step_action})" if self.step_action else ""
            lines.append(f"  step       : {self.step_index}{action}")
        if self.url:
            lines.append(f"  url        : {self.url}")
        if self.expected:
            lines.append(f"  expected   : {self.expected}")
        if self.observed:
            lines.append(f"  observed   : {self.observed}")
        if self.screenshot:
            lines.append(f"  screenshot : {self.screenshot}")
        if self.evidence_dir:
            lines.append(f"  evidence   : {self.evidence_dir}")
        for key, value in self.context.items():
            lines.append(f"  {key:<10} : {value}")
        lines += ["", f"  {self.question}"]
        if self.options:
            lines.append("")
            lines += [f"    - {opt}" for opt in self.options]
        lines += ["=" * 68, ""]
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "reason": self.reason.value, "capability": self.capability,
            "step_index": self.step_index, "step_action": self.step_action,
            "question": self.question, "expected": self.expected, "observed": self.observed,
            "url": self.url, "screenshot": self.screenshot, "checkpoint": self.checkpoint,
            "evidence_dir": self.evidence_dir, "options": self.options,
            "context": self.context, "raised_at": self.raised_at,
        }


# The packet is the same object whether it is raised by replay or by a takeover;
# both names read correctly at their own call sites.
Handoff = InterventionRequest


def raise_intervention(
    reason: Reason | str,
    capability: Capability,
    step: Step,
    page: PageLike,
    evidence: str | Path | None = None,
    *,
    question: str = "",
    observed: str | None = None,
    options: list[str] | None = None,
) -> InterventionRequest:
    """Stop, capture the scene, and write the request down.

    The URL and every free-text field go through `redact` first. An intervention
    is precisely the moment a member's record is on screen, and the packet will be
    read by more people than the run itself, so it is the last place to relax
    about what gets persisted.
    """
    directory = Path(evidence) if evidence else None
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
    request_id = f"int-{uuid.uuid4().hex[:8]}"
    detail = reason.value if isinstance(reason, Reason) else str(reason)

    request = InterventionRequest(
        id=request_id,
        reason=reason if isinstance(reason, Reason) else Reason.UNEXPECTED,
        capability=capability.name,
        step_index=step.index,
        step_action=step.action.value,
        question=redact(question or f"step {step.index} stopped: {detail}"),
        expected=_expected(step),
        observed=redact(observed) if observed else None,
        url=redact(_safe_url(page)),
        screenshot=_try_screenshot(page, directory / f"{request_id}-before.png") if directory else None,
        checkpoint=step.checkpoint.to_json() if step.checkpoint else None,
        evidence_dir=str(directory) if directory else None,
        options=options or ["resume once the page is fixed", "skip this step",
                            "abort the run", "you finished it by hand"],
        context={"app": capability.app_id, "variant": capability.app_variant or "default"},
    )
    if directory:
        _write_json(directory / f"{request_id}.json", request.to_json())
    return request


def _expected(step: Step) -> str | None:
    if step.checkpoint is None:
        return None
    return f"{step.checkpoint.kind}={step.checkpoint.value!r}"


# --------------------------------------------------------------------------
# the console, mocked
# --------------------------------------------------------------------------

class OperatorConsole(Protocol):
    """Whoever currently holds the browser. Small on purpose, so the terminal
    implementation and a production review queue are substitutable."""

    def ask(self, request: InterventionRequest) -> Response: ...

    def approve(self, *, capability: str, step: Step) -> bool: ...


class ConsoleOwner:
    """A person at the terminal. The CLI's default console, and the mocked part.

    `auto` exists so the demo and the test suite can run unattended. It answers
    every handoff with ABORT and every approval with no, never with yes: an
    automatic yes would leave the escalation path untested in exactly the way
    that matters, and would quietly approve writes.
    """

    def __init__(self, *, auto: bool = False, stream: Any = None) -> None:
        self.auto = auto
        self.out = stream or sys.stderr

    def ask(self, request: InterventionRequest) -> Response:
        print(request.render(), file=self.out)
        if self.auto:
            print("  [unattended: aborting rather than guessing]\n", file=self.out)
            return Response(Decision.ABORT, "no operator available")
        print("  the browser is yours. [r]esume  [s]kip step  [a]bort  [c]ompleted by hand",
              file=self.out)
        try:
            choice = input("  > ").strip().lower()[:1]
            note = input("  what did you do? ").strip()
        except (EOFError, KeyboardInterrupt):
            return Response(Decision.ABORT, "no input available")
        return Response({"r": Decision.RESUME, "s": Decision.SKIP,
                         "c": Decision.COMPLETED}.get(choice, Decision.ABORT),
                        note=note or "(no note given)")

    def approve(self, *, capability: str, step: Step) -> bool:
        detail = step.text or (step.target.primary.value if step.target else "")
        print(f"\n  APPROVAL  {capability} step {step.index}: "
              f"{step.action.value} {detail!r} [{step.risk.value}]", file=self.out)
        if self.auto:
            print("  [unattended: declining]\n", file=self.out)
            return False
        try:
            return input("  approve? [y/N] ").strip().lower().startswith("y")
        except (EOFError, KeyboardInterrupt):
            return False


@dataclass
class ScriptedConsole:
    """Non-interactive stand-in for tests and demos.

    `actions` is how a test says "the human moved the page": it is handed the same
    OwnedPage a real operator would drive, so the resume path sees genuine drift
    rather than a simulated flag.
    """

    decision: Decision = Decision.RESUME
    note: str = "fixed by hand"
    approves: bool = False
    actions: Callable[[OwnedPage], None] | None = None
    presented: list[InterventionRequest] = field(default_factory=list)

    def ask(self, request: InterventionRequest) -> Response:
        self.presented.append(request)
        return Response(self.decision, self.note)

    def approve(self, *, capability: str, step: Step) -> bool:
        return self.approves

    def act(self, page: OwnedPage) -> None:
        if self.actions:
            self.actions(page)


# --------------------------------------------------------------------------
# takeover and handback
# --------------------------------------------------------------------------

@dataclass
class TakeoverRecord:
    """What the human did, captured as evidence rather than taken on trust."""

    request_id: str
    decision: Decision
    url_before: str
    url_after: str
    note: str
    seconds: float
    screenshot_after: str | None = None

    @property
    def page_moved(self) -> bool:
        return self.url_before != self.url_after

    def to_json(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "decision": self.decision.value,
            "url_before": self.url_before, "url_after": self.url_after,
            "page_moved": self.page_moved, "note": self.note,
            "seconds": round(self.seconds, 3), "screenshot_after": self.screenshot_after,
        }


def operator_takeover(
    request: InterventionRequest,
    page: PageLike,
    owner: SessionOwner,
    console: OperatorConsole,
) -> TakeoverRecord:
    """Hand the operator the same live page, then record what they did.

    The same page, not a fresh one: the entire value of escalating rather than
    failing is that the operator inherits the session, the frameset and the
    half-filled form. Sending them a URL to reopen loses the session, turns a
    thirty second fix into a re-login, and on a real core banking product may not
    be reachable by URL at all.

    Returns with the session in RESUMING. It does not return control to the
    automation; only a re-verified checkpoint does that.
    """
    owner.to_operator(f"intervention {request.id}: {request.reason.value}")
    handle = OwnedPage(page, Owner.OPERATOR, owner)
    started = time.monotonic()
    url_before = _safe_url(page)

    try:
        response = console.ask(request)
        # A scripted console drives the page here; a human drives the real
        # browser window while `ask` is blocked on their answer. Both end up
        # having moved the same object, which is the point.
        act = getattr(console, "act", None)
        if act is not None:
            act(handle)
    finally:
        # Even if the console blew up mid-handoff, the session must not be left
        # stranded in OPERATOR with nobody holding it.
        hand_back(owner, request)

    directory = Path(request.evidence_dir) if request.evidence_dir else None
    record = TakeoverRecord(
        request_id=request.id,
        decision=response.decision,
        url_before=redact(url_before),
        url_after=redact(_safe_url(page)),
        note=redact(response.note),
        seconds=time.monotonic() - started,
        screenshot_after=(_try_screenshot(page, directory / f"{request.id}-after.png")
                          if directory else None),
    )
    if directory:
        _write_json(directory / f"{request.id}-takeover.json", record.to_json())
    return record


def hand_back(owner: SessionOwner, request: InterventionRequest) -> None:
    """OPERATOR -> RESUMING. Idempotent, so a caller's finally block is safe."""
    if owner.state is Owner.RESUMING:
        return
    owner.to_resuming(f"operator handed back {request.id}")


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------

class ResumeRefused(Exception):
    """The page moved while the human had control, and moved somewhere the run
    cannot safely continue from."""


@dataclass(frozen=True)
class ResumeVerdict:
    verified: bool
    checkpoint: dict[str, str] | None
    observed: str
    message: str


CheckpointVerifier = Callable[[Any, Checkpoint], tuple[bool, str]]


def resume_after_takeover(
    owner: SessionOwner,
    step: Step,
    page: PageLike,
    verify: CheckpointVerifier | None = None,
) -> ResumeVerdict:
    """Re-check the current step's checkpoint before trusting anything inherited.

    This is the part that is easy to skip and expensive to skip. Every position
    the replay engine holds -- which frame is current, which record is open, that
    the search was submitted -- was inferred from actions it took itself. A human
    who has just had the browser may have navigated away, signed in as somebody
    else, opened a different member or closed the frameset, and none of that
    invalidates a single variable in our process. Inherited state is a claim about
    the world made before a person changed the world. Resuming blindly at step N+1
    is how automation performs an action on the wrong record.

    So resume asserts the same condition the step asserted, against the page as it
    is now. It succeeds and returns control, or it fails and leaves the session in
    RESUMING for the caller to escalate again. It never guesses.

    `verify` is injectable because the replay engine's verifier knows about the
    artifact's frames and targets; the default below is the frame-aware fallback
    used when escalation runs on its own.
    """
    if owner.state is not Owner.RESUMING:
        raise OwnershipError(f"resume expects the session in resuming, found {owner.state.value}")

    if step.checkpoint is None:
        # No checkpoint means no way to prove the page is where we left it. Say
        # so in the verdict rather than letting the absence read as a pass.
        owner.to_automation("no checkpoint on step, resumed unverified")
        return ResumeVerdict(True, None, redact(_safe_url(page)),
                             f"step {step.index} has no checkpoint; resumed without proof")

    ok, observed = (verify or default_verify)(page, step.checkpoint)
    if ok:
        owner.to_automation(f"checkpoint re-verified at step {step.index}")
        return ResumeVerdict(True, step.checkpoint.to_json(), redact(observed),
                             f"step {step.index} checkpoint holds after takeover")
    return ResumeVerdict(False, step.checkpoint.to_json(), redact(observed),
                         f"step {step.index} checkpoint failed after takeover; "
                         "the page is not where the automation left it")


def default_verify(page: Any, checkpoint: Checkpoint) -> tuple[bool, str]:
    """Frame-aware checkpoint check.

    The fixture is a frameset, so `page.content()` returns the frameset markup and
    none of the text. Anything reading only the top document reports a false
    negative on every text checkpoint here, which is worth a few lines to avoid.
    """
    if checkpoint.kind == "url_contains":
        url = _safe_url(page)
        return checkpoint.value in url, url

    text = _all_text(page)
    excerpt = redact(" ".join(text.split())[:200])
    if checkpoint.kind == "text_present":
        return checkpoint.value in text, excerpt
    if checkpoint.kind == "text_absent":
        return checkpoint.value not in text, excerpt
    if checkpoint.kind == "element_present":
        finder = getattr(page, "query_selector", None)
        if finder is None:
            return checkpoint.value in text, excerpt
        try:
            return finder(checkpoint.value) is not None, excerpt
        except Exception as exc:                       # a bad selector is a failed check
            return False, f"selector error: {exc}"
    return False, f"unknown checkpoint kind {checkpoint.kind!r}"


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

class Escalator:
    """Owns the handoff, the ownership transitions, and the re-verification.

    The object replay reaches for. Everything it does is available piecemeal
    above; this exists so the caller cannot assemble the sequence wrongly, in
    particular cannot resume without re-verifying.
    """

    def __init__(self, console: OperatorConsole, evidence: Any = None,
                 owner: SessionOwner | None = None) -> None:
        self.console = console
        self.evidence = evidence
        self.owner = owner or SessionOwner()
        self.history: list[tuple[InterventionRequest, TakeoverRecord]] = []

    def escalate(self, request: InterventionRequest, page: PageLike) -> TakeoverRecord:
        self._note(request.reason.value, request.question)
        record = operator_takeover(request, page, self.owner, self.console)
        self.history.append((request, record))
        self._note(request.reason.value, request.question,
                   answer=f"{record.decision.value}: {record.note}".strip(": "))
        return record

    def resume(self, step: Step, page: PageLike,
               verifier: CheckpointVerifier | None = None) -> ResumeVerdict:
        """Re-verify and return control, or refuse and stay in RESUMING."""
        verdict = resume_after_takeover(self.owner, step, page, verifier)
        if self.evidence is not None and verdict.verified:
            _safely(lambda: self.evidence.event("resume_verified", checkpoint=verdict.checkpoint))
        return verdict

    def verify_resume(self, page: Any, last_checkpoint: Checkpoint | None,
                      verifier: CheckpointVerifier | None = None) -> None:
        """Raising form of `resume`, for callers that treat a moved page as fatal.

        A missing checkpoint is itself a refusal to guess: the failure happened
        before anything was confirmed, so there is no position to resume from.
        """
        if last_checkpoint is None:
            raise ResumeRefused("no confirmed checkpoint to resume from; the run cannot "
                                "verify which screen it is on")
        ok, observed = (verifier or default_verify)(page, last_checkpoint)
        if not ok:
            raise ResumeRefused(
                f"page moved during handoff: expected {last_checkpoint.kind}="
                f"{last_checkpoint.value!r}, saw {redact(observed)!r}")
        if self.evidence is not None:
            _safely(lambda: self.evidence.event("resume_verified",
                                                checkpoint=last_checkpoint.to_json()))

    def approve(self, *, capability: str, step: Step) -> bool:
        """So an Escalator can be handed to `Policy(approver=...)` directly."""
        return self.console.approve(capability=capability, step=step)

    def _note(self, reason: str, question: str, answer: str | None = None) -> None:
        if self.evidence is None:
            return
        # A recorder that throws must never change an outcome.
        _safely(lambda: self.evidence.escalation(reason, question, answer=answer))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _all_text(page: Any) -> str:
    frames = getattr(page, "frames", None) or [page]
    parts: list[str] = []
    for frame in frames:
        try:
            parts.append(frame.content())
        except Exception:
            continue                                   # a detached frame is not fatal
    return "\n".join(parts)


def _safe_url(page: Any) -> str:
    try:
        return page.url
    except Exception as exc:
        return f"(url unavailable: {exc})"


def _try_screenshot(page: Any, path: Path) -> str | None:
    """Escalation must not fail because the screenshot did.

    A packet without a picture is degraded. A crash here loses the live page,
    which is the whole asset the operator was about to be handed.
    """
    try:
        page.screenshot(path=str(path))
        return str(path)
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(redact_json(payload), indent=2, default=str), encoding="utf-8")


def _safely(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:
        pass


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def _demo() -> None:
    from .schema import ActionKind, Risk

    class FakePage:
        """Stands in for a playwright Page. Everything else in the demo is real."""

        def __init__(self, url: str, body: str) -> None:
            self.url = url
            self._body = body

        def content(self) -> str:
            return self._body

        def goto(self, url: str, body: str) -> None:
            self.url, self._body = url, body

        def screenshot(self, *, path: str) -> None:
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n(fake)")

    capability = Capability(
        name="lookup_member_balance", description="", app_id="meridian-core",
        entry_url="http://127.0.0.1:8899/", params=[], outputs=[], steps=[], approved=True)
    step = Step(index=4, action=ActionKind.READ, risk=Risk.SAFE,
                checkpoint=Checkpoint("text_present", "Member Detail"))
    evidence = Path("evidence/demo-escalation")
    detail = "http://127.0.0.1:8899/members/search?member_id=12345"

    print("== ownership refuses the second writer ==")
    owner = SessionOwner()
    page = FakePage("http://127.0.0.1:8899/members/search?fail=timeout&member_id=12345",
                    "<b>Session Expired</b> Please sign in again.")
    stale = OwnedPage(page, Owner.AUTOMATION, owner)
    print(f"  automation reads the url while it owns the session: {stale.url}")
    owner.to_operator("demo")
    try:
        stale.content()
    except OwnershipError as exc:
        print(f"  OwnershipError: {exc}")
    try:
        owner.to_automation("skipping the re-verify")
    except TransitionError as exc:
        print(f"  TransitionError: {exc}")
    owner.to_resuming("demo")
    owner.to_automation("demo")

    print("\n== case 1: the operator fixes the page ==")
    request = raise_intervention(Reason.AUTH, capability, step, page, evidence,
                                 observed="Session Expired")
    print(request.render())
    console = ScriptedConsole(
        note="re-authenticated and reopened member 12345",
        actions=lambda p: p.goto(detail, "<b>Member Detail</b> Dolores Abernathy 4812.55"))
    record = operator_takeover(request, page, owner, console)
    print(f"  takeover: decision={record.decision.value} moved={record.page_moved} "
          f"note={record.note!r}")
    verdict = resume_after_takeover(owner, step, page)
    print(f"  resume: verified={verdict.verified} owner={owner.state.value} :: {verdict.message}")

    print("\n== case 2: the operator leaves the page somewhere else ==")
    owner2 = SessionOwner()
    page2 = FakePage("http://127.0.0.1:8899/members/search", "<b>Session Expired</b>")
    request2 = raise_intervention(Reason.AUTH, capability, step, page2, evidence)
    stray = ScriptedConsole(note="got distracted, ended up on reports",
                            actions=lambda p: p.goto("http://127.0.0.1:8899/reports", "<b>Reports</b>"))
    operator_takeover(request2, page2, owner2, stray)
    verdict2 = resume_after_takeover(owner2, step, page2)
    print(f"  resume: verified={verdict2.verified} owner={owner2.state.value} :: {verdict2.message}")
    print(f"  observed: {verdict2.observed}")
    try:
        OwnedPage(page2, Owner.AUTOMATION, owner2).content()
    except OwnershipError as exc:
        print(f"  automation is still blocked: {exc}")

    print("\n== trail ==")
    for entry in owner.trail():
        print(f"  {entry}")
    print("\n  evidence written:", sorted(p.name for p in evidence.iterdir()))
    print("  nothing regulated on disk:",
          "12345" not in (evidence / f"{request.id}.json").read_text())


if __name__ == "__main__":
    _demo()
