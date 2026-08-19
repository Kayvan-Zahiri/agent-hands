"""The capability artifact, and the contract replay returns.

This is the center of the system. A capability is what an agent calls; the
artifact is its serialized definition. The recorder, the replay engine, and the
escalation path are all arranged around these types.

Two decisions shape the rest:

1. A target is a ranked list of strategies rather than one selector. Recording
   captures every way the element could have been identified, ordered by how
   likely each is to survive, and replay walks the list until one works. That is
   why a recording still works next month.

2. A result is a tagged union of three outcomes rather than an exception. "No
   such member" is the answer the caller asked for, a stale session is worth a
   retry, and a missing control is a bug. Collapsing those into success or
   exception is what makes automation unusable in production.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# targeting
# --------------------------------------------------------------------------

class Strategy(str, enum.Enum):
    """How an element can be identified, best first.

    The order is the fallback order. Role plus accessible name is what a screen
    reader uses: it comes from what the control is rather than how it was built,
    so it survives markup changes and works on native desktop surfaces too. A
    DOM path survives nothing. Coordinates survive even less, and are recorded
    only so a human reading the artifact can see the element was found no other
    way.
    """

    ROLE_NAME = "role_name"                # button "Search"
    ROLE_NAME_IN_REGION = "role_name_in_region"   # ...inside the "Members" frame
    LABELLED_FIELD = "labelled_field"      # the input whose label is "Member ID"
    NTH_OF_ROLE = "nth_of_role"            # 3rd row of the results table
    DOM_PATH = "dom_path"                  # last resort before coordinates
    POINT = "point"                        # screenshot coordinates, flagged brittle


@dataclass(frozen=True)
class Target:
    """One way to find one control, plus why it is expected to hold.

    `durability` is an ordinal rank. It says how long this way of finding the
    control should keep working as the application changes, and it is a constant
    per strategy rather than anything measured on the page. Nothing compares it
    to a threshold, so only the ordering matters. It is stored so `show` can
    print it and a reviewer can see that a run which fell to 0.4 is one release
    away from breaking.
    """

    strategy: Strategy
    value: str
    frame: str | None = None
    durability: float = 0.0
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "value": self.value,
            "frame": self.frame,
            "durability": round(self.durability, 3),
            "note": self.note,
        }


@dataclass
class TargetSet:
    """Every way the recorder could identify one element, best first.

    `rejected` is kept on purpose. A reviewer asking "why is this matching by
    position?" can see the control had no accessible name, which is a fact about
    the application rather than a shortcut the recorder took.
    """

    candidates: list[Target]
    rejected: list[Target] = field(default_factory=list)

    @property
    def primary(self) -> Target:
        return self.candidates[0]

    def to_json(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_json() for c in self.candidates],
            "rejected": [r.to_json() for r in self.rejected],
        }


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

class ActionKind(str, enum.Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    READ = "read"
    WAIT_FOR = "wait_for"


class Risk(str, enum.Enum):
    """Whether an action can be undone.

    SAFE is read-only. REVERSIBLE changes UI state but no records. IRREVERSIBLE
    writes something a person would have to undo by hand, and is the class the
    policy layer gates.
    """

    SAFE = "safe"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


@dataclass
class Step:
    index: int
    action: ActionKind
    target: TargetSet | None = None
    # `{member_id}` is substituted from the invocation parameters at replay.
    text: str | None = None
    url: str | None = None
    risk: Risk = Risk.SAFE
    # what must be true after this step; replay fails here rather than three
    # steps later with an unrelated error.
    checkpoint: Checkpoint | None = None
    extracts: str | None = None      # output name this step's READ populates
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action.value,
            "target": self.target.to_json() if self.target else None,
            "text": self.text,
            "url": self.url,
            "risk": self.risk.value,
            "checkpoint": self.checkpoint.to_json() if self.checkpoint else None,
            "extracts": self.extracts,
            "note": self.note,
        }


@dataclass(frozen=True)
class Checkpoint:
    """An assertion that the step actually arrived somewhere.

    A click can land and change nothing. Without a checkpoint, replay reports
    success for a flow that did nothing at all, which is worse than failing.
    """

    kind: Literal["url_contains", "text_present", "element_present", "text_absent"]
    value: str

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}


# --------------------------------------------------------------------------
# parameters and outputs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BusinessRule:
    """A screen that carries an answer rather than a fault.

    "No member found" is what a lookup that ran correctly returns. The rule
    lives in the artifact instead of the replay engine because which screens
    mean what is a fact about one application, and a shared engine that
    hard-codes one tenant's error strings is wrong for the next tenant.

    `terminal` separates an answer from a condition. A not-found result ends the
    run successfully. A validation message means the input was wrong and the
    caller may want to retry with different parameters, so it is reported the
    same way but flagged.
    """

    code: str                  # "member_not_found"
    when_text: str             # substring that identifies the screen
    message: str = ""          # what to tell the caller
    terminal: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code, "when_text": self.when_text,
            "message": self.message, "terminal": self.terminal,
        }


@dataclass(frozen=True)
class Param:
    name: str
    type: Literal["string", "integer", "number", "boolean"]
    required: bool = True
    description: str = ""
    example: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "type": self.type, "required": self.required,
            "description": self.description, "example": self.example,
        }


@dataclass(frozen=True)
class Output:
    name: str
    type: Literal["string", "integer", "number", "boolean"]
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "description": self.description}


# --------------------------------------------------------------------------
# the artifact
# --------------------------------------------------------------------------

@dataclass
class Capability:
    """A recorded flow an agent can invoke by name.

    `app_id` and `app_variant` cover the multi-tenant case: many institutions
    run the same vendor product with different branding and versions. The
    artifact is keyed on the product, and a variant can override individual
    targets instead of forcing a re-record per tenant.
    """

    name: str
    description: str
    app_id: str
    entry_url: str
    params: list[Param]
    outputs: list[Output]
    steps: list[Step]
    # Screens that are answers rather than faults. Empty means every unexpected
    # screen is a failure, which is the safe default for a freshly recorded flow.
    business_rules: list[BusinessRule] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    artifact_version: int = 1
    app_variant: str | None = None
    recorded_by: str = ""
    recorded_at: str = ""
    # replay is refused unless approved; a freshly recorded flow is a draft.
    approved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_version": self.artifact_version,
            "name": self.name,
            "description": self.description,
            "app_id": self.app_id,
            "app_variant": self.app_variant,
            "entry_url": self.entry_url,
            "approved": self.approved,
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at,
            "params": [p.to_json() for p in self.params],
            "outputs": [o.to_json() for o in self.outputs],
            "business_rules": [b.to_json() for b in self.business_rules],
            "steps": [s.to_json() for s in self.steps],
        }


# --------------------------------------------------------------------------
# the result contract
# --------------------------------------------------------------------------

class Outcome(str, enum.Enum):
    """The three-way split.

    SUCCESS and BUSINESS_OUTCOME both mean the automation worked; they differ in
    what the caller does next. Folding either into FAILURE is the mistake this
    type prevents. "No such member" is a real answer, and reporting it as a
    crash makes the capability useless for the case it was built for.
    """

    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


@dataclass
class ReplayResult:
    outcome: Outcome
    capability: str
    outputs: dict[str, Any] = field(default_factory=dict)
    # set for BUSINESS_OUTCOME, e.g. "member_not_found"
    business_code: str | None = None
    message: str = ""
    # set for FAILURE, enough to debug without re-running
    failed_step: int | None = None
    expected: str | None = None
    observed: str | None = None
    recovered: list[str] = field(default_factory=list)
    evidence_dir: str | None = None
    escalated: bool = False

    @property
    def ok(self) -> bool:
        """True when the automation did its job, whatever the business answer."""
        return self.outcome in (Outcome.SUCCESS, Outcome.BUSINESS_OUTCOME)

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "capability": self.capability,
            "outputs": self.outputs,
            "business_code": self.business_code,
            "message": self.message,
            "failed_step": self.failed_step,
            "expected": self.expected,
            "observed": self.observed,
            "recovered": self.recovered,
            "evidence_dir": self.evidence_dir,
            "escalated": self.escalated,
        }
