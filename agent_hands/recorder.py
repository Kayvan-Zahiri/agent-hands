"""Turn one successful discovery run into a reusable Capability artifact.

The discovery agent works in literals: it typed "12345" because the goal said
member 12345. Keep those literals and the artifact can only ever look up that
one member, so the real work here is parameterization.

It happens at record time on purpose. Right now we still know where a literal
came from: it appeared in the goal, so it was an input, so it is a parameter. By
replay time it is just a digit string somewhere in a URL, and guessing at that
point is how a flow ends up templating a session id.

The rule: a literal that appears in the goal and is also used by the trajectory
becomes a parameter; everything else stays literal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema import (
    BusinessRule,
    SCHEMA_VERSION,
    ActionKind,
    Capability,
    Checkpoint,
    Output,
    infer_output_type,
    Param,
    Risk,
    Step,
    Strategy,
    Target,
    TargetSet,
)


class SchemaVersionError(ValueError):
    """A stored artifact this build cannot be trusted to replay."""


# --------------------------------------------------------------------------
# what the discovery agent hands over
# --------------------------------------------------------------------------

@dataclass
class RecordedAction:
    """One thing the agent did, with the targeting the perception layer derived.

    Close to Step, but a separate type: a Step has an index and is parameterized,
    and neither is true until this module has run. Keeping them apart stops
    half-built Steps from leaking into an artifact.
    """

    action: ActionKind
    targets: TargetSet | None = None
    text: str | None = None
    url: str | None = None
    risk: Risk = Risk.SAFE
    checkpoint: Checkpoint | None = None
    extracts: str | None = None
    note: str = ""
    # What a READ step pulled off the page. Kept out of the artifact, and here
    # only so outputs can be typed and described from a real observation.
    observed: str | None = None


# --------------------------------------------------------------------------
# parameter inference
# --------------------------------------------------------------------------

# Words that never name a parameter, so the scan keeps walking left past them.
# The verbs are here because a goal reads as an instruction: "look up member
# 12345" puts a verb next to the value about as often as a noun.
_STOPWORDS = frozenset("""
a an the for of with to on in and or is are be was this that their its
please find look up get fetch show me then report check what value from
search open note enter type select set add create update read return lookup
""".split())

# Words that qualify the noun before them: "account number" is one name.
_QUALIFIERS = {
    "id": "id", "ids": "id", "number": "number", "no": "number", "num": "number",
    "code": "code", "ref": "ref", "reference": "ref",
}

# A quoted phrase is one token: "Maeve Millay" is a single value the operator
# already delimited, and splitting it on the space loses that.
_TOKEN = re.compile(
    r"\"[^\"]{1,80}\""
    r"|(?<![A-Za-z])'[^']{1,80}'"   # single quotes only where they cannot be an apostrophe
    r"|[A-Za-z0-9][A-Za-z0-9._@#/-]*"
)
_HAS_DIGIT = re.compile(r"\d")
_QUOTED = re.compile(r"^[\"'](.+)[\"']$")


@dataclass
class _Binding:
    name: str
    literal: str
    used: bool = False
    where: list[str] = field(default_factory=list)


def infer_bindings(goal: str) -> list[_Binding]:
    """Pull the input literals out of the goal and give each one a name.

    The name comes from the words in front of the value, where the operator
    already put it: "member 12345" is a member_id, "account number 22887" is an
    account_number. Naming from context instead of from a fixed vocabulary is
    what lets this work on the next tenant's wording.
    """
    tokens = list(_TOKEN.finditer(goal))
    words = [t.group(0) for t in tokens]
    bindings: list[_Binding] = []
    taken: set[str] = set()

    for i, raw in enumerate(words):
        literal = _unquote(raw)
        if not _is_value(raw, literal):
            continue
        name = _name_for(words[:i], literal)
        while name in taken:
            name = _bump(name)
        taken.add(name)
        bindings.append(_Binding(name=name, literal=literal))
    return bindings


def _is_value(raw: str, literal: str) -> bool:
    # Quoted strings are values because the operator marked them as such.
    # Anything else needs a digit to tell "12345" apart from "member".
    if _QUOTED.match(raw):
        return len(literal) >= 1
    return bool(_HAS_DIGIT.search(literal)) and len(literal) >= 2


def _name_for(before: Sequence[str], literal: str) -> str:
    nouns: list[str] = []
    qualifier: str | None = None
    for word in reversed(before):
        clean = _slug(word)
        if not clean or clean in _STOPWORDS:
            continue
        if qualifier is None and clean in _QUALIFIERS and not nouns:
            qualifier = _QUALIFIERS[clean]
            continue
        nouns.append(clean)
        break

    if not nouns:
        return "value"
    noun = nouns[0]
    if qualifier:
        return f"{noun}_{qualifier}"
    # A bare number after a noun is an id in these back-office apps; naming it
    # `member` alone would read as the member record itself.
    if literal.isdigit():
        return f"{noun}_id"
    return noun


def _bump(name: str) -> str:
    match = re.search(r"_(\d+)$", name)
    if match:
        return name[: match.start()] + f"_{int(match.group(1)) + 1}"
    return name + "_2"


def _unquote(raw: str) -> str:
    match = _QUOTED.match(raw)
    return match.group(1) if match else raw


def _slug(word: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", word.lower())


# --------------------------------------------------------------------------
# substitution
# --------------------------------------------------------------------------

def _sub(text: str | None, bindings: Iterable[_Binding], where: str) -> str | None:
    """Replace whole-token occurrences of each literal with its placeholder.

    Bounded on both sides so member 12345 does not rewrite the middle of account
    123456, and so a literal cannot corrupt an unrelated numeric id.
    """
    if not text:
        return text
    for binding in bindings:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(binding.literal)}(?![A-Za-z0-9])"
        )
        text, count = pattern.subn("{" + binding.name + "}", text)
        if count:
            binding.used = True
            binding.where.append(where)
    return text


def _sub_targets(targets: TargetSet | None, bindings: Iterable[_Binding], where: str) -> TargetSet | None:
    """Parameterize target values too, including the rejected candidates.

    A link whose accessible name is the member id has to be templated, or the
    artifact only ever finds that one member. Rejected candidates get the same
    treatment so the record of what was considered stays readable.
    """
    if targets is None:
        return None
    return TargetSet(
        candidates=[_sub_target(t, bindings, where) for t in targets.candidates],
        rejected=[_sub_target(t, bindings, f"{where}.rejected") for t in targets.rejected],
    )


def _sub_target(target: Target, bindings: Iterable[_Binding], where: str) -> Target:
    return replace(target, value=_sub(target.value, bindings, where) or target.value)


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def record(
    goal: str,
    trajectory: Sequence[RecordedAction | Step | dict[str, Any]],
    app_id: str,
    entry_url: str,
    *,
    name: str | None = None,
    app_variant: str | None = None,
    recorded_by: str = "discovery-agent",
) -> Capability:
    """Build a parameterized Capability from one successful run.

    The result is a draft: `approved` stays false, because a flow that has run
    once against one member is not evidence of anything yet.
    """
    actions = [_coerce(a) for a in trajectory]
    bindings = infer_bindings(goal)

    steps = [_to_step(i, a, bindings) for i, a in enumerate(actions)]
    entry = _sub(entry_url, bindings, "entry_url") or entry_url
    params = [
        Param(
            name=b.name,
            type="string",
            required=True,
            # Ids stay strings even when they look numeric: leading zeros are
            # real in account numbers and integer round-tripping eats them.
            description=f"Recorded from the goal; used in {', '.join(sorted(set(b.where)))}.",
            example=b.literal,
        )
        for b in bindings
        if b.used
    ]
    outputs = _outputs(actions)

    # A credential typed during the run must not survive into the file, by any
    # route: not as the text of a step, and not as the worked example on the
    # parameter that replaced it. The second route is the one that catches you
    # out -- name the password in the goal and it becomes a parameter on its own,
    # carrying the literal forward as a helpful example.
    secrets, secret_names = _hide_secrets(steps)
    params = [
        replace(p, example=None, description="Supplied per invocation. Never recorded.")
        if p.name in secret_names else p
        for p in params
    ]
    params += [
        Param(name=n, type="string", required=True,
              description="Supplied per invocation. Never recorded.")
        for n in sorted(secret_names)
        if n not in {p.name for p in params}
    ]

    # The goal is rewritten with the same placeholders so the description does
    # not carry a real member id, but only for parameters the trajectory used:
    # a literal that appeared in the prose and nowhere else is not a parameter,
    # and a placeholder with no param behind it is a bug. The copies keep this
    # pass from marking bindings as used, whatever order it runs in.
    used = [_Binding(b.name, b.literal) for b in bindings if b.used]
    description = _sub(goal.strip(), used, "description") or goal.strip()
    # The goal is prose a person wrote, and it may name the credential too.
    for literal in secrets.values():
        description = description.replace(literal, "[not recorded]")

    return Capability(
        name=name or _capability_name(goal, bindings),
        description=description,
        app_id=app_id,
        app_variant=app_variant,
        entry_url=entry,
        params=params,
        outputs=outputs,
        steps=steps,
        recorded_by=recorded_by,
        recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        approved=False,
    )


# Captions that mean "do not write this down". Matched against every candidate
# a target carries, because which one is primary depends on the page.
_SECRET_FIELD = re.compile(
    r"pass\s*(word|code)|passphrase|\bpin\b|secret|security\s*(code|answer)|\botp\b|token",
    re.IGNORECASE)


def _hide_secrets(steps: list[Step]) -> tuple[dict[str, str], set[str]]:
    """Turn anything typed into a credential field into a parameter.

    The recorder's job is to write down what happened, and what happened
    included typing a password. Writing *that* down is the one thing it must not
    do: an artifact is reviewed in a diff and committed, so a literal here is a
    credential in version control, and no amount of redaction downstream gets it
    back out again.

    It becomes a required parameter with no example, which is also the honest
    contract -- the caller supplies it per invocation, and the evidence writer
    already keeps it out of the log. Returns the names it created, mapped to the
    literals it removed, so the caller can scrub them from the prose, and the
    names of every parameter typed into such a field.
    """
    hidden: dict[str, str] = {}
    names: set[str] = set()
    for step in steps:
        if step.action is not ActionKind.TYPE or not step.text or step.target is None:
            continue
        if not any(_SECRET_FIELD.search(c.value or "") for c in step.target.candidates):
            continue
        already = _ONE_PLACEHOLDER.fullmatch(step.text)
        if already:
            names.add(already.group(1))   # the goal parameterized it already
            continue
        name = _secret_name(step.target.candidates[0].value)
        hidden[name] = step.text
        names.add(name)
        step.text = "{" + name + "}"
    return hidden, names


_ONE_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _secret_name(caption: str) -> str:
    """A parameter name from the caption of the field it was typed into."""
    match = _SECRET_FIELD.search(caption or "")
    word = re.sub(r"[^a-z0-9]+", "_", (match.group(0) if match else "secret").lower())
    return word.strip("_") or "secret"


def _to_step(index: int, action: RecordedAction, bindings: list[_Binding]) -> Step:
    where = f"step[{index}]"
    checkpoint = action.checkpoint
    if checkpoint is not None:
        # Checkpoints carry the same literals as the steps they guard, so one
        # left unparameterized asserts the recorded member on every replay.
        checkpoint = replace(
            checkpoint, value=_sub(checkpoint.value, bindings, f"{where}.checkpoint") or checkpoint.value
        )
    return Step(
        index=index,
        action=action.action,
        target=_sub_targets(action.targets, bindings, f"{where}.target"),
        text=_sub(action.text, bindings, f"{where}.text"),
        url=_sub(action.url, bindings, f"{where}.url"),
        risk=action.risk,
        checkpoint=checkpoint,
        extracts=action.extracts,
        note=action.note,
    )


def _outputs(actions: Sequence[RecordedAction]) -> list[Output]:
    seen: dict[str, Output] = {}
    for action in actions:
        if not action.extracts:
            continue
        sample = f" Example observed at record time: {action.observed}." if action.observed else ""
        seen.setdefault(
            action.extracts,
            Output(name=action.extracts, type=infer_output_type(action.observed or ""),
                   description=f"Read from the page.{sample}"),
        )
    return list(seen.values())


def _capability_name(goal: str, bindings: Sequence[_Binding]) -> str:
    literals = {b.literal for b in bindings}
    words = [
        _slug(w) for w in _TOKEN.findall(goal)
        if _unquote(w) not in literals and _slug(w) and _slug(w) not in _STOPWORDS
    ]
    return "_".join(words[:4]) or "recorded_flow"


def _coerce(item: RecordedAction | Step | dict[str, Any]) -> RecordedAction:
    """Accept whatever the discovery loop emits.

    The agent loop is a separate component, and pinning it to one class would
    make this the second place a step's shape is defined. Steps and plain dicts
    are both normalized here instead.
    """
    if isinstance(item, RecordedAction):
        return item
    if isinstance(item, Step):
        return RecordedAction(
            action=item.action, targets=item.target, text=item.text, url=item.url,
            risk=item.risk, checkpoint=item.checkpoint, extracts=item.extracts, note=item.note,
        )
    data = dict(item)
    checkpoint = data.get("checkpoint")
    return RecordedAction(
        action=ActionKind(data["action"]) if not isinstance(data.get("action"), ActionKind) else data["action"],
        targets=data.get("targets") or data.get("target"),
        text=data.get("text"),
        url=data.get("url"),
        risk=Risk(data.get("risk", "safe")) if not isinstance(data.get("risk"), Risk) else data["risk"],
        checkpoint=_checkpoint_from_json(checkpoint) if isinstance(checkpoint, dict) else checkpoint,
        extracts=data.get("extracts"),
        note=data.get("note", ""),
        observed=data.get("observed"),
    )


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def save(capability: Capability, path: Path | str) -> Path:
    """Write the artifact: sorted, indented, newline-terminated, because it gets
    reviewed in a diff and it decides what an agent is allowed to do."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(capability.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target


def load(path: Path | str) -> Capability:
    """Read an artifact back, refusing one this build cannot replay faithfully.

    The check is on the major version. A minor bump adds fields an older reader
    can ignore; a major bump changes the meaning of a field it already reads,
    and replaying that against a live banking app is what this prevents.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return capability_from_json(data)


def capability_from_json(data: dict[str, Any]) -> Capability:
    found = str(data.get("schema_version", ""))
    if not found:
        raise SchemaVersionError("artifact has no schema_version; refusing to load")
    if found.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise SchemaVersionError(
            f"artifact schema_version {found} is incompatible with {SCHEMA_VERSION}"
        )
    return Capability(
        name=data["name"],
        description=data.get("description", ""),
        app_id=data["app_id"],
        entry_url=data["entry_url"],
        params=[_param_from_json(p) for p in data.get("params", [])],
        outputs=[_output_from_json(o) for o in data.get("outputs", [])],
        business_rules=[_rule_from_json(b) for b in data.get("business_rules", [])],
        steps=[_step_from_json(s) for s in data.get("steps", [])],
        schema_version=found,
        artifact_version=int(data.get("artifact_version", 1)),
        app_variant=data.get("app_variant"),
        recorded_by=data.get("recorded_by", ""),
        recorded_at=data.get("recorded_at", ""),
        approved=bool(data.get("approved", False)),
    )


def _rule_from_json(d: dict[str, Any]) -> BusinessRule:
    """Read one business rule back.

    This function exists because `save` serializes the whole capability while
    `load` rebuilds it field by field, so any field the reader does not know
    about is dropped. The damage lands on the next load-then-save, which is what
    `approve` does: the artifact silently loses the rules that tell "no such
    member" apart from a crash, and that surfaces much later as a mysterious
    failure. Any field added to `Capability` has to be added here too, in the
    same change.
    """
    return BusinessRule(
        code=d["code"], when_text=d["when_text"],
        message=d.get("message", ""), terminal=bool(d.get("terminal", True)),
    )


def _param_from_json(d: dict[str, Any]) -> Param:
    return Param(
        name=d["name"], type=d.get("type", "string"), required=bool(d.get("required", True)),
        description=d.get("description", ""), example=d.get("example"),
    )


def _output_from_json(d: dict[str, Any]) -> Output:
    return Output(name=d["name"], type=d.get("type", "string"), description=d.get("description", ""))


def _step_from_json(d: dict[str, Any]) -> Step:
    return Step(
        index=int(d["index"]),
        action=ActionKind(d["action"]),
        target=_targetset_from_json(d.get("target")),
        text=d.get("text"),
        url=d.get("url"),
        risk=Risk(d.get("risk", "safe")),
        checkpoint=_checkpoint_from_json(d.get("checkpoint")),
        extracts=d.get("extracts"),
        note=d.get("note", ""),
    )


def _targetset_from_json(d: dict[str, Any] | None) -> TargetSet | None:
    if not d:
        return None
    return TargetSet(
        candidates=[_target_from_json(t) for t in d.get("candidates", [])],
        rejected=[_target_from_json(t) for t in d.get("rejected", [])],
    )


def _target_from_json(d: dict[str, Any]) -> Target:
    return Target(
        strategy=Strategy(d["strategy"]), value=d["value"], frame=d.get("frame"),
        # accepts the pre-rename key so artifacts recorded earlier still load
        durability=float(d.get("durability", d.get("confidence", 0.0))), note=d.get("note", ""),
    )


def _checkpoint_from_json(d: dict[str, Any] | None) -> Checkpoint | None:
    if not d:
        return None
    return Checkpoint(kind=d["kind"], value=d["value"])
