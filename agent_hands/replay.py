"""Executing a recorded capability. Deterministic by construction.

There is no model in this module, and no import that could reach one. That is
the central claim of the design, so it is worth being blunt about why.

A recorded flow that re-plans at runtime is not a capability, it is an agent with
extra steps: it costs a model call per invocation, it can do something the
reviewer never approved, and two runs of the same input can differ. Determinism
is what makes the artifact reviewable -- a person reads the steps, approves them,
and what runs is those steps.

The interesting work is therefore in the failure taxonomy, not the happy path.
Three things can go wrong and they are not the same thing:

- **The page moved but the flow is fine.** An interstitial, a slow render, a
  session that ended. Recovered from, bounded, and every recovery is written
  down; silent recovery is how a system degrades for months with nobody noticing.
- **The application answered, and the answer was "no".** No such member. That is
  a correctly automated lookup, and reporting it as a crash is what makes a
  capability useless for the case it was built for.
- **The recording no longer matches the application.** A control that no strategy
  resolves, a checkpoint that will not hold, a refusal, an error screen. That is
  a real failure and it stops the run, with enough evidence to repair the
  artifact without re-running anything against production.

Only the third is a FAILURE. Collapsing the second into it is the single most
common mistake in this kind of system, and `Outcome` exists to make it awkward.

Three ordering rules the rest of the file exists to keep:

*Conditions before answers before checkpoints.* A session timeout is never read
as an answer, and a legitimate "not found" is never reported as a broken click.

*Every step verifies itself.* A click that did not arrive fails at that step, not
three steps later as an unrelated extraction error.

*Every wait is a condition.* There is no sleep in this file. Waits are locator
waits that return the instant the page reaches a state we recognize, which is
exactly what lets a slow application be a recovery rather than a failure.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Literal

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .escalation import Decision as OperatorDecision
from .escalation import Escalator, Reason, ResumeRefused, raise_intervention
from .perception import (
    Observation, TargetResolutionError, frame_by_key, observe, resolve_detail,
)
from .policy import Policy, PolicyViolation
from .schema import (
    ActionKind, BusinessRule, Capability, Checkpoint, Degradation, Outcome, ReplayResult,
    Step, Target, output_violation,
)

# Short wait first, then one longer one. A step that is merely slow is treated
# as a symptom worth waiting out, not as a failure. Two numbers rather than a
# per-step setting: if a step needs its own timeout, it is really waiting on a
# sleep.
STEP_TIMEOUT_MS = 2_000
SLOW_TIMEOUT_MS = 12_000

# Three tries per step. Low on purpose. A step that needs four has bad
# targeting, and retrying harder just hides that.
MAX_ATTEMPTS = 3

# Starting the whole flow over is the last resort, so it is capped. A popup
# that comes back every time you dismiss it is not going away, and the caller
# should be told that instead of watching us loop.
MAX_REENTRIES = 3
MAX_SESSION_RECOVERIES = 1
# How many times we dismiss the same popup on one step before deciding it is
# not going away.
MAX_DISMISSALS = 2

# A handoff that comes back "fixed" buys the step another attempt. Escalation
# only ever happens on a step's last attempt, because `_reobserve` absorbs the
# earlier ones, so a resume with no attempt left could never re-run the step it
# had just asked somebody to fix. Capped, because a person who hands the session
# back unchanged is not making progress and the run should say so rather than
# keep asking.
MAX_RESUMES = 2


class ParamError(ValueError):
    """The invocation does not satisfy the capability's declared parameters.

    Raised before the browser is touched. A malformed call is the caller's bug
    rather than a replay outcome: returning it as a FAILURE would bury it among
    the real automation failures, and discovering it halfway through would leave
    a part-completed flow on a member's record.
    """


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------

def frame_for(page: Any, step: Step | None = None) -> Any:
    """Which half of the window a step's checks apply to.

    Framesets are why this exists. The nav frame repeats most of the words the
    content frame uses, so "is 'Member Search' on the page" is also true on the
    access-denied screen. Asking about one frame instead of the whole page is the
    difference between a real check and a coincidence.
    """
    key = None
    if step is not None and step.target and step.target.candidates:
        key = step.target.primary.frame
    for wanted in (key, "content"):
        if not wanted:
            continue
        frame = frame_by_key(page, wanted)
        if frame is not None:
            return frame
    return getattr(page, "main_frame", page)


def frame_text(frame: Any) -> str:
    """The frame's visible text, for matching answers against and for evidence.

    `html` rather than `body`, because a frameset's top document has no body and
    `frame_for` falls back to it whenever a tenant names its frames something
    other than "content". That read waits out the whole timeout and then reports
    nothing, so the capped `html` read is both faster and the one that works.
    """
    try:
        return frame.inner_text("html", timeout=STEP_TIMEOUT_MS)
    except PlaywrightError:      # the frame navigated out from under the read
        return ""


def page_text(page: Any) -> str:
    """Visible text across every frame. For evidence, not for decisions.

    Kept separate from `frame_text` on purpose: a diagnostic excerpt wants
    everything that was on screen, and a checkpoint wants one frame. Using this
    for both is exactly the bug `frame_for` exists to prevent.
    """
    parts = []
    for frame in list(getattr(page, "frames", None) or [page]):
        try:
            parts.append(frame.evaluate("() => document.body ? document.body.innerText : ''"))
        except Exception:
            continue
    return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# checkpoints
# --------------------------------------------------------------------------

def _hold(frame: Any, checkpoint: Checkpoint, timeout_ms: int) -> None:
    """Wait for the checkpoint, raising PlaywrightTimeout if it never holds.

    Every branch is a Playwright wait rather than a read-and-compare, so the
    assertion is also the synchronisation. That is what removes the settle sleep
    this kind of engine otherwise grows after every click.
    """
    kind, value = checkpoint.kind, checkpoint.value
    if kind == "url_contains":
        # The frame's URL, not the page's. In a frameset the outer document
        # sits at "/" the whole session, so matching on it tells you nothing.
        frame.wait_for_url(lambda url: value in url, timeout=timeout_ms)
    elif kind == "text_present":
        frame.get_by_text(_exactly(value)).first.wait_for(state="visible", timeout=timeout_ms)
    elif kind == "text_absent":
        frame.get_by_text(_exactly(value)).first.wait_for(state="detached", timeout=timeout_ms)
    elif kind == "element_present":
        # Matched by name, so a recording can say "the Search button is here"
        # without depending on markup that changes every render.
        if not _named_control(frame, value):
            raise PlaywrightTimeout(f"no control named {value!r}")
    else:
        raise PlaywrightTimeout(f"unknown checkpoint kind {kind!r}")


def _exactly(value: str) -> "re.Pattern[str]":
    """Case-sensitive substring matching, which is what a checkpoint means.

    `get_by_text("MAIN MENU")` is case-insensitive, so it matched MERIDIAN's
    function-key bar -- `F3=Sign Off  F5=Main Menu  F7=Member Inquiry` -- which
    the application prints on every screen it renders, including the one it
    returns when a sign-on is refused. Step 4 of every artifact here holds on
    "MAIN MENU", so signing on with the wrong password reported success and
    exit 0. A run that did not do the thing, saying it did, is the one failure
    this engine exists to make impossible.

    A regex restores case sensitivity without giving up the wait, so the
    assertion is still the synchronisation. It also lines replay up with
    `default_verify`, which has always compared with a plain `in` -- the two
    halves of the same checkpoint disagreeing was the deeper bug.

    Substring, not equality: a checkpoint names a phrase on the screen, not the
    whole of an element's text.
    """
    return re.compile(re.escape(value))


def _named_control(frame: Any, name: str) -> bool:
    for role in ("button", "link", "textbox", "combobox", "checkbox"):
        try:
            if frame.get_by_role(role, name=name).count():
                return True
        except PlaywrightError:
            continue
    return False


def verify(page: Any, checkpoint: Checkpoint, *, frame: Any = None,
           timeout_ms: int = STEP_TIMEOUT_MS) -> tuple[bool, str]:
    """Did the step actually arrive? Returns the answer and what was on screen.

    It returns what it saw even when the check passed, because that goes into the
    evidence file, and a good run's evidence is what you compare against when a
    later one fails.

    Public rather than private so the escalation path checks position using this
    same definition, instead of inventing a second one.
    """
    scope = frame if frame is not None else frame_for(page)
    try:
        _hold(scope, checkpoint, timeout_ms)
    except (PlaywrightTimeout, PlaywrightError):
        return False, _excerpt(frame_text(scope))
    return True, checkpoint.value


def _wait_for_any_text(frame: Any, needles: Iterable[str], timeout_ms: int) -> None:
    """Block until any one of `needles` is visible in the frame.

    A single or-ed locator rather than a polling loop: it returns the instant the
    page arrives, and survives the navigation a form submit causes, because
    locator waits are re-evaluated against whatever document is current. This is
    what replaces the settle sleep after a click.
    """
    locator = None
    for needle in needles:
        candidate = frame.get_by_text(needle)
        locator = candidate if locator is None else locator.or_(candidate)
    if locator is not None:
        locator.first.wait_for(state="visible", timeout=timeout_ms)


# --------------------------------------------------------------------------
# conditions the engine recognizes on any application
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Condition:
    """A screen about the session or the server, rather than about the request.

    Session timeouts and server errors live here, in the engine, because they are
    true of this kind of application generally. "No such member" lives in the
    artifact instead, because it is true of one bank. That split is the point.

    `kind` is what we decided it is; `tactic` is what we do about it. Kept apart
    so you can change the response without re-arguing the diagnosis.
    """

    code: str
    when_text: str          # lowercase; matched as a substring of the frame's text
    kind: Literal["recoverable", "failure"]
    tactic: Literal["dismiss", "reenter", "stop"] = "stop"
    expected: str = ""


# Order matters. Check for a logged-out screen first: a login page can still
# show the old page's words, and reading those as an answer would report "no
# such member" for a member who exists.
CONDITIONS: tuple[Condition, ...] = (
    Condition("session_expired", "session has expired", "recoverable", "reenter"),
    Condition("interstitial", "scheduled maintenance", "recoverable", "dismiss"),
    Condition("permission_denied", "you do not have permission", "failure", "stop",
              "the record this flow was recorded against"),
    Condition("app_error", "unexpected error occurred", "failure", "stop",
              "the record this flow was recorded against"),
)

# The words that appear on a popup's dismiss button, best first. A fixed list
# rather than guessing: old apps use a handful of words, and replay does not
# guess.
_DISMISS = ("Continue", "OK", "Acknowledge", "Close", "Proceed")


@dataclass(frozen=True)
class AppProfile:
    """The wording one application uses for the states the engine recognizes.

    `BusinessRule` already lives in the artifact, on the argument that which
    screens mean what is a fact about one application and a shared engine that
    hard-codes one tenant's error strings is wrong for the next tenant. The same
    argument applies to conditions, and for a long time this module ignored it:
    the table was a module constant that looked for "session has expired", so
    against a target that says "your session has timed out" two of its six
    exceptional states were dead code.

    Conditions live here rather than in the artifact because they belong to the
    application, not to one recorded flow. Every capability against a given app
    shares them, and duplicating them into each artifact would mean fixing a
    wrong string in twenty files.
    """

    conditions: tuple[Condition, ...] = CONDITIONS
    dismiss: tuple[str, ...] = _DISMISS

    def condition(self, text: str) -> Condition | None:
        low = text.lower()
        for rule in self.conditions:
            if rule.when_text in low:
                return rule
        return None


DEFAULT_PROFILE = AppProfile()

# Keyed by the artifact's `app_id`. Adding a target means adding an entry, not
# editing the engine, which is the difference between adapting by configuration
# and editing the engine for the second customer.
PROFILES: dict[str, AppProfile] = {
    "meridian-core": AppProfile(conditions=(
        Condition("session_expired", "your session has timed out", "recoverable", "reenter"),
        Condition("interstitial", "scheduled maintenance", "recoverable", "dismiss"),
        Condition("app_error", "an unexpected error occurred", "failure", "stop",
                  "the record this flow was recorded against"),
    )),
    # Note what is deliberately absent. This target refuses a teller a
    # supervisor-only action with "SUPERVISOR OVERRIDE REQUIRED", and that is
    # the application answering rather than faulting -- so it belongs in the
    # artifact as a BusinessRule, where it comes back as a business outcome the
    # caller can act on. Adding it here would make it a hard failure and lose
    # the distinction. Conditions are for states the engine can do something
    # about: retry, re-enter, dismiss. "Not you" is not one of those.
}


def _condition(text: str) -> Condition | None:
    """Module-level lookup against the default profile, kept for callers that
    have no capability in hand."""
    return DEFAULT_PROFILE.condition(text)


def _match_business(cap: Capability, text: str) -> BusinessRule | None:
    low = text.lower()
    for rule in cap.business_rules:
        if rule.when_text.lower() in low:
            return rule
    return None


@dataclass(frozen=True)
class _Reenter:
    """Signal to load the entry URL again and run the flow from the top.

    Returned rather than raised so it passes through the evidence recorder's step
    context manager without being written down as an error. A recovery is not a
    failure and the run file should not have to be read twice to see that.
    """

    code: str
    detail: str


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
}


def bind_params(cap: Capability, params: dict[str, Any]) -> dict[str, Any]:
    """Check an invocation against the declared parameters, before anything runs.

    Unknown names are rejected rather than ignored: a mistyped parameter that is
    silently dropped runs the flow with whatever the page already had and returns
    a confident wrong answer, which is the worst outcome available.
    """
    declared = {p.name: p for p in cap.params}
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise ParamError(f"unknown parameter(s) for {cap.name!r}: {', '.join(unknown)}")

    bound: dict[str, Any] = {}
    for name, spec in declared.items():
        value = params.get(name)
        if value is None:
            if spec.required:
                raise ParamError(f"missing required parameter {name!r}")
            # Bound to nothing rather than left unbound. A step is allowed to
            # reference an optional parameter -- a memo field on a transfer is
            # the obvious case -- and `substitute` refuses a placeholder it
            # cannot fill, so leaving it out made every optional parameter a
            # required one the moment a step mentioned it.
            bound[name] = ""
            continue
        # In Python, True is also an int. Without this check, True would pass
        # as an account number.
        if isinstance(value, bool) != (spec.type == "boolean"):
            raise ParamError(
                f"parameter {name!r} must be {spec.type}, got {type(value).__name__}")
        if not isinstance(value, _TYPES[spec.type]):
            raise ParamError(
                f"parameter {name!r} must be {spec.type}, got {type(value).__name__}")
        bound[name] = value
    return bound


def substitute(template: str | None, bound: dict[str, Any], where: str = "step") -> str | None:
    """Fill `{name}` placeholders, refusing any the invocation did not bind.

    An unbound placeholder is an error, not a blank: left standing it would be
    typed into the application literally, and a member search for "{member_id}"
    comes back as a validation message that looks like a business outcome.
    """
    if template is None:
        return None

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bound:
            raise ParamError(f"{where} references unbound parameter {name!r}")
        return str(bound[name])

    return _PLACEHOLDER.sub(one, template)


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------

class Replay:
    """Runs one capability against one page under one policy."""

    def __init__(
        self,
        page: Any,
        policy: Policy | None = None,
        *,
        evidence: Any = None,
        escalator: Escalator | None = None,
        profile: AppProfile | None = None,
    ) -> None:
        self.page = page
        self.policy = policy
        self.evidence = evidence
        self.escalator = escalator
        # An explicit profile overrides the registry; without one the artifact's
        # app_id selects it, so a caller usually says nothing.
        self.profile = profile
        self._profile = profile or DEFAULT_PROFILE
        self._begin()

    def _begin(self) -> None:
        """Clear the state that belongs to one run.

        Called from `run` as well as here, because every one of these is a budget
        or a record for a single replay. A Replay used twice would otherwise
        start with the previous run's re-entry budget already spent, and would
        report recoveries it had not made.
        """
        self._last_checkpoint: Checkpoint | None = None
        self._handoff_note: str = ""
        self._recovered: list[str] = []
        self._degraded: list[Degradation] = []
        self._reentries = 0
        self._session_recoveries = 0
        # Counted per step. One popup per screen is normal; two on the same
        # screen is not.
        self._dismissals: dict[int, int] = {}

    # -- public ------------------------------------------------------------

    def run(self, cap: Capability, params: dict[str, Any] | None = None) -> ReplayResult:
        """Replay `cap`. Returns an outcome; raises only for a refusal to start.

        Parameters are bound and substituted into every step before the browser
        is touched, so a malformed call cannot leave a half-finished flow behind.
        """
        self._begin()
        self._profile = self.profile or PROFILES.get(cap.app_id, DEFAULT_PROFILE)
        bound = bind_params(cap, dict(params or {}))
        cap = _bind_targets(cap, bound)
        entry = substitute(cap.entry_url, bound, "entry_url") or cap.entry_url
        text = {s.index: substitute(s.text, bound, f"step {s.index} text") for s in cap.steps}
        urls = {s.index: substitute(s.url, bound, f"step {s.index} url") for s in cap.steps}

        if self.policy is not None:
            try:
                self.policy.check_capability(cap)
            except PolicyViolation as exc:
                # Returned, not raised. The caller is a program, and a refusal
                # is something it has to handle. Exceptions get swallowed, and
                # then "we declined" looks like "we crashed". ParamError still
                # raises, because that is the caller's mistake, not a result.
                return self._fail(
                    cap, None, f"policy[{exc.decision.gate}]", exc.decision.reason,
                    message="refused before starting",
                )

        self._event("replay_started", capability=cap.name, params=dict(bound),
                    artifact_version=cap.artifact_version, app_variant=cap.app_variant)

        while True:
            outcome = self._one_pass(cap, entry, text, urls)
            if isinstance(outcome, ReplayResult):
                return outcome
            self._reentries += 1
            if self._reentries > MAX_REENTRIES:
                return self._fail(cap, None, "the flow to reach its checkpoint",
                                  f"gave up after {MAX_REENTRIES} re-entries ({outcome.detail})",
                                  message="the same recoverable condition kept coming back")
            self._event("re_entering", attempt=self._reentries, code=outcome.code,
                        detail=outcome.detail)

    def _one_pass(
        self, cap: Capability, entry: str, text: dict[int, str | None],
        urls: dict[int, str | None],
    ) -> ReplayResult | _Reenter:
        outputs: dict[str, Any] = {}
        for step in cap.steps:
            try:
                early = self._step(cap, step, entry, text, urls, outputs)
            except _Abort as exc:
                return self._fail(cap, step.index, exc.expected, exc.observed, exc.message)
            if early is not None:
                return early          # a business outcome, or a re-entry signal
        return self._succeed(cap, outputs)

    # -- one step ----------------------------------------------------------

    def _step(
        self, cap: Capability, step: Step, entry: str, text: dict[int, str | None],
        urls: dict[int, str | None], outputs: dict[str, Any],
    ) -> ReplayResult | _Reenter | None:
        # The question is "are we allowed to act here". For a navigate, that
        # means where we are going; for everything else, where we already are.
        # Checking the page we are leaving would be pointless.
        where = (urls[step.index] or entry) if step.action is ActionKind.NAVIGATE else self._url()
        self._gate(cap, step, where)

        recorder = self.evidence.step(step) if self.evidence else _NullStep()
        with recorder as record:
            record.risk = step.risk.value
            budget, attempt, resumes = MAX_ATTEMPTS, 0, 0
            while attempt < budget:
                attempt += 1
                last = attempt == budget
                # Before doing it again, check whether it already happened.
                # Dismissing a popup often lands you where you were going, and
                # repeating the click would walk straight back into it. On an
                # irreversible step that is a duplicate payment, which no
                # amount of retrying can undo.
                if attempt > 1 and step.checkpoint is not None and self._already_there(step):
                    record.checkpoint_ok = True
                    record.extra["satisfied_by_recovery"] = True
                    self._last_checkpoint = step.checkpoint
                    self._event("satisfied_by_recovery", index=step.index,
                                checkpoint=step.checkpoint.to_json())
                    return None
                try:
                    self._act(cap, step, entry, text, urls, outputs, record)
                except TargetResolutionError as exc:
                    record.attempted = [_as_target(a) for a in exc.attempts]
                    if not last and self._reobserve(step):
                        continue
                    if self._unresolved(cap, step, exc):
                        budget, resumes = _after_resume(budget, resumes)
                        continue
                except (PlaywrightTimeout, PlaywrightError) as exc:
                    # We found the control but it would not take the action:
                    # covered, disabled, or gone mid-flight. Worth one more
                    # look, because that is what a stale handle looks like.
                    if not last and self._reobserve(step):
                        continue
                    raise _Abort(f"{step.action.value} could not be carried out",
                                 _expected(step), _excerpt(str(exc))) from exc
                verdict = self._read(cap, step, record, outputs, last)
                if verdict is _RETRY:
                    budget, resumes = _after_resume(budget, resumes)
                    continue
                return verdict          # ReplayResult, _Reenter, or None to go on
            # Every conclusive path above returns from inside the loop, so
            # arriving here means the attempts ran out with the step unfinished.
            # Returning None would tell `_one_pass` the step was done, and the
            # run would end in success with this step's checkpoint never
            # verified and, for a click, the click never sent.
            raise _Abort(f"{step.action.value} did not complete in {attempt} attempts",
                         _expected(step), _excerpt(page_text(self.page)))

    def _already_there(self, step: Step) -> bool:
        """Does this step's checkpoint hold right now, without acting?

        Deliberately given a short budget: this is a cheap "did we get there
        anyway" probe between attempts, not the authoritative wait, which
        `_read` still performs with the full timeout.
        """
        if step.checkpoint is None:
            return False
        try:
            _hold(frame_for(self.page, step), step.checkpoint, STEP_TIMEOUT_MS // 4)
            return True
        except (PlaywrightTimeout, PlaywrightError):
            return False

    def _gate(self, cap: Capability, step: Step, where: str) -> None:
        if self.policy is None:
            return
        decision = self.policy.check_step(cap, step, where)
        if decision.allowed:
            return
        # A missing approval is something a person can answer, so hand it over.
        # Any other refusal needs the policy or the artifact changed on purpose,
        # not someone waving it through at 3am.
        if decision.gate == "risk" and self.escalator is not None:
            if self._handoff(cap, step, Reason.APPROVAL, decision.reason):
                return
            raise _Abort(f"declined: {self._handoff_note}" if self._handoff_note
                         else "declined",
                         f"approval for step {step.index}", decision.reason)
        raise _Abort(f"policy[{decision.gate}]", decision.reason, self._url())

    # -- what the page came back with --------------------------------------

    def _read(
        self, cap: Capability, step: Step, record: Any, outputs: dict[str, Any], last: bool,
    ) -> Any:
        """Classify what is on the page, then hold the checkpoint.

        The frame is re-resolved here rather than carried over from before the
        action, because the action is usually what replaced it.
        """
        frame = frame_for(self.page, step)
        if step.checkpoint is not None:
            needles = [c.when_text for c in self._profile.conditions]
            needles += [b.when_text for b in cap.business_rules]
            if step.checkpoint.kind == "text_present":
                needles.append(step.checkpoint.value)
            try:
                self._patiently(step.index, "the page",
                                lambda ms: _wait_for_any_text(frame, needles, ms))
            except PlaywrightTimeout:
                pass      # nothing recognized arrived; the checkpoint is the verdict

        # No checkpoint means the recording is saying there is nothing to
        # arrive at, like typing in a field. Waiting for a state nobody expects
        # would burn the full timeout on every such step and find nothing, so
        # we just read the page as it is. Anything genuinely on screen is still
        # caught, and anything still rendering is caught by the next step.
        text = frame_text(frame)

        condition = self._profile.condition(text)
        if condition is not None:
            self._event("condition", index=step.index, code=condition.code,
                        verdict=condition.kind, tactic=condition.tactic)
            record.observed = condition.code
            return self._respond(cap, step, condition, frame)

        hit = _match_business(cap, text)
        if hit is not None:
            self._event("business_outcome", index=step.index, code=hit.code)
            record.observed = hit.code
            record.checkpoint_ok = True
            return self._business(cap, hit, outputs)

        if step.checkpoint is None:
            record.checkpoint_ok = True
            return None

        ok, seen = self._hold_patiently(step, frame)
        record.checkpoint_ok = ok
        record.observed = seen
        if ok:
            self._last_checkpoint = step.checkpoint
            return None

        # Not retried, on purpose. The action already happened and the page has
        # usually moved, so running the step again would either resubmit the
        # form or fail to find a control that is gone, then blame that. The
        # longer wait above was the second chance; this is the verdict.
        self._capture(step, f"checkpoint failed: {step.checkpoint.to_json()}")
        if self._handoff(
            cap, step, Reason.CHECKPOINT,
            f"step {step.index} did not arrive where the recording expected",
            expected=f"{step.checkpoint.kind}={step.checkpoint.value!r}", observed=seen,
        ):
            return _RETRY          # the operator fixed it and resume re-verified
        raise _Abort("checkpoint failed",
                     f"{step.checkpoint.kind}={step.checkpoint.value!r}", seen)

    def _hold_patiently(self, step: Step, frame: Any) -> tuple[bool, str]:
        checkpoint = step.checkpoint
        assert checkpoint is not None
        try:
            self._patiently(step.index, "the checkpoint",
                            lambda ms: _hold(frame, checkpoint, ms))
        except (PlaywrightTimeout, PlaywrightError):
            return False, _excerpt(frame_text(frame))
        return True, checkpoint.value

    def _respond(
        self, cap: Capability, step: Step, condition: Condition, frame: Any,
    ) -> ReplayResult | _Reenter | _Retry:
        if condition.kind == "failure":
            # Not retried. Trying a permission error again just reaches the
            # same screen more slowly. Someone has to fix it.
            self._capture(step, condition.code)
            self._handoff(cap, step, Reason.UNEXPECTED,
                          f"{condition.code.replace('_', ' ')} during step {step.index}",
                          expected=condition.expected, observed=_excerpt(frame_text(frame)))
            raise _Abort(condition.code, condition.expected or _expected(step),
                         _excerpt(frame_text(frame)))

        if condition.tactic == "dismiss":
            if self._dismissals.get(step.index, 0) >= MAX_DISMISSALS:
                # Dismissed it and it came straight back, so it is not going
                # away. Saying so here matters: otherwise the retries run out
                # and the run blames whatever the next attempt tripped over,
                # sending someone to fix a control that was never broken.
                self._capture(step, "interstitial kept reappearing")
                self._handoff(cap, step, Reason.LIMIT,
                              f"an interstitial blocked step {step.index} after "
                              f"{MAX_DISMISSALS} dismissals",
                              expected=_expected(step), observed=_excerpt(frame_text(frame)))
                raise _Abort("interstitial kept reappearing",
                             f"the notice to stay dismissed after {MAX_DISMISSALS} attempts",
                             _excerpt(frame_text(frame)))
            detail = self._dismiss(frame, condition, step)
            if detail is None:
                raise _Abort("interstitial could not be dismissed", _expected(step),
                             _excerpt(frame_text(frame)))
            self._dismissals[step.index] = self._dismissals.get(step.index, 0) + 1
            self._note(step.index, condition.code, detail)
            # What to do next depends on where dismissing left you. If the
            # popup was covering the screen you were heading for, the step is
            # already done and repeating it would be a second write for nothing.
            # If it bounced you back to the start, whatever you typed went with
            # it, and only starting the flow over puts it back. Retrying just
            # the step would submit an empty form and blame the checkpoint.
            if self._already_there(step):
                return _RETRY
            return _Reenter(condition.code, "dismissed an interstitial")

        # Starting over means loading the entry URL again. This system never
        # handles passwords, so if the app still wants a login after that, a
        # person decides what happens next.
        if self._session_recoveries >= MAX_SESSION_RECOVERIES:
            self._capture(step, "session expired again after re-entering")
            self._handoff(cap, step, Reason.AUTH,
                          f"the session expired twice during step {step.index}",
                          expected="an authenticated session",
                          observed=_excerpt(frame_text(frame)))
            raise _Abort("session expired", "an authenticated session",
                         "expired again after re-entering once")
        self._session_recoveries += 1
        self._note(step.index, condition.code, "loaded the entry URL again")
        return _Reenter(condition.code, "session expired")

    # -- actions -----------------------------------------------------------

    def _act(
        self, cap: Capability, step: Step, entry: str, text: dict[int, str | None],
        urls: dict[int, str | None], outputs: dict[str, Any], record: Any,
    ) -> None:
        if step.action is ActionKind.NAVIGATE:
            url = urls[step.index] or entry
            if self.policy is not None:
                try:
                    self.policy.check_navigation(url)
                except PolicyViolation as exc:
                    raise _Abort("policy[surface]", str(exc), url) from exc
            record.url = url
            self._patiently(step.index, "navigation",
                            lambda ms: self.page.goto(url, timeout=ms, wait_until="load"))
            return

        if step.action is ActionKind.WAIT_FOR:
            return          # the step's own checkpoint is the wait

        if step.target is None:
            raise ValueError(f"step {step.index} is {step.action.value} with no target")

        resolution = resolve_detail(self.page, step.target)
        # The strategies that failed are the early warning. A flow that has
        # slipped from "the link named Search" down to a raw HTML path still
        # passes, and is one release from not passing. Only the log shows it.
        record.resolved_by(resolution.winner,
                           after=[_as_target(a) for a in resolution.attempts[:resolution.rank]])
        if resolution.degraded:
            record.extra["degraded_to_rank"] = resolution.rank
            # Recorded once per step, not once per attempt. A retry and a
            # whole-flow re-entry both resolve the same target again, and a
            # degradation is a standing fact about that target against this
            # application rather than an event, so repeating it would make a
            # count of these spike on any run that merely met an interstitial.
            fell_to = Degradation(
                step=step.index, primary=step.target.primary.strategy,
                won_by=resolution.winner.strategy, rank=resolution.rank,
            )
            if fell_to not in self._degraded:
                self._degraded.append(fell_to)
        locator = resolution.locator
        value = text[step.index] or ""

        # Clicks and typing get the long timeout straight away, instead of the
        # short-then-longer retry used for waiting on the page. Playwright
        # already waits for the control to be usable, and a click is the one
        # thing that must never be sent twice. Whether it worked is the
        # checkpoint's job, not the timeout's.
        if step.action is ActionKind.CLICK:
            locator.click(timeout=SLOW_TIMEOUT_MS)
        elif step.action is ActionKind.TYPE:
            # The template, never the value, whenever the value came from a
            # parameter. Evidence is kept and reviewed, and a password typed into
            # a form is still a password once it is a line in a log. Redacting
            # the invocation but recording what was typed is all of the cost of
            # redaction and none of the benefit. The parameters are already in
            # `run_started`, with the redactor applied.
            record.text = step.text if step.text and _PLACEHOLDER.search(step.text) else value
            locator.fill(value, timeout=SLOW_TIMEOUT_MS)
        elif step.action is ActionKind.SELECT:
            locator.select_option(value, timeout=SLOW_TIMEOUT_MS)
        elif step.action is ActionKind.READ:
            read = (locator.inner_text(timeout=SLOW_TIMEOUT_MS) or "").strip()
            record.extra["read"] = read
            if step.extracts:
                # A read that lands one cell sideways returns a real string from
                # a real element, so the run has nothing to notice. Every other
                # step is checked by whether the screen changed; a read changes
                # nothing. The declared type is the only thing standing between
                # a shifted row and a date reported as a balance.
                declared = next((o.type for o in cap.outputs
                                 if o.name == step.extracts), "string")
                why = output_violation(read, declared)
                if why is not None:
                    record.extra["type_violation"] = why
                    raise _Abort(f"read for {step.extracts!r} failed its declared type",
                                 f"{step.extracts} is {declared}", _excerpt(read))
                outputs[step.extracts] = read

    # -- waiting and recovery ----------------------------------------------

    def _patiently(self, index: int, what: str, wait: Any) -> None:
        """Wait, and if the short budget expires, wait again for much longer.

        A slow application is a recoverable condition rather than a verdict. The
        recovery is only claimed when the second attempt actually arrived, so
        `recovered` never credits the run with surviving something it did not.
        """
        try:
            wait(STEP_TIMEOUT_MS)
            return
        except PlaywrightTimeout:
            pass
        wait(SLOW_TIMEOUT_MS)          # may raise again; the caller decides what that means
        self._note(index, "slow_response",
                   f"{what} took over {STEP_TIMEOUT_MS}ms; waited up to "
                   f"{SLOW_TIMEOUT_MS}ms and it arrived")

    def _reobserve(self, step: Step) -> bool:
        """Look again for a control that was not there a moment ago.

        No sleep, no tactic: we do not know why it was missing, so we just re-read
        the page and try to find it again. Capped by MAX_ATTEMPTS.

        Logged as an event, not a recovery. `recovered` means we recognized a
        named problem and handled it. If plain retries counted, a run that failed
        three times would claim three recoveries.
        """
        self._event("re_resolving", index=step.index, action=step.action.value)
        return True

    def _dismiss(self, frame: Any, condition: Condition, step: Step) -> str | None:
        """Click an interstitial's acknowledgement and confirm it actually went.

        The confirmation is the whole point. A dismissal that is assumed rather
        than checked is how a run records a recovery it did not make, carries on
        against a notice still on screen, and then reports the next control it
        cannot find as the fault -- sending someone to fix a control that was
        never broken.
        """
        for wanted in self._profile.dismiss:
            for role in ("link", "button"):
                try:
                    control = frame.get_by_role(role, name=wanted, exact=True)
                    if not control.count():
                        continue
                    control.first.click(timeout=SLOW_TIMEOUT_MS)
                except (PlaywrightTimeout, PlaywrightError):
                    continue
                # Found again from scratch, because dismissing a popup usually
                # navigates and the frame may be a different document now.
                after = frame_for(self.page, step)
                try:
                    after.get_by_text(condition.when_text).first.wait_for(
                        state="hidden", timeout=STEP_TIMEOUT_MS)
                except (PlaywrightTimeout, PlaywrightError):
                    return None          # clicked, but the notice is still there
                return f"clicked {role} {wanted!r}"
        return None

    def _note(self, index: int, tactic: str, detail: str) -> None:
        self._recovered.append(f"step {index}: {tactic} ({detail})")
        if self.evidence:
            self.evidence.recovery(index, tactic, detail)

    def _event(self, _event_kind: str, **fields: Any) -> None:
        # Underscored so a caller can pass a field actually named `kind` (the
        # condition events do) without clashing with this parameter. This is
        # the keyword collision that only showed up on the error path, where
        # nothing was exercising it.
        if self.evidence:
            self.evidence.event(_event_kind, **fields)

    # -- handoff -----------------------------------------------------------

    def _unresolved(self, cap: Capability, step: Step, exc: Exception) -> bool:
        self._capture(step, "no strategy resolved the control")
        if self._handoff(cap, step, Reason.UNRESOLVED,
                         f"step {step.index} ({step.action.value}) could not find its control",
                         expected=_expected(step), observed=_excerpt(page_text(self.page))):
            return True
        raise _Abort("unresolved control", _expected(step),
                     _excerpt(page_text(self.page))) from exc

    def _handoff(
        self, cap: Capability, step: Step | None, reason: Reason, question: str,
        *, expected: str | None = None, observed: str | None = None,
    ) -> bool:
        """Hand over. True means the operator fixed it and the step may re-run.

        The packet is built by `raise_intervention` rather than assembled here,
        so the screenshot and the redaction happen on the escalation module's
        terms; this engine should not be deciding what is safe to write to disk.
        """
        if self.escalator is None or step is None:
            return False
        request = raise_intervention(
            reason, cap, step, self.page,
            str(self.evidence.dir) if self.evidence else None,
            question=question, observed=observed,
            options=["fix the page in the open browser, then resume",
                     "abort the run"],
        )
        if expected:
            request.expected = expected
        record = self.escalator.escalate(request, self.page)
        # Resume is the only choice this engine acts on, so the console offers
        # only resume and abort. Skip-this-step and completed-by-hand are not
        # implemented: an option that silently quits is worse than no option.
        if record.decision is not OperatorDecision.RESUME:
            # Kept so the run can say which of these it was: a person aborting,
            # nobody answering, or the window being closed. "declined" on its
            # own sends a reader to the log to find out which.
            self._handoff_note = record.note
            return False
        try:
            self.escalator.verify_resume(
                self.page, self._last_checkpoint or _approval_anchor(reason, request), verify)
        except ResumeRefused as exc:
            # The person left the session somewhere we cannot continue from.
            # Refusing is the whole point of checking.
            self._note(step.index, "resume_refused", str(exc))
            return False
        return True

    def _capture(self, step: Step, reason: str) -> None:
        if self.evidence:
            self.evidence.capture_failure(
                self.page, step_index=step.index, reason=reason, targets=step.target)

    # -- terminal results --------------------------------------------------

    def _succeed(self, cap: Capability, outputs: dict) -> ReplayResult:
        return self._finish(ReplayResult(
            outcome=Outcome.SUCCESS, capability=cap.name, outputs=outputs,
            message="flow completed",
            recovered=list(self._recovered), degraded=list(self._degraded),
        ))

    def _business(self, cap: Capability, rule: BusinessRule, outputs: dict) -> ReplayResult:
        return self._finish(ReplayResult(
            outcome=Outcome.BUSINESS_OUTCOME, capability=cap.name, outputs=outputs,
            business_code=rule.code, message=rule.message or rule.code,
            recovered=list(self._recovered), degraded=list(self._degraded),
        ))

    def _fail(
        self, cap: Capability, step: int | None, expected: str | None,
        observed: str | None, message: str = "",
    ) -> ReplayResult:
        escalated = bool(self.escalator and self.escalator.history)
        if not escalated and self.policy is not None:
            escalated = bool(self.policy.should_escalate(cap, message))
        return self._finish(ReplayResult(
            outcome=Outcome.FAILURE, capability=cap.name, failed_step=step,
            expected=expected, observed=observed,
            message=message or "replay stopped",
            recovered=list(self._recovered), degraded=list(self._degraded),
            escalated=escalated,
        ))

    def _finish(self, result: ReplayResult) -> ReplayResult:
        return self.evidence.finish(result) if self.evidence else result

    def _url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""


def replay(
    capability: Capability,
    params: dict[str, Any],
    page: Any,
    policy: Policy | None = None,
    evidence: Any = None,
    *,
    escalator: Escalator | None = None,
) -> ReplayResult:
    """Replay one recorded capability against a live page.

    The functional entry point, and the one an agent runtime calls. Returns one
    of the three outcomes and does not raise for anything the application did; it
    raises `ParamError` for a malformed invocation and `PolicyViolation` for an
    artifact that may not be replayed at all, both of which are refusals to start
    rather than results of having started.
    """
    return Replay(page, policy, evidence=evidence, escalator=escalator).run(capability, params)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class _Retry:
    """Means "try this step again". Distinct from None, which means the step is
    done and the run moves on."""


_RETRY = _Retry()


class _Abort(Exception):
    """Internal: unwinds one step to the run loop with its diagnosis intact."""

    def __init__(self, message: str, expected: str | None, observed: str | None) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.observed = observed


class _NullStep:
    """Stands in for the evidence recorder when none is attached, so the engine
    has one code path rather than an `if evidence` around every write."""

    def __enter__(self) -> Any:
        from .evidence import StepRecord
        self._record = StepRecord(index=-1, action="")
        return self._record

    def __exit__(self, *exc: Any) -> Literal[False]:
        return False


def _as_target(attempt: Any) -> Target:
    """A failed Attempt, in the shape the evidence writer records.

    Durability is zeroed rather than carried over: this is a strategy that did
    not work, and leaving its recorded durability on it would read, in the
    evidence file, as though it had.
    """
    return Target(
        strategy=attempt.strategy, value=attempt.value, frame=attempt.frame,
        durability=0.0,
        note=attempt.reason or (
            f"matched {attempt.matched}" if attempt.matched is not None else "no match"
        ),
    )


def _bind_targets(cap: Capability, bound: dict[str, Any]) -> Capability:
    """The capability again, with parameters filled into its target values.

    Targets were the one place substitution never reached. A step could say what
    to type and where to go in terms of the invocation, but not *which control*,
    so a target meaning "the row whose first cell is {share_id}" went to the page
    with the braces still in it and matched nothing. Any flow that has to pick one
    row out of a table -- which is most of what a servicing console is -- needs
    this.

    A copy, because an artifact is loaded once and replayed many times, and the
    next caller's share id is not this one's. Skipped entirely when no target
    mentions a parameter, which is the common case.
    """
    if not any(_PLACEHOLDER.search(c.value)
               for step in cap.steps if step.target
               for c in step.target.candidates):
        return cap
    clone = copy.deepcopy(cap)
    for step in clone.steps:
        if step.target is None:
            continue
        step.target.candidates = [
            replace(c, value=substitute(c.value, bound, f"step {step.index} target") or c.value)
            for c in step.target.candidates
        ]
    return clone


def _after_resume(budget: int, resumes: int) -> tuple[int, int]:
    """A resume buys the step one more attempt, up to `MAX_RESUMES` of them."""
    if resumes >= MAX_RESUMES:
        return budget, resumes
    return budget + 1, resumes + 1


def _excerpt(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _elements_excerpt(obs: Observation, limit: int = 8) -> str:
    named = [f'{n.role} "{n.name}"' for n in obs.nodes if n.name][:limit]
    return ", ".join(named)


def _approval_anchor(reason: Reason, request: Any) -> Checkpoint | None:
    """An approval handoff has not lost its place, so it can anchor itself.

    Every other handoff happens because something went wrong, and the last
    confirmed checkpoint is the only thing that says how far the run got. An
    approval is not that: nothing failed, the page is exactly where the engine
    left it, and the person is answering a question about a click that has not
    happened yet. So the question on resume is not "where did we get to" but
    "is the browser still on the screen I asked about", and that screen's URL
    is known for free at the moment of asking.

    Without this the approval handoff is dead code against every artifact the
    recorder produces, because those carry a checkpoint only on the final step
    and the step needing approval comes before it. Refusing to resume is still
    the right answer when a person has moved the browser -- this changes which
    question is asked, not whether one is.
    """
    if reason is not Reason.APPROVAL:
        return None
    url = getattr(request, "url", "")
    return Checkpoint(kind="url_contains", value=url) if url else None


def _expected(step: Step) -> str:
    if step.target and step.target.candidates:
        primary = step.target.primary
        return f"{primary.strategy.value}={primary.value!r}"
    return step.action.value


__all__ = [
    "Replay", "replay", "ParamError", "Condition", "CONDITIONS",
    "AppProfile", "PROFILES", "DEFAULT_PROFILE",
    "bind_params", "substitute", "verify", "frame_for", "frame_text", "page_text",
    "STEP_TIMEOUT_MS", "SLOW_TIMEOUT_MS", "MAX_ATTEMPTS", "MAX_REENTRIES",
    "MAX_SESSION_RECOVERIES", "MAX_RESUMES",
]
