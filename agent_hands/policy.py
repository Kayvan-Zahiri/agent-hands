"""What the automation is allowed to do, decided before it does it.

The agent is assumed to be well-meaning. The danger is that it acts on a page
that changed underneath it, or on text an attacker planted in a record so that a
model would read it as an instruction. Both end the same way, with a
plausible-looking action against the wrong thing. So the rules are structural,
checked here before dispatch, never negotiated in a prompt. A refusal that
arrives after the click has landed is only a log line.

Three gates, kept separate because they fail for different reasons and a reviewer
needs to see which one fired:

1. **Surface.** Host *and* path prefix. A capability recorded for member search
   must not become a general browser pointed at everything the session cookie can
   reach, so the allowlist narrows the origin as well as picking it. Checked
   against the live URL rather than the artifact's `entry_url`, because the
   artifact is data, and data can be stale or supplied by whoever filed the
   ticket.
2. **Action.** Which `ActionKind` values this lane permits. The useful case is a
   read-only lane: the same artifact replayed for evidence gathering with writes
   turned off, instead of a second artifact to keep in step.
3. **Risk.** Classified once, at record time, by the human who approved the
   artifact. `Risk.IRREVERSIBLE` is default-deny; see `_check_risk`.

`redact` exists because evidence outlives the run. Screenshots, page text and
URLs from a servicing screen carry account numbers, and once on disk they fall
under every control that covers the underlying data. Redaction applies on the way
to an artifact, a log or an evidence file, and *not* to the values returned to
the caller: the caller asked for the balance and is entitled to it in memory.
Nobody is entitled to our copy on disk.

The deliberate gap: this gates *what* runs, not *who* asked. Identity, per-caller
scoping and an approvals audit trail belong to whatever calls this, and are out
of scope here rather than half-built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from .schema import ActionKind, Capability, Risk, Step


class PolicyViolation(Exception):
    """Refusal to act.

    Never caught by the retry path. A policy failure will not clear on its own,
    so retrying it only breaks the rule more slowly.
    """

    def __init__(self, decision: "Decision | str") -> None:
        self.decision = decision if isinstance(decision, Decision) else Decision(False, "policy", str(decision))
        super().__init__(self.decision.reason)


class ApprovalRequired(PolicyViolation):
    """The action is legitimate but has not been signed off by anyone."""


# An alias: refusal is the same event under either name, and both read correctly
# at the call site.
PolicyRefusal = PolicyViolation


# --------------------------------------------------------------------------
# the allowlist
# --------------------------------------------------------------------------

WILDCARD = "*"


@dataclass(frozen=True)
class AppAllowance:
    """One application the automation may drive, narrowed to part of its surface.

    `host` includes the port. That is what separates the fixture from anything
    else on loopback, and in production the servicing host from the admin host on
    the same box.
    """

    host: str
    path_prefixes: tuple[str, ...] = (WILDCARD,)
    schemes: tuple[str, ...] = ("http", "https")

    def matches_host(self, netloc: str) -> bool:
        return netloc.lower() == self.host.lower()

    def matches_path(self, path: str) -> bool:
        for prefix in self.path_prefixes:
            if prefix == WILDCARD or path == prefix:
                return True
            # "/" is exact-match only. As a prefix it would allow the whole
            # origin, defeating the point of listing prefixes at all.
            if prefix != "/" and path.startswith(prefix):
                return True
        return False


@dataclass(frozen=True)
class Decision:
    """Why a request was allowed or refused, and which gate decided.

    Shaped for the replay engine, which reads `.allowed` and `.reason` off
    whatever the policy layer returns.
    """

    allowed: bool
    gate: str          # "surface" | "action" | "risk" | "artifact" | "ok"
    reason: str

    def __bool__(self) -> bool:
        return self.allowed

    def __str__(self) -> str:
        return f"{'allow' if self.allowed else 'REFUSE'} [{self.gate}] {self.reason}"

    def to_json(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "gate": self.gate, "reason": self.reason}


# --------------------------------------------------------------------------
# the policy
# --------------------------------------------------------------------------

@dataclass
class Policy:
    """One tenant's rules. Constructed by the caller, never by the model."""

    apps: tuple[AppAllowance, ...] = ()
    actions: frozenset[ActionKind] = frozenset(ActionKind)
    require_approved_artifact: bool = True
    max_steps: int = 60
    name: str = "default"

    # Set when a human is reachable, which turns a refusal into a question.
    # Without it an irreversible step fails closed. Any object with
    # `approve(capability=..., step=...) -> bool` works, such as the operator
    # console in escalation.py.
    approver: Any = None
    # Optional live-URL source, e.g. `lambda: page.url`. With it, every step is
    # surface-checked against where the browser actually is instead of where the
    # artifact says it should be. See `check_step`.
    url_provider: Callable[[], str] | None = None
    notes: list[str] = field(default_factory=list)

    # -- constructors ------------------------------------------------------

    @classmethod
    def for_host(cls, url: str, **kw: Any) -> "Policy":
        """Allow one origin, whole. A shortcut for a tenant whose surface has not
        been narrowed yet; prefer listing path prefixes."""
        return cls(apps=(AppAllowance(host=urlparse(url).netloc),), **kw)

    def read_only(self) -> "Policy":
        """The same surface with the writes removed. See the module docstring."""
        clone = Policy(**{**self.__dict__, "notes": list(self.notes)})
        clone.actions = frozenset({ActionKind.NAVIGATE, ActionKind.READ, ActionKind.WAIT_FOR})
        clone.name = f"{self.name}:read-only"
        return clone

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(app.host for app in self.apps)

    # -- individual gates --------------------------------------------------

    def _check_surface(self, url: str) -> Decision:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            # Catches "javascript:...", "file:///etc/passwd" and bare paths,
            # none of which can be checked against a host allowlist at all.
            return Decision(False, "surface", f"not an absolute http(s) URL: {url!r}")
        if not self.apps:
            return Decision(False, "surface", "policy has no allowed applications; refusing all navigation")
        for app in self.apps:
            if not app.matches_host(parsed.netloc):
                continue
            if parsed.scheme not in app.schemes:
                return Decision(False, "surface", f"scheme {parsed.scheme!r} not permitted on {app.host}")
            if not app.matches_path(parsed.path):
                return Decision(False, "surface", f"path {parsed.path!r} is outside the allowlist for {app.host}")
            return Decision(True, "surface", f"{app.host}{parsed.path}")
        return Decision(False, "surface", f"host {parsed.netloc!r} is not in the allowlist")

    def _check_action(self, action: ActionKind) -> Decision:
        if action in self.actions:
            return Decision(True, "action", f"{action.value} permitted by lane {self.name!r}")
        return Decision(False, "action", f"{action.value} is not permitted by lane {self.name!r}")

    def _check_risk(self, step: Step, capability: Capability | None, confirm: bool) -> Decision:
        """Gate the one class of action a person has to undo by hand.

        An irreversible step needs two separate facts. `capability.approved` says
        a reviewer read the recording and agreed the flow is correct, a judgment
        about the script made once, maybe months ago. `confirm` says the caller of
        *this* invocation, with today's parameters, is taking responsibility for
        the write. Neither implies the other: an agent looping over a thousand
        members has approval on the artifact and still must not post a thousand
        adjustments by accident.

        The default is refuse rather than prompt, deliberately: replay may run
        unattended, so a gate that depends on somebody being at a keyboard
        protects nothing. A configured approver turns the refusal into a question,
        and that is the only substitute for the caller's confirm.
        """
        if step.risk is not Risk.IRREVERSIBLE:
            return Decision(True, "risk", f"{step.risk.value} action needs no confirmation")
        if capability is not None and not capability.approved:
            return Decision(False, "risk",
                            f"irreversible step in unapproved capability {capability.name!r}")
        # No capability at all means this is a recording: the artifact is what
        # the session is trying to produce, so there is nothing yet to have been
        # approved. Refusing here made a write flow impossible to record, which
        # is not a safety property, just a gap. The approver still has to say
        # yes, and with none configured this refuses exactly as before.
        if capability is None and self.approver is None:
            return Decision(False, "risk",
                            "irreversible step with no capability to authorize it and nobody "
                            "watching; a recording needs an operator to allow writes")
        if confirm:
            return Decision(True, "risk", "irreversible step approved and confirmed by the caller")
        if self.approver is None:
            return Decision(False, "risk",
                            "irreversible step needs an explicit confirm, and no approver is "
                            "configured; refusing to act unattended")
        if self.approver.approve(
                capability=capability.name if capability else "<recording>", step=step):
            return Decision(True, "risk", f"irreversible step approved at runtime for step {step.index}")
        return Decision(False, "risk", f"step {step.index} was declined by the approver")

    # -- entry points ------------------------------------------------------

    def check(
        self,
        step: Step,
        url: str,
        capability: Capability | None = None,
        confirm: bool = False,
    ) -> Decision:
        """Decide one step. `url` is where the step will act, resolved already.

        For NAVIGATE that is the destination. For anything else it is the page the
        automation is on right now, so a flow that has drifted onto an unlisted
        screen stops there instead of typing into it.
        """
        for decision in (
            self._check_surface(url),
            self._check_action(step.action),
            self._check_risk(step, capability, confirm),
        ):
            if not decision.allowed:
                return decision
        return Decision(True, "ok", f"step {step.index} ({step.action.value}) permitted")

    def check_step(self, capability: Capability, step: Step, url: str | None = None) -> Decision:
        """The replay engine's entry point. Argument order matches its protocol.

        Replay passes no URL, so one is resolved in this order: the caller's
        argument, the live page via `url_provider`, the step's own destination,
        then the artifact's entry URL. Only the first two prove where the browser
        actually is, so wire up `url_provider` in production and the surface gate
        checks every step instead of only the first.
        """
        resolved = url or self._live_url() or step.url or capability.entry_url
        return self.check(step, resolved or "", capability)

    def enforce(
        self,
        step: Step,
        url: str,
        capability: Capability | None = None,
        confirm: bool = False,
    ) -> Decision:
        """`check`, but it raises, so a caller cannot forget to read the result."""
        decision = self.check(step, url, capability, confirm)
        if not decision.allowed:
            raise (ApprovalRequired if decision.gate == "risk" else PolicyViolation)(decision)
        return decision

    def check_navigation(self, url: str) -> Decision:
        decision = self._check_surface(url)
        if not decision.allowed:
            raise PolicyViolation(decision)
        return decision

    def check_capability(self, capability: Capability) -> Decision:
        """Gate the artifact itself, before any of its steps run."""
        if self.require_approved_artifact and not capability.approved:
            raise ApprovalRequired(Decision(
                False, "artifact",
                f"capability {capability.name!r} v{capability.artifact_version} is a draft; "
                "a recorded flow must be reviewed before it can be replayed"))
        if len(capability.steps) > self.max_steps:
            raise PolicyViolation(Decision(
                False, "artifact",
                f"capability has {len(capability.steps)} steps, over the {self.max_steps} limit"))
        self.check_navigation(capability.entry_url)
        return Decision(True, "artifact", f"{capability.name} v{capability.artifact_version}")

    def should_escalate(self, capability: Capability, code: str) -> bool:
        """Whether a failure is worth a person's time.

        A business outcome like "member not found" is an answer, so it never
        escalates. Neither does a policy refusal: the fix there is to change the
        policy or the artifact deliberately, not to ask an operator to wave it
        through.
        """
        never = {"member_not_found", "validation_rejected", "blocked_by_policy"}
        return code not in never

    def _live_url(self) -> str | None:
        if self.url_provider is None:
            return None
        try:
            return self.url_provider()
        except Exception:
            return None                                # a dead page is the caller's problem


ALL_ACTIONS: frozenset[ActionKind] = frozenset(ActionKind)

# The lane the fixture runs in. /admin is reachable from the nav frame and is
# left out on purpose, so a misrecorded or drifting flow gets refused instead of
# an admin screen.
FIXTURE_POLICY = Policy(
    apps=(
        AppAllowance(
            host="127.0.0.1:8899",
            path_prefixes=("/", "/nav", "/dialog", "/members/search", "/servicing/member-lookup"),
            schemes=("http",),
        ),
    ),
    actions=ALL_ACTIONS,
    name="fixture",
)


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

# Order matters: the earlier patterns consume the digits that the later, broader
# ones would mislabel. Placeholders hold no digits, so a later pass cannot match
# inside one.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # 13 to 19 digits, optionally grouped. No Luhn check: a Luhn failure only
    # means "probably not a card", and over-redacting costs a slightly harder log
    # while under-redacting writes out a real card number.
    ("PAN", re.compile(r"\b\d(?:[ -]?\d){12,18}\b")),
    ("PHONE", re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),
    # Anything sitting next to an identifier caption. This catches the fixture's
    # five digit member IDs, in page text and in "?member_id=12345".
    ("ACCOUNT", re.compile(
        r"(?i)\b(?:member|account|acct|customer|routing|iban|card)[ _-]?"
        r"(?:id|no|num|number|#)?\s*[:=#]?\s*\d(?:[ -]?\d){3,18}\b")),
    # Bare runs long enough not to be a quantity, a date or a balance.
    ("ACCOUNT", re.compile(r"\b\d{9,12}\b")),
)

_CAPTION = re.compile(r"(?i)^((?:member|account|acct|customer|routing|iban|card)[ _-]?"
                      r"(?:id|no|num|number|#)?\s*[:=#]?\s*)")

# A value under one of these keys is sensitive whatever it looks like. In a
# manifest the caption and the value sit in different places, so the text rule
# never sees them together and a bare "22887" would slip through. A field named
# `password` needs no pattern at all; the name is enough.
_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "pin", "ssn", "tax_id", "secret", "token", "api_key",
    "authorization", "cookie", "session_id", "member_id", "account_id",
    "account_number", "acct", "card_number", "routing_number",
})


def redact(text: Any) -> Any:
    """Scrub identifiers out of anything about to be persisted.

    Takes a string, returns a string. It also accepts a structure and walks it,
    because the evidence writer hands whole payloads to whatever redactor it is
    given, and a version that quietly returned a dict untouched would look like it
    was working while writing member IDs to disk.

    Applied to evidence files, artifact notes and log lines, but not to the
    outputs handed back to the caller in memory. Reading regulated data is
    allowed; leaving copies of it on disk is not.

    Errs toward over-redaction, and keeps the caption on keyed matches
    ("Member ID: [REDACTED:ACCOUNT]") so a redacted log can still be attached to a
    ticket and read by the person holding it.
    """
    if not isinstance(text, str):
        return redact_json(text)
    if not text:
        return text
    for label, pattern in _PATTERNS:
        text = pattern.sub(partial(_placeholder, label), text)
    return text


def _placeholder(label: str, match: re.Match[str]) -> str:
    if label != "ACCOUNT":
        return f"[REDACTED:{label}]"
    head = _CAPTION.match(match.group(0))
    return f"{head.group(1)}[REDACTED:ACCOUNT]" if head else "[REDACTED:ACCOUNT]"


def redact_json(value: Any) -> Any:
    """`redact` over a structure, for artifacts and evidence manifests.

    Dictionary keys are left alone, since a key is a field name and redacting it
    would wreck the shape of the document for no gain. Values under an
    identifier-shaped key are replaced whole.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS and v is not None
                else redact_json(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_json(v) for v in value]
    return value


def redacted_lines(lines: Iterable[str]) -> list[str]:
    return [redact(line) for line in lines]


# --------------------------------------------------------------------------
# risk classification, at record time
# --------------------------------------------------------------------------

def classify_risk(action: str, *, url: str = "", label: str = "") -> Risk:
    """Best-effort risk class at record time, for a human to correct.

    The real safety mechanism is a person reading the artifact before approving
    it. This only gets the common cases right up front, so the reviewer's
    attention lands on the ones it got wrong.
    """
    # The label only, for a click. Where you are is not what you pressed: every
    # control on a page whose path contains "transfer" is not a transfer, and
    # reading the URL made a menu link, a Search button and a Select link all
    # come back irreversible. An over-classified step is fail-safe but it is not
    # free -- each one stops the run and asks a person, so a flow with five of
    # them asks five times and the gate stops meaning anything.
    text = (label or "").lower()
    if action in ("read", "wait_for", "navigate"):
        return Risk.SAFE
    if action in ("type", "select"):
        return Risk.REVERSIBLE
    if action == "click":
        # Phrases rather than bare words, because the bare ones over-match: a
        # menu link reading "Open New Share" navigates and writes nothing, while
        # the button reading "Open Share" on the confirmation screen creates an
        # account. Added after a real recording: this target's most restricted
        # action, the one that needs a supervisor, is a button reading "Apply
        # Hold", and none of the original words appear in it -- so freezing
        # somebody's account classified as reversible and the gate never ran.
        # "transfer" is absent on purpose: it is a noun in navigation --
        # "Funds Transfer" is a menu link -- and the button that actually moves
        # money reads "Post Transfer", which "post" already catches.
        writing = ("submit", "save", "delete", "remove", "approve", "post",
                   "pay", "send", "confirm", "close account",
                   "apply hold", "place hold", "open share", "freeze",
                   "release hold", "override")
        if any(word in text for word in writing):
            return Risk.IRREVERSIBLE
        # A search button is a click that writes nothing. Defaulting clicks to
        # REVERSIBLE saves the approval prompt for the cases that deserve it, and
        # a reviewer who disagrees edits the artifact.
        return Risk.REVERSIBLE
    return Risk.REVERSIBLE


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def _demo() -> None:
    from .schema import Checkpoint, Strategy, Target, TargetSet

    def step(index: int, action: ActionKind, risk: Risk = Risk.SAFE) -> Step:
        return Step(index=index, action=action, risk=risk,
                    target=TargetSet([Target(Strategy.ROLE_NAME, "Search")]),
                    checkpoint=Checkpoint("text_present", "Member Detail"))

    def capability(approved: bool, name: str) -> Capability:
        return Capability(name=name, description="", app_id="meridian-core",
                          entry_url="http://127.0.0.1:8899/", params=[], outputs=[],
                          steps=[], approved=approved)

    approved, draft = capability(True, "lookup_member_balance"), capability(False, "post_adjustment")
    policy = FIXTURE_POLICY

    print("== gates ==")
    cases = [
        ("navigate to the search screen",
         policy.check(step(1, ActionKind.NAVIGATE), "http://127.0.0.1:8899/members/search?member_id=12345", approved)),
        ("navigate off-host",
         policy.check(step(1, ActionKind.NAVIGATE), "http://evil.example.com/members/search", approved)),
        ("navigate to the admin screen",
         policy.check(step(1, ActionKind.NAVIGATE), "http://127.0.0.1:8899/admin", approved)),
        ("javascript: URL",
         policy.check(step(1, ActionKind.NAVIGATE), "javascript:alert(1)", approved)),
        ("type into the form, read-only lane",
         policy.read_only().check(step(2, ActionKind.TYPE), "http://127.0.0.1:8899/members/search", approved)),
        ("irreversible, capability not approved",
         policy.check(step(3, ActionKind.CLICK, Risk.IRREVERSIBLE), "http://127.0.0.1:8899/members/search", draft, True)),
        ("irreversible, approved, nobody confirmed",
         policy.check(step(3, ActionKind.CLICK, Risk.IRREVERSIBLE), "http://127.0.0.1:8899/members/search", approved)),
        ("irreversible, approved and confirmed",
         policy.check(step(3, ActionKind.CLICK, Risk.IRREVERSIBLE), "http://127.0.0.1:8899/members/search", approved, True)),
    ]
    for label, decision in cases:
        print(f"  {label:44s} -> {decision}")

    print("\n== an approver turns the refusal into a question ==")

    class YesApprover:
        def approve(self, *, capability: str, step: Step) -> bool:
            print(f"  operator asked about {capability} step {step.index}: yes")
            return True

    attended = Policy(apps=policy.apps, name="fixture+operator", approver=YesApprover())
    print("  ", attended.check(step(3, ActionKind.CLICK, Risk.IRREVERSIBLE),
                               "http://127.0.0.1:8899/members/search", approved))

    print("\n== enforce raises before the action runs ==")
    for url in ("http://127.0.0.1:8899/admin",):
        try:
            policy.enforce(step(4, ActionKind.CLICK), url, approved)
        except PolicyViolation as exc:
            print(f"  {type(exc).__name__}({exc.decision})")

    print("\n== replay's entry point, bound to a live page ==")
    live = Policy(apps=policy.apps, name="fixture+live", url_provider=lambda: "http://127.0.0.1:8899/admin")
    print("  ", live.check_step(approved, step(5, ActionKind.CLICK)))

    sample = (
        "Member ID: 12345 | Name Dolores Abernathy | Savings Balance 4812.55\n"
        "url=http://127.0.0.1:8899/members/search?member_id=30021\n"
        "SSN 078-05-1120, card 4111 1111 1111 1111, routing 021000021\n"
        "contact dolores@example.com, reference 0x8007007E, 3 accounts"
    )
    print("\n== redaction ==")
    for line in redact(sample).splitlines():
        print(f"  {line}")
    print("\n  json:", redact_json({"outputs": {"savings": "4812.55"}, "member_id": "22887",
                                    "note": "emailed bernard.lowe@example.com"}))


if __name__ == "__main__":
    _demo()
