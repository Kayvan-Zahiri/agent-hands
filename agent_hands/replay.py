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
waits that return the instant the page reaches a state we recognise, which is
exactly what lets a slow application be a recovery rather than a failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    ActionKind, BusinessRule, Capability, Checkpoint, Outcome, ReplayResult, Step, Target,
)

# A step gets a short budget first; anything slower is treated as a symptom
# rather than a verdict, and the budget is widened once. Two numbers rather than
# a per-step tunable, because a replay that needs per-step timeouts to pass is a
# replay that is really waiting on a sleep.
STEP_TIMEOUT_MS = 2_000
SLOW_TIMEOUT_MS = 12_000

# Per-step attempt ceiling. Low on purpose: a step needing four tries is a step
# whose targeting is wrong, and retrying harder hides that from the one person
# who could fix it.
MAX_ATTEMPTS = 3

# Re-entering the application is the recovery of last resort, and it is bounded.
# An interstitial that reappears after every dismissal is not an interstitial, it
# is a wall, and the caller needs to hear that rather than watch us loop.
MAX_REENTRIES = 3
MAX_SESSION_RECOVERIES = 1
# Dismissals of the same interstitial on the same step before it counts as a
# wall rather than a notice.
MAX_DISMISSALS = 2


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
    """The frame a step's assertions apply to.

    Framesets are why this exists, and why nothing here asserts against the whole
    page. In this class of application the nav frame repeats most of the words
    the content frame uses, so "is 'Member Search' on the page" is also true on
    the access-denied screen. Scoping the question to one frame is the difference
    between a checkpoint and a coincidence.
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
    try:
        return frame.inner_text("body")
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
        # The frame's URL, not the page's: for a frameset the top document stays
        # at "/" for the whole session and would match nothing useful.
        frame.wait_for_url(lambda url: value in url, timeout=timeout_ms)
    elif kind == "text_present":
        frame.get_by_text(value).first.wait_for(state="visible", timeout=timeout_ms)
    elif kind == "text_absent":
        frame.get_by_text(value).first.wait_for(state="detached", timeout=timeout_ms)
    elif kind == "element_present":
        # Matched on accessible name, so a recording can assert "the Search
        # button is here" without pinning to markup that changes per render.
        if not _named_control(frame, value):
            raise PlaywrightTimeout(f"no control named {value!r}")
    else:
        raise PlaywrightTimeout(f"unknown checkpoint kind {kind!r}")


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
    """Did the step actually arrive? Returns the verdict and what was seen.

    The observed value comes back even on success, because that is what the
    evidence file records, and a passing run's evidence is the thing you compare
    against when a later one fails. Exposed rather than private so the escalation
    path re-establishes position using this definition of a checkpoint instead of
    forming a second opinion about what one means.
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
# conditions the engine recognises on any application
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Condition:
    """A screen that is about the session or the server, not about the request.

    These live in the engine rather than in the artifact because they are true of
    the class of application, not of one tenant's business rules -- which is the
    opposite of `BusinessRule`, and the line between them is the whole point.
    `kind` is the verdict and `tactic` is the response, kept apart so a reviewer
    can change what we do about a condition without re-deciding what it is.
    """

    code: str
    when_text: str          # lowercase; matched as a substring of the frame's text
    kind: Literal["recoverable", "failure"]
    tactic: Literal["dismiss", "reenter", "stop"] = "stop"
    expected: str = ""


# Order is priority. The session check runs first because a re-login screen can
# still carry the previous screen's wording, and reading that as an answer would
# report "no such member" for a member that exists.
CONDITIONS: tuple[Condition, ...] = (
    Condition("session_expired", "session has expired", "recoverable", "reenter"),
    Condition("interstitial", "scheduled maintenance", "recoverable", "dismiss"),
    Condition("permission_denied", "you do not have permission", "failure", "stop",
              "the record this flow was recorded against"),
    Condition("app_error", "unexpected error occurred", "failure", "stop",
              "the record this flow was recorded against"),
)

# Text on an interstitial's dismissal control, in preference order. A closed
# vocabulary, not inference: the words a legacy app puts on an acknowledge
# control are few, and guessing at replay time is what this module refuses to do.
_DISMISS = ("Continue", "OK", "Acknowledge", "Close", "Proceed")


def _condition(text: str) -> Condition | None:
    low = text.lower()
    for rule in CONDITIONS:
        if rule.when_text in low:
            return rule
    return None


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
            continue
        # `bool` subclasses `int` in Python, so an unguarded integer check would
        # accept True as an account number. Booleans are settled first.
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
    ) -> None:
        self.page = page
        self.policy = policy
        self.evidence = evidence
        self.escalator = escalator
        self._last_checkpoint: Checkpoint | None = None
        self._recovered: list[str] = []
        self._reentries = 0
        self._session_recoveries = 0
        # Per step, because an interstitial that appears once per screen is
        # normal and one that appears twice on the same screen is not.
        self._dismissals: dict[int, int] = {}

    # -- public ------------------------------------------------------------

    def run(self, cap: Capability, params: dict[str, Any] | None = None) -> ReplayResult:
        """Replay `cap`. Returns an outcome; raises only for a refusal to start.

        Parameters are bound and substituted into every step before the browser
        is touched, so a malformed call cannot leave a half-finished flow behind.
        """
        bound = bind_params(cap, dict(params or {}))
        entry = substitute(cap.entry_url, bound, "entry_url") or cap.entry_url
        text = {s.index: substitute(s.text, bound, f"step {s.index} text") for s in cap.steps}
        urls = {s.index: substitute(s.url, bound, f"step {s.index} url") for s in cap.steps}

        if self.policy is not None:
            try:
                self.policy.check_capability(cap)
            except PolicyViolation as exc:
                # Returned rather than raised. The caller is an agent, and a
                # refusal is information it has to act on -- an exception
                # crossing this boundary is the kind of thing a caller swallows,
                # and "the automation declined" then looks like "the automation
                # broke". ParamError above still raises: that one is the caller's
                # own bug, not an outcome of the attempt.
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
        # The surface gate asks "may we act here", so a navigation is judged on
        # where it is going and everything else on where the browser already is.
        # Handing it the current page for a NAVIGATE would check the screen we
        # are leaving, which is exactly the check that is not worth making.
        where = (urls[step.index] or entry) if step.action is ActionKind.NAVIGATE else self._url()
        self._gate(cap, step, where)

        recorder = self.evidence.step(step) if self.evidence else _NullStep()
        with recorder as record:
            record.risk = step.risk.value
            for attempt in range(1, MAX_ATTEMPTS + 1):
                last = attempt == MAX_ATTEMPTS
                # Before re-acting, ask whether the recovery already achieved
                # what this step was for. Dismissing an interstitial commonly
                # lands on the destination, and repeating the action would walk
                # straight back into it. On an IRREVERSIBLE step it would be
                # worse than a loop: re-clicking a submit that already went
                # through is a duplicate write, which no amount of retrying can
                # undo.
                if attempt > 1 and step.checkpoint is not None and self._already_there(step):
                    record.checkpoint_ok = True
                    record.extra["satisfied_by_recovery"] = True
                    self._last_checkpoint = step.checkpoint
                    self._event("satisfied_by_recovery", index=step.index,
                                checkpoint=step.checkpoint.to_json())
                    return None
                try:
                    self._act(step, entry, text, urls, outputs, record)
                except TargetResolutionError as exc:
                    record.attempted = [_as_target(a) for a in exc.attempts]
                    if not last and self._reobserve(step):
                        continue
                    if self._unresolved(cap, step, exc):
                        continue
                except (PlaywrightTimeout, PlaywrightError) as exc:
                    # The control resolved but would not accept the action: it
                    # was covered, disabled, or detached mid-flight. Worth one
                    # more look, because that is what a stale handle looks like.
                    if not last and self._reobserve(step):
                        continue
                    raise _Abort(f"{step.action.value} could not be carried out",
                                 _expected(step), _excerpt(str(exc))) from exc
                verdict = self._read(cap, step, record, outputs, last)
                if verdict is _RETRY:
                    continue
                return verdict          # ReplayResult, _Reenter, or None to go on
        return None

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
        # An approval gap is answerable by a person, so it becomes a handoff. Any
        # other refusal is structural: the answer is to change the policy or the
        # artifact, deliberately, not to have an operator wave it through at 3am.
        if decision.gate == "risk" and self.escalator is not None:
            if self._handoff(cap, step, Reason.APPROVAL, decision.reason):
                return
            raise _Abort("declined", f"approval for step {step.index}", decision.reason)
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
            needles = [c.when_text for c in CONDITIONS]
            needles += [b.when_text for b in cap.business_rules]
            if step.checkpoint.kind == "text_present":
                needles.append(step.checkpoint.value)
            try:
                self._patiently(step.index, "the page",
                                lambda ms: _wait_for_any_text(frame, needles, ms))
            except PlaywrightTimeout:
                pass      # nothing recognised arrived; the checkpoint is the verdict

        # A step with no checkpoint is the recording saying there is nothing to
        # arrive at -- typing into a field, reading a cell. Waiting for a state
        # nobody expects would spend the whole budget on every such step and
        # find nothing, so the page is classified as it stands. A condition that
        # is genuinely on screen is still caught; one that has not rendered yet
        # is caught by the next step, which does have something to wait for.
        text = frame_text(frame)

        condition = _condition(text)
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

        # Deliberately not retried. The action has already happened, and on a
        # legacy app it has usually navigated, so re-running the step would
        # either re-submit a form or fail to find a control that is no longer
        # there -- and then report *that* as the fault. The patient wait above
        # already gave the page its second chance; this is the verdict.
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
    ) -> ReplayResult | _Reenter:
        if condition.kind == "failure":
            # Not retried. Retrying a permission refusal or an application error
            # just reaches the same screen more slowly, and the fix is a person's.
            self._capture(step, condition.code)
            self._handoff(cap, step, Reason.UNEXPECTED,
                          f"{condition.code.replace('_', ' ')} during step {step.index}",
                          expected=condition.expected, observed=_excerpt(frame_text(frame)))
            raise _Abort(condition.code, condition.expected or _expected(step),
                         _excerpt(frame_text(frame)))

        if condition.tactic == "dismiss":
            if self._dismissals.get(step.index, 0) >= MAX_DISMISSALS:
                # Dismissed, and it came straight back. That is not an
                # interstitial, it is a wall. Saying so here matters: without
                # this the attempt loop runs out and the run blames whatever
                # unrelated thing the next attempt tripped over, which sends
                # someone to fix a control that was never broken.
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
            # Where dismissing leaves you decides what to do next. If the notice
            # was covering the screen the step was heading for, the step is
            # already done and repeating the action would be a second write for
            # nothing. If it bounced back to the start, then anything typed
            # before the interruption is gone with it, and only re-entering the
            # flow puts it back -- retrying the step alone would submit a form
            # that is now empty and blame the checkpoint for the result.
            if self._already_there(step):
                return _RETRY
            return _Reenter(condition.code, "dismissed an interstitial")

        # Re-entry means loading the entry URL again. This system never handles
        # credentials, so if the application still wants a login after that, it
        # is a person's decision rather than another retry.
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
        self, step: Step, entry: str, text: dict[int, str | None],
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
        # The losing strategies are the early warning: a flow that has quietly
        # slipped from role+name down to a DOM path still passes, and is one
        # release away from not passing. Only the evidence shows the slide.
        record.resolved_by(resolution.winner,
                           after=[_as_target(a) for a in resolution.attempts[:resolution.rank]])
        if resolution.degraded:
            record.extra["degraded_to_rank"] = resolution.rank
        locator = resolution.locator
        value = text[step.index] or ""

        # Interactions get the long budget in one go rather than the short-then-
        # long retry used for page states. Playwright's own actionability wait is
        # already condition-based, and a click is the one thing that must never
        # be issued twice: whether it arrived is the checkpoint's question, not
        # the timeout's.
        if step.action is ActionKind.CLICK:
            locator.click(timeout=SLOW_TIMEOUT_MS)
        elif step.action is ActionKind.TYPE:
            record.text = value
            locator.fill(value, timeout=SLOW_TIMEOUT_MS)
        elif step.action is ActionKind.SELECT:
            locator.select_option(value, timeout=SLOW_TIMEOUT_MS)
        elif step.action is ActionKind.READ:
            read = (locator.inner_text(timeout=SLOW_TIMEOUT_MS) or "").strip()
            if step.extracts:
                outputs[step.extracts] = read
            record.extra["read"] = read

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
        """One cheap re-attempt for a control that was not there a moment ago.

        No sleep and no tactic, because there is nothing recognised to respond
        to: the page is simply re-read and the step re-resolved against whatever
        is current. Bounded by MAX_ATTEMPTS, and it does not pretend to know why
        the control was missing.

        Logged as an event rather than a recovery. `recovered` means a named
        condition was recognised and handled; putting a bare re-attempt in there
        would let a run that failed three times report three recoveries.
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
        for wanted in _DISMISS:
            for role in ("link", "button"):
                try:
                    control = frame.get_by_role(role, name=wanted, exact=True)
                    if not control.count():
                        continue
                    control.first.click(timeout=SLOW_TIMEOUT_MS)
                except (PlaywrightTimeout, PlaywrightError):
                    continue
                # Re-resolved, because acknowledging a notice usually navigates
                # and the frame that held it may be a different document now.
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
        # The name is underscored so that a caller may pass a field literally
        # called `kind` -- which the condition events do -- without colliding
        # with this parameter. Cheap, and it removes a whole class of call-site
        # bug that only shows up on the error path.
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
            options=["fix it in the open browser, then resume",
                     "skip this step", "abort", "mark completed by hand"],
        )
        if expected:
            request.expected = expected
        record = self.escalator.escalate(request, self.page)
        if record.decision is not OperatorDecision.RESUME:
            return False
        try:
            self.escalator.verify_resume(self.page, self._last_checkpoint, verify)
        except ResumeRefused as exc:
            # The person moved the session somewhere the run cannot continue
            # from. Refusing is the entire point of re-verifying.
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
            message="flow completed", recovered=list(self._recovered),
        ))

    def _business(self, cap: Capability, rule: BusinessRule, outputs: dict) -> ReplayResult:
        return self._finish(ReplayResult(
            outcome=Outcome.BUSINESS_OUTCOME, capability=cap.name, outputs=outputs,
            business_code=rule.code, message=rule.message or rule.code,
            recovered=list(self._recovered),
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
            message=message or "replay stopped", recovered=list(self._recovered),
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

# Sentinel for "this step is worth another attempt", distinct from None, which
# means the step is done and the run moves on.
_RETRY = object()


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

    def __exit__(self, *exc: Any) -> bool:
        return False


def _as_target(attempt: Any) -> Target:
    """A failed Attempt, in the shape the evidence writer records.

    Confidence is zeroed rather than carried over: this is a strategy that did
    not work, and leaving its recorded confidence on it would read, in the
    evidence file, as though it had.
    """
    return Target(
        strategy=attempt.strategy, value=attempt.value, frame=attempt.frame,
        confidence=0.0,
        note=attempt.reason or (
            f"matched {attempt.matched}" if attempt.matched is not None else "no match"
        ),
    )


def _excerpt(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _elements_excerpt(obs: Observation, limit: int = 8) -> str:
    named = [f'{n.role} "{n.name}"' for n in obs.nodes if n.name][:limit]
    return ", ".join(named)


def _expected(step: Step) -> str:
    if step.target and step.target.candidates:
        primary = step.target.primary
        return f"{primary.strategy.value}={primary.value!r}"
    return step.action.value


__all__ = [
    "Replay", "replay", "ParamError", "Condition", "CONDITIONS",
    "bind_params", "substitute", "verify", "frame_for", "frame_text", "page_text",
    "STEP_TIMEOUT_MS", "SLOW_TIMEOUT_MS", "MAX_ATTEMPTS", "MAX_REENTRIES",
    "MAX_SESSION_RECOVERIES",
]
