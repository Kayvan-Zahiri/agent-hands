"""Author the MERIDIAN CORE capability set, deriving targets from the live app.

Not a recording. `discover.py` needs an API key this machine does not have, and
it has no `select` tool, which four of the seven flows need. So the trajectory is
written by hand here -- but every *target* still comes from `perception.derive_targets`
against the real screen, which is the same code the recorder uses. What is
hand-authored is which control to touch and in what order, not how to find it.

`recorded_by` on each artifact says so. Replay cannot tell the difference and
should not: the artifact is the contract, and discovery is one of two ways to
fill it in.

    PYTHONPATH=. .venv/bin/python tools/build_meridian.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from agent_hands.perception import Observation, derive_targets, observe
from agent_hands.recorder import save
from agent_hands.schema import (
    ActionKind, BusinessRule, Capability, Checkpoint, Output, Param, Risk, Step, Strategy,
    Target, TargetSet,
)

HOST = "https://web-sample.interface-hiring.com"
OUT = Path(__file__).resolve().parent.parent / "capabilities" / "meridian"
AUTHOR = "hand-authored trajectory; targets derived from the live app by perception.derive_targets"

# The demo member. 103001 rather than 100234 because other people are hammering
# the low-numbered ones, and this is the member seeded with a share on HOLD.
DEMO_MEMBER = "103001"

# Screens that are an answer rather than a fault. Read off the live host.
RULES = [
    BusinessRule("member_not_found", "RECORD NOT FOUND",
                 "no member exists with that identifier"),
    BusinessRule("no_search_match", "No member records matched",
                 "the search returned nothing"),
    BusinessRule("insufficient_funds", "INSUFFICIENT FUNDS",
                 "the from-share does not hold that amount"),
    BusinessRule("transaction_rejected", "TRANSACTION REJECTED",
                 "the application refused the transaction as entered"),
    BusinessRule("supervisor_required", "SUPERVISOR OVERRIDE REQUIRED",
                 "this function needs a supervisor to sign on", terminal=False),
]

SIGNON_PARAMS = [
    Param("operator", "string", True, "operator id", "teller1"),
    # Never stored in the artifact. Supplied per invocation, and `policy.redact`
    # already strips keys named password out of the evidence.
    Param("password", "string", True, "operator password"),
    Param("branch", "string", True, "branch code", "MAIN-001"),
]


# --------------------------------------------------------------------------
# deriving targets from a live screen
# --------------------------------------------------------------------------

def _captioned(pg) -> tuple[Observation, dict[str, TargetSet]]:
    """Every field on the current screen, keyed by the caption a person reads.

    The key is whatever `derive_targets` decided the caption was, so a screen
    whose fields cannot be addressed by caption shows up here as a missing key
    rather than as a target that quietly counts controls.
    """
    obs = observe(pg)
    found: dict[str, TargetSet] = {}
    for node in obs.nodes:
        if node.role not in ("textbox", "searchbox", "combobox"):
            continue
        frame = obs.frame_for(node)
        if frame is None:
            continue
        targets = derive_targets(node, frame)
        if targets.primary.strategy is Strategy.LABELLED_FIELD:
            found[targets.primary.value] = targets
    return obs, found


def _control(pg, role: str, name: str) -> TargetSet:
    obs = observe(pg)
    node = next(n for n in obs.nodes if n.role == role and n.name == name)
    frame = obs.frame_for(node)
    assert frame is not None
    return derive_targets(node, frame)


def _row_cell(column: int) -> TargetSet:
    """The cell in the column of the row whose first cell is `{share_id}`.

    Hand-written, because nothing derives this. A share's balance has no caption
    of its own -- the caption is the column header, one row up and shared with
    every other share -- so the only stable landmark is the share id in the same
    row. `derive_targets` would offer `cell "$25.00"`, which is the answer we are
    trying to read, so it matches only the member it was recorded against and
    then falls back to counting cells in a table that grows as shares are opened.

    This is the label-relative read the report lists as the first thing missing.
    Expressed here per-artifact because the engine cannot express it yet.
    """
    xpath = f'xpath=//tr[td[1][normalize-space(.)="{{share_id}}"]]/td[{column}]'
    return TargetSet(candidates=[Target(
        strategy=Strategy.DOM_PATH, value=xpath, frame=None, durability=0.6,
        note="the cell in the row whose first cell is the share id")])


# --------------------------------------------------------------------------
# the shared prologue
# --------------------------------------------------------------------------

def _signon_steps(pg) -> list[Step]:
    """Sign on, as the first five steps of every capability.

    Folded in rather than left as a separate session because the engine has no
    concept of a session: one invocation is one browser. It costs a few seconds
    and it means a caller never has to sequence two capabilities to get one
    answer.
    """
    pg.goto(HOST + "/signon", wait_until="load")
    _, fields = _captioned(pg)
    missing = {"Operator ID:", "Password:", "Branch:"} - set(fields)
    if missing:
        raise SystemExit(f"sign-on fields not addressable by caption: {sorted(missing)}\n"
                         f"got: {sorted(fields)}")
    return [
        Step(0, ActionKind.NAVIGATE, url=HOST + "/signon", risk=Risk.SAFE,
             checkpoint=Checkpoint("text_present", "NOT SIGNED ON")),
        Step(1, ActionKind.TYPE, target=fields["Operator ID:"], text="{operator}",
             risk=Risk.REVERSIBLE),
        Step(2, ActionKind.TYPE, target=fields["Password:"], text="{password}",
             risk=Risk.REVERSIBLE),
        Step(3, ActionKind.SELECT, target=fields["Branch:"], text="{branch}",
             risk=Risk.REVERSIBLE),
        Step(4, ActionKind.CLICK, target=_control(pg, "button", "Sign On"),
             risk=Risk.REVERSIBLE,
             checkpoint=Checkpoint("text_present", "MAIN MENU")),
    ]


def _capability(name: str, description: str, steps: list[Step],
                params: list[Param], outputs: list[Output]) -> Capability:
    return Capability(
        name=name, description=description, app_id="meridian-core",
        entry_url=HOST + "/signon", params=SIGNON_PARAMS + params, outputs=outputs,
        business_rules=RULES, steps=steps, recorded_by=AUTHOR, approved=True,
    )


def _labelled_cell(caption: str) -> TargetSet:
    """The cell next to the cell that reads `caption`.

    The same idea `LABELLED_FIELD` uses for form fields, applied to a value. The
    engine only knows how to do this for things you can type into, which is the
    gap the report names first; until it does, an artifact can carry the xpath.
    """
    return TargetSet(candidates=[Target(
        strategy=Strategy.DOM_PATH,
        value=f'xpath=//td[normalize-space(.)="{caption}"]/following-sibling::td[1]',
        frame=None, durability=0.6, note=f"the cell to the right of {caption!r}")])


# --------------------------------------------------------------------------
# the seven
# --------------------------------------------------------------------------

def build(pg) -> list[Capability]:
    caps: list[Capability] = []
    signon = _signon_steps(pg)

    # 1. sign on -------------------------------------------------------------
    caps.append(_capability(
        "meridian_signon", "Sign on to MERIDIAN CORE and land on the main menu.",
        list(signon), [], []))

    # 2. member inquiry ------------------------------------------------------
    pg.goto(HOST + "/members", wait_until="load")
    _, fields = _captioned(pg)
    caps.append(_capability(
        "meridian_member_inquiry",
        "Find a member by member number or by last name, and open the record.",
        signon + [
            Step(5, ActionKind.NAVIGATE, url=HOST + "/members", risk=Risk.SAFE,
                 checkpoint=Checkpoint("text_present", "Search by:")),
            Step(6, ActionKind.SELECT, target=fields["Search by:"], text="{search_by}",
                 risk=Risk.REVERSIBLE),
            Step(7, ActionKind.TYPE, target=fields["Value:"], text="{value}",
                 risk=Risk.REVERSIBLE),
            # Search lands on a result list, not on the record, so the flow has
            # to pick the row. Both search modes come back the same shape, which
            # is why one artifact covers "by number" and "by last name".
            Step(8, ActionKind.CLICK, target=_control(pg, "button", "Search"),
                 risk=Risk.REVERSIBLE,
                 checkpoint=Checkpoint("text_present", "Shares")),
            Step(9, ActionKind.CLICK, target=TargetSet(candidates=[Target(
                strategy=Strategy.ROLE_NAME, value='link "Select"', frame=None,
                durability=0.95, note="the result row's own link")]),
                risk=Risk.REVERSIBLE,
                checkpoint=Checkpoint("text_present", "Member No.:")),
            Step(10, ActionKind.READ, target=_labelled_cell("Name:"), extracts="member_name",
                 risk=Risk.SAFE),
        ],
        [Param("search_by", "string", True, "'number' or 'name'", "number"),
         Param("value", "string", True, "the member number, or a last name", DEMO_MEMBER)],
        [Output("member_name", "string", "the member's name as filed")]))

    # 3. member record / balance --------------------------------------------
    caps.append(_capability(
        "meridian_member_balance",
        "Read one share's balance and status from a member's record.",
        signon + [
            Step(5, ActionKind.NAVIGATE, url=HOST + "/members/{member_id}", risk=Risk.SAFE,
                 checkpoint=Checkpoint("text_present", "Member No.:")),
            Step(6, ActionKind.READ, target=_labelled_cell("Name:"), extracts="member_name",
                 risk=Risk.SAFE),
            Step(7, ActionKind.READ, target=_row_cell(3), extracts="balance", risk=Risk.SAFE),
            Step(8, ActionKind.READ, target=_row_cell(4), extracts="status", risk=Risk.SAFE,
                 checkpoint=Checkpoint("text_present", "Member No.:")),
        ],
        [Param("member_id", "string", True, "member number", DEMO_MEMBER),
         Param("share_id", "string", True, "which share to read", f"{DEMO_MEMBER}-MMKT-2")],
        [Output("member_name", "string", "the member's name as filed"),
         Output("balance", "number", "the share's balance"),
         Output("status", "string", "OPEN, HOLD or CLOSED")]))

    # 4. funds transfer ------------------------------------------------------
    pg.goto(HOST + f"/members/{DEMO_MEMBER}/transfer", wait_until="load")
    _, fields = _captioned(pg)
    caps.append(_capability(
        "meridian_funds_transfer",
        "Move money between two of a member's shares. Posts immediately.",
        signon + [
            # Straight to the form, never to /review or /post. Those pages check
            # a per-session token the browser only sends by submitting the form
            # it came from, and arriving at one directly is what fails.
            Step(5, ActionKind.NAVIGATE, url=HOST + "/members/{member_id}/transfer",
                 risk=Risk.SAFE, checkpoint=Checkpoint("text_present", "FUNDS TRANSFER")),
            Step(6, ActionKind.SELECT, target=fields["From Share:"], text="{from_share}",
                 risk=Risk.REVERSIBLE),
            Step(7, ActionKind.SELECT, target=fields["To Share:"], text="{to_share}",
                 risk=Risk.REVERSIBLE),
            Step(8, ActionKind.TYPE, target=fields["Amount:"], text="{amount}",
                 risk=Risk.REVERSIBLE),
            Step(9, ActionKind.TYPE, target=fields["Memo:"], text="{memo}",
                 risk=Risk.REVERSIBLE),
            Step(10, ActionKind.CLICK, target=_control(pg, "button", "Continue"),
                 risk=Risk.REVERSIBLE,
                 checkpoint=Checkpoint("text_present", "CONFIRM FUNDS TRANSFER")),
            # The only irreversible step in the artifact, and the reason the
            # policy layer exists. Nothing before this writes anything.
            Step(11, ActionKind.CLICK, target=TargetSet(candidates=[Target(
                strategy=Strategy.ROLE_NAME, value='button "Post Transfer"', frame=None,
                durability=0.95, note="on the confirmation screen")]),
                risk=Risk.IRREVERSIBLE,
                checkpoint=Checkpoint("text_present", "TRANSACTION COMPLETE")),
            Step(12, ActionKind.READ, target=_labelled_cell("Confirmation:"),
                 extracts="confirmation", risk=Risk.SAFE),
        ],
        [Param("member_id", "string", True, "member number", DEMO_MEMBER),
         Param("from_share", "string", True, "share id to debit"),
         Param("to_share", "string", True, "share id to credit"),
         Param("amount", "string", True, "amount, as the screen wants it", "0.01"),
         Param("memo", "string", False, "free text on the transaction", "")],
        [Output("confirmation", "string", "the confirmation number the app issued")]))

    # 5. open new share ------------------------------------------------------
    pg.goto(HOST + f"/members/{DEMO_MEMBER}/open-share", wait_until="load")
    _, fields = _captioned(pg)
    caps.append(_capability(
        "meridian_open_share", "Open a new share for a member. Posts immediately.",
        signon + [
            Step(5, ActionKind.NAVIGATE, url=HOST + "/members/{member_id}/open-share",
                 risk=Risk.SAFE, checkpoint=Checkpoint("text_present", "OPEN NEW SHARE")),
            Step(6, ActionKind.SELECT, target=fields["Share Type:"], text="{share_type}",
                 risk=Risk.REVERSIBLE),
            Step(7, ActionKind.TYPE, target=fields["Initial Deposit:"], text="{deposit}",
                 risk=Risk.REVERSIBLE),
            Step(8, ActionKind.CLICK, target=_control(pg, "button", "Continue"),
                 risk=Risk.REVERSIBLE,
                 checkpoint=Checkpoint("text_present", "CONFIRM")),
            Step(9, ActionKind.CLICK, target=TargetSet(candidates=[Target(
                strategy=Strategy.ROLE_NAME, value='button "Open Share"', frame=None,
                durability=0.95, note="on the confirmation screen")]),
                risk=Risk.IRREVERSIBLE,
                checkpoint=Checkpoint("text_present", "TRANSACTION COMPLETE")),
        ],
        [Param("member_id", "string", True, "member number", DEMO_MEMBER),
         Param("share_type", "string", True, "S0001, S0070, MMKT or CERT", "MMKT"),
         Param("deposit", "string", True, "opening deposit", "5.00")],
        []))

    # 6. update member information ------------------------------------------
    pg.goto(HOST + f"/members/{DEMO_MEMBER}/update", wait_until="load")
    _, fields = _captioned(pg)
    caps.append(_capability(
        "meridian_update_member", "Change a member's email, phone and mailing address.",
        signon + [
            Step(5, ActionKind.NAVIGATE, url=HOST + "/members/{member_id}/update",
                 risk=Risk.SAFE,
                 checkpoint=Checkpoint("text_present", "UPDATE MEMBER INFORMATION")),
            Step(6, ActionKind.TYPE, target=fields["E-mail:"], text="{email}",
                 risk=Risk.REVERSIBLE),
            Step(7, ActionKind.TYPE, target=fields["Phone:"], text="{phone}",
                 risk=Risk.REVERSIBLE),
            Step(8, ActionKind.TYPE, target=fields["Mailing Address:"], text="{address}",
                 risk=Risk.REVERSIBLE),
            Step(9, ActionKind.CLICK, target=_control(pg, "button", "Save Changes"),
                 risk=Risk.IRREVERSIBLE,
                 checkpoint=Checkpoint("text_present", "Member No.:")),
        ],
        [Param("member_id", "string", True, "member number", DEMO_MEMBER),
         Param("email", "string", True, "new email address"),
         Param("phone", "string", True, "new phone number"),
         Param("address", "string", True, "new mailing address")],
        []))

    # 7. place account hold --------------------------------------------------
    pg.goto(HOST + f"/members/{DEMO_MEMBER}/hold", wait_until="load")
    _, fields = _captioned(pg)
    caps.append(_capability(
        "meridian_place_hold",
        "Freeze a share. Restricted: a teller is refused and a supervisor must sign on.",
        signon + [
            Step(5, ActionKind.NAVIGATE, url=HOST + "/members/{member_id}/hold",
                 risk=Risk.SAFE, checkpoint=Checkpoint("text_present", "PLACE ACCOUNT HOLD")),
            Step(6, ActionKind.SELECT, target=fields["Share:"], text="{share_id}",
                 risk=Risk.REVERSIBLE),
            Step(7, ActionKind.SELECT, target=fields["Reason Code:"], text="{reason}",
                 risk=Risk.REVERSIBLE),
            Step(8, ActionKind.TYPE, target=fields["Notes:"], text="{notes}",
                 risk=Risk.REVERSIBLE),
            # The refusal lands here, at review, before anything is written --
            # which is what makes this safe to demonstrate against a shared host.
            Step(9, ActionKind.CLICK, target=_control(pg, "button", "Continue"),
                 risk=Risk.REVERSIBLE,
                 checkpoint=Checkpoint("text_present", "CONFIRM")),
            Step(10, ActionKind.CLICK, target=TargetSet(candidates=[Target(
                strategy=Strategy.ROLE_NAME, value='button "Place Hold"', frame=None,
                durability=0.95, note="on the confirmation screen")]),
                risk=Risk.IRREVERSIBLE,
                checkpoint=Checkpoint("text_present", "TRANSACTION COMPLETE")),
        ],
        [Param("member_id", "string", True, "member number", DEMO_MEMBER),
         Param("share_id", "string", True, "which share to freeze"),
         Param("reason", "string", True, "FRAUD, LEGAL or DECEASED", "LEGAL"),
         Param("notes", "string", False, "free text", "")],
        []))

    return caps


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as play:
        browser = play.chromium.launch()
        pg = browser.new_page()
        pg.goto(HOST + "/signon", wait_until="load")
        pg.fill("input[name=operator]", "teller1")
        pg.fill("input[name=password]", "password")
        pg.select_option("select[name=branch]", "MAIN-001")
        pg.get_by_role("button", name="Sign On").click()
        pg.wait_for_load_state("load")
        caps = build(pg)
        browser.close()

    for cap in caps:
        path = save(cap, OUT / f"{cap.name}.json")
        by_caption = sum(
            1 for s in cap.steps if s.target
            and s.target.primary.strategy is Strategy.LABELLED_FIELD)
        print(f"  {cap.name:28} {len(cap.steps):2d} steps, "
              f"{by_caption} addressed by caption -> {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
