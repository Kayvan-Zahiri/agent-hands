"""Turn a sentence into a capability invocation.

The front door for somebody who would rather type "what's the balance on
103001" than fill in a form. It resolves a request to one capability and the
arguments it can find, and hands both to the same API everything else uses.

**No model runs here.** That is deliberate twice over. The project's one claim
is that the production path has no model in the decision loop, and a chat box
that quietly called one would blur exactly that line. It also keeps the front
door free and offline. The cost is real and worth stating: this understands the
phrasings written down below and nothing else. `read_intent` is the seam -- a
model that returns the same `(capability, slots)` pair drops in without
anything downstream noticing.

Kept in Python rather than in the page because this is the part with judgment
in it, and judgment needs tests. Two bugs found by writing those tests:
"hold on, check the balance" resolved to *freeze this account*, and "for Member
103001" invented a surname. Both are covered below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# What a person calls each job, in the order the more specific wins. The score
# breaks ties: an explicit noun beats a bare verb.
_TRANSFER = re.compile(r"\b(transfer|move|send)\b")
_MONEYISH = re.compile(r"\b(money|funds|cash)\b|\$|\d")
_OPEN = re.compile(r"\b(open|create|start)\b")
_ACCOUNTISH = re.compile(r"\b(share|account)\b")
_FREEZE = re.compile(r"\b(freeze|block|restrict)\b")
_PLACE_HOLD = re.compile(r"\b(place|put|apply|set)\s+(an?\s+)?hold\b")
_HOLD_ON = re.compile(r"\bhold on\b")
_BARE_HOLD = re.compile(r"\bhold\b")
_HOLDABLE = re.compile(r"\b(account|share|member|\d{6})\b")
_CHANGE = re.compile(r"\b(update|change|correct|fix|set)\b")
_CONTACT = re.compile(r"\b(email|phone|address|contact|details)\b")
_BALANCE = re.compile(r"\bbalance\b")
_HOW_MUCH = re.compile(r"\bhow much\b")
_LOOKUP = re.compile(r"\b(who is|look ?up|find|search|inquiry|details for|record for)\b")
_SIGNON = re.compile(r"\b(sign ?on|sign ?in|log ?in)\b")


def _score_hold(t: str) -> int:
    """Explicit instructions first, then the interjection veto.

    "hold on, check the balance" means wait. "put a hold on member 102777" does
    not. Freezing somebody's account because a sentence opened with a filler
    word is the worst mistake in this module, so the veto exists -- but it runs
    after the phrasings that unambiguously ask for a hold, or it eats those too.
    """
    if _FREEZE.search(t) or _PLACE_HOLD.search(t):
        return 3
    if _HOLD_ON.search(t):
        return 0
    return 3 if _BARE_HOLD.search(t) and _HOLDABLE.search(t) else 0


_INTENTS: tuple[tuple[str, Any], ...] = (
    ("transfer", lambda t: 3 if _TRANSFER.search(t) and _MONEYISH.search(t) else 0),
    ("open_share", lambda t: 3 if _OPEN.search(t) and _ACCOUNTISH.search(t) else 0),
    ("hold", _score_hold),
    ("update", lambda t: 3 if _CHANGE.search(t) and _CONTACT.search(t) else 0),
    ("balance", lambda t: 3 if _BALANCE.search(t) else 2 if _HOW_MUCH.search(t) else 0),
    ("inquiry", lambda t: 2 if _LOOKUP.search(t) else 0),
    ("signon", lambda t: 2 if _SIGNON.search(t) else 0),
)


def read_intent(text: str) -> str | None:
    """Which job the sentence is asking for, or None if nothing matched.

    None is a real answer and the caller must handle it. Guessing a capability
    for a sentence nobody understood is how a chat box ends up moving money.
    """
    t = text.lower()
    best: tuple[str, int] | None = None
    for name, score in _INTENTS:
        s = score(t)
        if s and (best is None or s > best[1]):
            best = (name, s)
    return best[0] if best else None


_SHARE = re.compile(r"\b(\d{6}-[A-Z0-9]+-\d+)\b", re.I)
_MEMBER = re.compile(r"\b(\d{6})\b")
_AMOUNT = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)|\b([\d,]+\.\d{2})\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PHONE = re.compile(r"\b(\d{3}-\d{4}|\(\d{3}\)\s?\d{3}-\d{4}|\d{3}-\d{3}-\d{4})\b")
_NAMED = re.compile(r"\b(?:named|called|last name|surname|search for)\s+\"?([A-Z][a-z]{2,})\"?")
_REASON = re.compile(r"\b(LEGAL|FRAUD|LIEN|ADMIN)\b", re.I)

# Words that follow "named"-ish markers but are not anybody's surname. Guessing
# one turns a lookup by number into a lookup by name that finds nobody.
_NOT_A_NAME = {"Member", "Account", "Share", "Balance", "Customer", "Branch", "Teller"}


def read_slots(text: str) -> dict[str, str]:
    """The values a person actually said. Everything else comes from defaults."""
    slots: dict[str, str] = {}

    shares = [m.group(1).upper() for m in _SHARE.finditer(text)]
    if shares:
        slots["share_id"] = slots["from_share"] = shares[0]
        if len(shares) > 1:
            slots["to_share"] = shares[1]

    # A member number is six digits that is not already part of a share id.
    member = _MEMBER.search(_SHARE.sub(" ", text))
    if member:
        slots["member_id"] = member.group(1)

    amount = _AMOUNT.search(text)
    if amount:
        slots["amount"] = (amount.group(1) or amount.group(2)).replace(",", "")

    email = _EMAIL.search(text)
    if email:
        slots["email"] = email.group(0)

    phone = _PHONE.search(text)
    if phone:
        slots["phone"] = phone.group(1)

    named = _NAMED.search(text)
    if named and named.group(1) not in _NOT_A_NAME:
        slots["last_name"] = named.group(1)

    reason = _REASON.search(text)
    if reason:
        slots["reason"] = reason.group(1).upper()

    return slots


@dataclass
class Understanding:
    """What one sentence resolved to. `capability` is None when nothing matched."""

    capability: str | None
    slots: dict[str, str] = field(default_factory=dict)
    text: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"capability": self.capability, "slots": self.slots, "text": self.text}


def understand(text: str, previous: Understanding | None = None) -> Understanding:
    """Resolve one turn, carrying the last intent forward when this one has none.

    "what about 100987" is a whole sentence to a person and a bare number to a
    parser. Carrying the previous intent is what makes a second question work;
    carrying it only when this turn found none is what stops it hijacking a
    turn that meant something else.
    """
    intent = read_intent(text)
    slots = read_slots(text)
    if intent is None and previous is not None and previous.capability and slots:
        intent = previous.capability
        slots = {**previous.slots, **slots}
    return Understanding(capability=intent, slots=slots, text=text)


# -- from an understood sentence to an invocation ---------------------------

# What each intent is called in a capability id. The catalog is matched on these
# rather than on a hard-coded id, so re-recording a flow under a new name does
# not break the front door.
_JOB_KEY = {
    "balance": "balance", "inquiry": "inquiry", "transfer": "transfer",
    "open_share": "open_share", "update": "update", "hold": "hold", "signon": "signon",
}

# What to say when somebody asks for something not filled in yet.
_ASK_FOR = {
    "member_id": "Which member number?",
    "share_id": "Which account? (for example 103001-MMKT-3)",
    "from_share": "Which account should the money come from?",
    "to_share": "Which account should it go to?",
    "amount": "How much?",
    "deposit": "How much is the opening deposit?",
    "reason": "What is the reason code? (LEGAL, FRAUD, LIEN or ADMIN)",
    "email": "What is the new e-mail address?",
    "phone": "What is the new phone number?",
    "address": "What is the new mailing address?",
    "value": "Which member number, or which last name?",
    "share_type": "What type of account? (for example MMKT)",
    "search_by": "Search by number or by name?",
    "notes": "Any notes to record?",
    "memo": "Any memo for the transfer?",
    "address_id": "Which address line?",
    "transfer": "How much?",
}


@dataclass
class Plan:
    """What to do about one turn.

    `action` is one of: puzzled (nothing matched), ask (a value is missing),
    confirm (it writes, so read it back first), invoke (go).
    """

    action: str
    message: str = ""
    capability: str | None = None
    args: dict[str, str] = field(default_factory=dict)
    missing: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"action": self.action, "message": self.message,
                "capability": self.capability, "args": self.args, "missing": self.missing}


def resolve(intent: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Which capability serves this intent.

    Where two artifacts do the same job, take the one carrying business rules:
    without them the application refusing you comes back as a broken
    automation, and a front door that says "something went wrong" when it means
    "you are not authorized" is worse than no front door.
    """
    key = _JOB_KEY.get(intent)
    if not key:
        return None
    best = None
    for cap in catalog:
        if key not in cap.get("id", ""):
            continue
        rules = len(cap.get("business_codes") or [])
        if best is None or rules > len(best.get("business_codes") or []):
            best = cap
    return best


# Who is signed on, rather than what is being asked for. These come from the
# session and it is right to fill them in. Nothing else is.
_SESSION = {"operator", "password", "branch"}


def _fill(cap: dict[str, Any], slots: dict[str, str],
          defaults: dict[str, str]) -> dict[str, str]:
    """Map what was said onto what this capability declares.

    An artifact's `example` fills a sign-on field and nothing else. The Service
    Desk seeds its whole form from examples, which is fine because the person
    reads the form before clicking. Here there is no form: quietly deciding
    which of somebody's accounts to look at -- or what type of account to open,
    and for how much -- because the recording happened to use one is not a
    default, it is a guess with money attached. Ask instead.
    """
    args: dict[str, str] = {}
    names = {p["name"] for p in cap.get("params", [])}
    for p in cap.get("params", []):
        n = p["name"]
        if n in slots:
            args[n] = slots[n]
        elif n in defaults:
            args[n] = defaults[n]
        elif n in _SESSION and p.get("example"):
            args[n] = str(p["example"])

    # The inquiry screen takes a field and a value rather than a member number,
    # which is how searching by last name is reached at all.
    if "search_by" in names and "value" in names:
        if slots.get("last_name"):
            args["search_by"], args["value"] = "name", slots["last_name"]
        elif slots.get("member_id"):
            args["search_by"], args["value"] = "number", slots["member_id"]

    # A transfer said as "move X from A to B" gives one account twice.
    if "from_share" in names and args.get("from_share") == args.get("to_share"):
        args.pop("to_share", None)
    return {k: v for k, v in args.items() if v != ""}


def plan(u: Understanding, catalog: list[dict[str, Any]],
         defaults: dict[str, str] | None = None,
         confirmed: bool = False) -> Plan:
    """Decide what to do about one understood sentence."""
    if u.capability is None:
        return Plan("puzzled", "I can check a balance, look up a member, move money, "
                               "open an account, freeze an account, or update contact "
                               "details. Try naming one, with a member number.")
    cap = resolve(u.capability, catalog)
    if cap is None:
        return Plan("puzzled", "I understood the request but there is no capability "
                               "recorded for it.")
    args = _fill(cap, u.slots, defaults or {})
    for p in cap.get("params", []):
        if p.get("required") and not args.get(p["name"]):
            return Plan("ask", _ASK_FOR.get(p["name"], f"What is the {p['name']}?"),
                        capability=cap["id"], args=args, missing=p["name"])
    if cap.get("irreversible") and not confirmed:
        return Plan("confirm", "", capability=cap["id"], args=args)
    return Plan("invoke", "", capability=cap["id"], args=args)


def describe(result: dict[str, Any]) -> str:
    """The answer, in words, keeping the three endings apart.

    A business outcome is the application answering, not a fault, and saying so
    is the whole reason the outcome model exists.
    """
    outcome = result.get("outcome")
    outputs = result.get("outputs") or {}
    if outcome == "success":
        if not outputs:
            return "Done. MERIDIAN accepted it."
        parts = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in outputs.items())
        return f"Done. {parts}."
    if outcome == "business_outcome":
        return (f"MERIDIAN says: {result.get('message') or 'the request was declined'}. "
                "That is its answer, not a fault, and nothing was changed.")
    return ("I could not finish that, and nothing was changed. "
            f"{result.get('message') or ''}").strip()


_YES = re.compile(r"^\s*(y|yes|yep|yeah|ok|okay|go|do it|confirm|approved?)\b", re.I)
_NO = re.compile(r"^\s*(n|no|nope|stop|cancel|never ?mind|forget it|abort)\b", re.I)


@dataclass
class Conversation:
    """One person's thread, and the little state a thread needs.

    Two things, both of which a person takes for granted and a parser does not:
    a bare reply answers the question just asked, and "yes" refers to the thing
    just read back to them.
    """

    defaults: dict[str, str] = field(default_factory=dict)
    last: Understanding | None = None
    asked: Plan | None = None       # a question waiting on a value
    proposed: Plan | None = None    # a write read back, waiting on a yes

    def say(self, text: str, catalog: list[dict[str, Any]]) -> Plan:
        if _NO.match(text):
            self.asked = self.proposed = None
            return Plan("puzzled", "Dropped it. Nothing was changed.")

        # "yes" means the thing just read back, and nothing else.
        if self.proposed is not None and _YES.match(text):
            go, self.proposed = self.proposed, None
            return Plan("invoke", capability=go.capability, args=go.args)

        # A bare value answers the question just asked. Anything with an intent
        # of its own is a change of subject and is treated as one.
        if self.asked is not None and read_intent(text) is None:
            slots = {**(self.last.slots if self.last else {}),
                     **read_slots(text)}
            if self.asked.missing and self.asked.missing not in slots:
                slots[self.asked.missing] = text.strip()
            u = Understanding(capability=self.asked.capability and
                              (self.last.capability if self.last else None),
                              slots=slots, text=text)
            if u.capability is None and self.last is not None:
                u.capability = self.last.capability
            return self._settle(u, catalog)

        return self._settle(understand(text, self.last), catalog)

    def _settle(self, u: Understanding, catalog: list[dict[str, Any]]) -> Plan:
        if u.capability:
            self.last = u
        p = plan(u, catalog, self.defaults)
        self.asked = p if p.action == "ask" else None
        self.proposed = p if p.action == "confirm" else None
        return p


# -- what the front door offers to click ------------------------------------

# Values a person can pick instead of typing, for parameters where naming one
# from the recording would be a guess. Demo data for the hosted target; the
# starters below are built from the catalog, so they follow a re-recording.
_CHIPS = {
    "member_id": ["103001", "100234", "100987"],
    "share_id": ["103001-MMKT-3", "103001-S0001"],
    "from_share": ["103001-MMKT-3"], "to_share": ["103001-S0070-7"],
    "amount": ["0.01", "25.00"], "deposit": ["5.00"],
    "reason": ["LEGAL", "FRAUD"], "share_type": ["MMKT"],
    "phone": ["555-0199"], "email": ["d.vaughan@example.test"],
    "address": ["14 Bay Street"], "search_by": ["number", "name"],
    "value": ["103001", "Vaughan"], "transfer": ["0.01"],
}

# One opening line per job, phrased the way `read_intent` actually reads it.
# Keyed on the same job words `_JOB_KEY` uses, so a capability that disappears
# from the catalog takes its starter with it.
_STARTERS = {
    "balance": "What's the balance on 103001?",
    "inquiry": "Look up the member named Vaughan",
    "transfer": "Move $0.01 from 103001-MMKT-3 to 103001-S0070-7 for 103001",
    "open_share": "Open a new share for 103001",
    "hold": "Freeze 103001-MMKT-3 for 103001, reason LEGAL",
    "update": "Change the phone for 103001 to 555-0199",
}


def starters(catalog: list[dict[str, Any]]) -> list[str]:
    """Openers to offer, for the jobs this catalog can actually do."""
    return [line for intent, line in _STARTERS.items() if resolve(intent, catalog)]


def chips(p: Plan) -> list[str]:
    """What to offer as a click, given what the front door just said."""
    if p.action == "confirm":
        return ["yes", "no"]
    if p.action == "ask" and p.missing:
        return _CHIPS.get(p.missing, [])
    return []


def state_of(c: "Conversation") -> dict[str, Any]:
    """A conversation, small enough to hand back to the browser and return.

    Kept on the client so the server holds no per-person session. There is
    nothing here worth stealing -- an intent and the values already typed --
    and nothing that can widen what a run may do: every plan is recomputed
    against the catalog on arrival.
    """
    return {
        "last": c.last.to_json() if c.last else None,
        "asked": c.asked.to_json() if c.asked else None,
        "proposed": c.proposed.to_json() if c.proposed else None,
    }


def conversation_from(state: dict[str, Any] | None,
                      defaults: dict[str, str]) -> "Conversation":
    state = state or {}
    def _plan(d: Any) -> Plan | None:
        return Plan(**d) if isinstance(d, dict) else None
    last = state.get("last")
    return Conversation(
        defaults=defaults,
        last=Understanding(**last) if isinstance(last, dict) else None,
        asked=_plan(state.get("asked")),
        proposed=_plan(state.get("proposed")),
    )
