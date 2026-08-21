"""The behavior suite. Every case asserts an outcome, not just that it ran.

This is the file that found every real bug in this project, so it is worth
saying what it is for. It is not unit tests of the helpers; it drives the whole
engine against the live fixture and asserts which of the three outcomes came
back. The bugs that mattered here were all classification bugs -- a business
answer reported as a crash, a recovery that undid itself, a write that would
have been submitted twice -- and none of them are visible to a test that only
checks the happy path completes.

Run it with the fixture already up, or let it start its own:

    PYTHONPATH=. .venv/bin/python tests/test_replay.py
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import Frame, sync_playwright

from agent_hands.escalation import Decision as OpDecision
from agent_hands.escalation import Escalator, ScriptedConsole
from agent_hands.evidence import Evidence
from agent_hands.perception import Node, Observation, derive_targets, observe
from agent_hands.policy import AppAllowance, Policy
from agent_hands.recorder import load
from agent_hands.replay import ParamError, Replay
from agent_hands.schema import (
    ActionKind, BusinessRule, Capability, Checkpoint, Outcome, Output, Param, Risk, Step,
    ValueType,
)

HOST = "127.0.0.1:8899"
BASE = f"http://{HOST}"
RULES = [
    BusinessRule("member_not_found", "No member found", "no member with that identifier"),
    BusinessRule("invalid_identifier", "must be 5 digits", "identifier failed validation"),
]


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

@dataclass
class Case:
    label: str
    expect: Outcome
    code: str | None = None
    recovered: bool | None = None      # None = don't care
    outputs: dict | None = None        # None = don't care
    # (step, the strategy that actually found the control), one entry per step
    # that did not resolve on its primary. [] asserts a run did not degrade, and
    # a list rather than a mapping so a step recorded twice is a mismatch.
    degraded: list[tuple[int, str]] | None = None


class Suite:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.ran = 0

    def check(self, case: Case, result: Any) -> None:
        self.ran += 1
        problems = []
        if result.outcome is not case.expect:
            problems.append(f"outcome {result.outcome.value} != {case.expect.value}")
        if case.code is not None and result.business_code != case.code:
            problems.append(f"code {result.business_code!r} != {case.code!r}")
        if case.recovered is not None and bool(result.recovered) != case.recovered:
            problems.append(f"recovered={bool(result.recovered)} != {case.recovered}")
        if case.outputs is not None and result.outputs != case.outputs:
            problems.append(f"outputs {result.outputs!r} != {case.outputs!r}")
        if case.degraded is not None:
            seen = [(d.step, d.won_by.value) for d in result.degraded]
            if seen != case.degraded:
                problems.append(f"degraded {seen!r} != {case.degraded!r}")
        status = "ok  " if not problems else "FAIL"
        detail = result.business_code or result.message or ""
        print(f"  {status} {case.label:36} {result.outcome.value:17} {detail[:44]}")
        for line in result.recovered:
            print(f"       recovered: {line}")
        if problems:
            self.failures.append(f"{case.label}: {'; '.join(problems)}")

    def claim(self, label: str, ok: bool, detail: str) -> None:
        """A case whose evidence is several runs rather than one result."""
        self.ran += 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label:36} {detail[:60]}")
        if not ok:
            self.failures.append(f"{label}: {detail}")


def _fixture_up(url: str = BASE + "/") -> bool:
    try:
        urllib.request.urlopen(url, timeout=1).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def _start_fixture() -> subprocess.Popen | None:
    if _fixture_up():
        return None
    proc = subprocess.Popen(
        [sys.executable, "-m", "fixture.app", "--port", "8899"],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if _fixture_up():
            return proc
        time.sleep(0.25)
    raise RuntimeError("fixture did not come up")


def _reset() -> None:
    """Clear the fixture's one-shot flags so cases do not leak into each other."""
    try:
        urllib.request.urlopen(BASE + "/reset", timeout=2).read()
    except (urllib.error.URLError, OSError):
        pass


# --------------------------------------------------------------------------
# capabilities under test
# --------------------------------------------------------------------------

def _frame(obs: Observation, node: Node) -> Frame:
    """The frame a node was seen in. `frame_for` returns None when a frame has
    gone since the observation, which here means a broken fixture, not a case
    the test is about."""
    frame = obs.frame_for(node)
    assert frame is not None, f"no frame {node.frame!r} in the observation"
    return frame


def form_capability(page: Any, entry: str = BASE + "/members/search") -> Capability:
    """The realistic shape: land on the form, type, click the submit control."""
    page.goto(entry)
    obs = observe(page)
    field = next(n for n in obs.nodes if n.role in ("textbox", "searchbox"))
    button = next(n for n in obs.nodes if n.role == "link" and n.name == "Search")
    return Capability(
        name="member_lookup", description="Open a member's detail screen by id.",
        app_id="meridian-core", entry_url=entry,
        params=[Param("member_id", "string", True, "member identifier", "12345")],
        outputs=[Output("detail", "string")], approved=True, business_rules=RULES,
        steps=[
            Step(0, ActionKind.NAVIGATE, url=entry, risk=Risk.SAFE,
                 checkpoint=Checkpoint("text_present", "Member Search")),
            Step(1, ActionKind.TYPE, target=derive_targets(field, _frame(obs, field)),
                 text="{member_id}", risk=Risk.REVERSIBLE),
            Step(2, ActionKind.CLICK, target=derive_targets(button, _frame(obs, button)),
                 risk=Risk.REVERSIBLE,
                 checkpoint=Checkpoint("text_present", "Member Detail")),
        ])


def direct_capability(mode: str | None) -> Capability:
    """Drives straight to a result screen, to reach one branch without a form."""
    url = BASE + "/members/search?member_id={member_id}" + (f"&fail={mode}" if mode else "")
    return Capability(
        name=f"member_lookup_{mode or 'plain'}", description="direct",
        app_id="meridian-core", entry_url=BASE + "/members/search",
        params=[Param("member_id", "string", True)], outputs=[],
        approved=True, business_rules=RULES,
        steps=[Step(0, ActionKind.NAVIGATE, url=url, risk=Risk.SAFE,
                    checkpoint=Checkpoint("text_present", "Member Detail"))])


def read_capability(page: Any, on_screen: str, out: str, declared: ValueType) -> Capability:
    """Land on a detail screen and read one cell back as an output.

    `declared` is supplied rather than inferred so a test can state a type the
    screen does not satisfy. That is the whole point: a read is the one action
    with nothing to contradict it, so the declared type has to be what stops a
    wrong cell from being reported as an answer.
    """
    page.goto(BASE + "/members/search?member_id=12345")
    obs = observe(page)
    node = next(n for n in obs.addressable() if n.name == on_screen)
    return Capability(
        name=f"member_{out}", description=f"Read {out} from the detail screen.",
        app_id="meridian-core", entry_url=BASE + "/members/search",
        params=[Param("member_id", "string", True, "member identifier", "12345")],
        outputs=[Output(out, declared)], approved=True, business_rules=RULES,
        steps=[
            Step(0, ActionKind.NAVIGATE, url=BASE + "/members/search?member_id={member_id}",
                 risk=Risk.SAFE, checkpoint=Checkpoint("text_present", "Member Detail")),
            Step(1, ActionKind.READ, target=derive_targets(node, _frame(obs, node)),
                 extracts=out, risk=Risk.SAFE),
        ])


def checkpoint_capability(phrase: str) -> Capability:
    """Land on the detail screen and hold on `phrase`, whatever its case.

    The fixture's function-key bar reads "F7=Member Detail", so a checkpoint on
    "MEMBER DETAIL" is satisfied by chrome rather than by the screen unless the
    match is case-sensitive.
    """
    return Capability(
        name="checkpoint_case", description="Hold on a phrase after navigating.",
        app_id="meridian-core", entry_url=BASE + "/members/search",
        params=[Param("member_id", "string", True, "member identifier", "12345")],
        outputs=[], approved=True, business_rules=[],
        steps=[Step(0, ActionKind.NAVIGATE,
                    url=BASE + "/members/search?member_id={member_id}",
                    risk=Risk.SAFE, checkpoint=Checkpoint("text_present", phrase))])


def transfer_capability(page: Any) -> Capability:
    """A flow that has to choose from dropdowns, which nothing else here does.

    The fixture had no `<select>` at all until this screen existed, so the whole
    SELECT path -- the recorder's tool, the engine's action, and choosing by the
    option's value rather than its label -- ran only against the hosted target.
    The labels here carry a balance on purpose: recording the label would bake
    one member's money into a capability meant to run for everybody.
    """
    page.goto(BASE + "/transfer")
    obs = observe(page)
    fields = {}
    for node in obs.nodes:
        if node.role in ("textbox", "combobox", "searchbox"):
            targets = derive_targets(node, _frame(obs, node))
            fields[targets.primary.value] = targets
    button = next(n for n in obs.nodes if n.role == "button" and n.name == "Continue")
    return Capability(
        name="fixture_transfer", description="Move money between two accounts.",
        app_id="meridian-core", entry_url=BASE + "/transfer",
        params=[Param("from_account", "string", True, "account to debit", "AC-1"),
                Param("to_account", "string", True, "account to credit", "AC-2"),
                Param("amount", "string", True, "how much", "1.00")],
        outputs=[], approved=True, business_rules=RULES,
        steps=[
            Step(0, ActionKind.NAVIGATE, url=BASE + "/transfer", risk=Risk.SAFE,
                 checkpoint=Checkpoint("text_present", "Funds Transfer")),
            Step(1, ActionKind.SELECT, target=fields["From Account"], text="{from_account}",
                 risk=Risk.REVERSIBLE),
            Step(2, ActionKind.SELECT, target=fields["To Account"], text="{to_account}",
                 risk=Risk.REVERSIBLE),
            Step(3, ActionKind.TYPE, target=fields["Amount"], text="{amount}",
                 risk=Risk.REVERSIBLE),
            Step(4, ActionKind.CLICK, target=derive_targets(button, _frame(obs, button)),
                 risk=Risk.REVERSIBLE,
                 checkpoint=Checkpoint("text_present", "Transfer Posted")),
        ])


def run(page: Any, cap: Capability, params: dict, policy: Policy | None = None) -> Any:
    """One replay with no operator available, so escalations abort rather than wait."""
    console = ScriptedConsole(decision=OpDecision.ABORT, note="unattended", approves=False)
    escalator = Escalator(console)
    policy = policy or Policy(apps=(AppAllowance(HOST),))
    return Replay(page, policy, escalator=escalator).run(cap, params)


def read_across_members(page: Any, cap: Capability, out: str) -> dict[str, str]:
    """The same read, run for three members whose answers differ from each other."""
    seen = {}
    for member in ("12345", "22887", "30021"):
        _reset()
        seen[member] = run(page, cap, {"member_id": member}).outputs.get(out)
    return seen


def run_with_operator(page: Any, cap: Capability, params: dict, actions: Any = None) -> Any:
    """One replay where a person takes the session, does something, and resumes.

    `actions` runs against the same live page the automation was using, which is
    the point of escalating rather than failing: the operator inherits the login,
    the frameset and the half-filled form. A test that simulated the handoff with
    a flag would not exercise the thing that decides whether resuming is safe.
    """
    console = ScriptedConsole(decision=OpDecision.RESUME, note="fixed by hand",
                              approves=False, actions=actions)
    policy = Policy(apps=(AppAllowance(HOST),))
    return Replay(page, policy, escalator=Escalator(console)).run(cap, params)


def _click_search(page: Any) -> None:
    """The operator finishing the step by hand, which advances the page."""
    frame = [f for f in page.frames if f.name == "content"][0]
    frame.get_by_role("link", name="Search", exact=True).click()
    page.wait_for_timeout(600)


def _corrects_on_the_third_try() -> Any:
    """An operator who gets it right on their third consult.

    Late on purpose. A control that cannot be found is only escalated on a
    step's last attempt, because the earlier ones are spent looking again, so
    the third consult is where a resume used to come back with no attempt left
    to use it. Nothing here navigates: resume is only allowed while the last
    confirmed checkpoint still holds, and a fix that moves the page is refused
    by the case above.
    """
    consults = []

    def act(page: Any) -> None:
        consults.append(1)
        if len(consults) >= 3:
            page.get_by_role("textbox").first.fill("12345")

    return act


def _refusal(result: Any) -> str:
    return next((line for line in result.recovered if "resume_refused" in line), "")


# --------------------------------------------------------------------------
# the cases
# --------------------------------------------------------------------------

def main() -> int:
    proc = _start_fixture()
    suite = Suite()
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch()
            page = browser.new_page()
            policy = Policy(apps=(AppAllowance(HOST),))
            cap = form_capability(page)

            print("\nform flow (navigate, type, click):")
            for member_id, case in [
                ("12345", Case("valid member", Outcome.SUCCESS)),
                ("99999", Case("unknown member", Outcome.BUSINESS_OUTCOME, "member_not_found")),
                ("abcde", Case("non-numeric input", Outcome.BUSINESS_OUTCOME, "invalid_identifier")),
            ]:
                suite.check(case, run(page, cap, {"member_id": member_id}))

            print("\nrecoverable conditions:")
            _reset()
            suite.check(Case("no failure", Outcome.SUCCESS),
                        run(page, direct_capability(None), {"member_id": "12345"}))
            suite.check(Case("interstitial, resumes", Outcome.SUCCESS, recovered=True),
                        run(page, direct_capability("dialog"), {"member_id": "12345"}))
            suite.check(Case("3s stall", Outcome.SUCCESS, recovered=True),
                        run(page, direct_capability("slow"), {"member_id": "12345"}))

            # The one that needs a whole-flow re-entry: the notice discards the
            # request, so the typed value is gone and repeating the click alone
            # would submit an empty form.
            _reset()
            lossy = form_capability(page, BASE + "/members/search?fail=dialog_lossy")
            lossy.name = "member_lookup_lossy"
            suite.check(Case("interstitial, discards input", Outcome.SUCCESS, recovered=True),
                        run(page, lossy, {"member_id": "12345"}))

            print("\nescalation, resume and handback:")
            _reset()
            stale = load(Path(__file__).resolve().parent.parent
                         / "capabilities" / "member_lookup_stale.json")
            # An artifact as the recorder actually produces one: a checkpoint on
            # the last step and nowhere else. Nothing earlier was ever confirmed,
            # so on resume there is no known screen to compare against and the
            # engine refuses rather than guessing where it is.
            r = run_with_operator(page, stale, {"member_id": "12345"}, _click_search)
            suite.check(Case("resume with nothing confirmed", Outcome.FAILURE), r)
            assert "no confirmed checkpoint" in _refusal(r), _refusal(r)

            # Same flow with an earlier step carrying a checkpoint. Now there is
            # a known screen, and the operator having moved past it is detected.
            early = replace(stale, steps=[
                replace(stale.steps[0], checkpoint=Checkpoint("text_present", "Member Search")),
                *stale.steps[1:]])
            r = run_with_operator(page, early, {"member_id": "12345"}, _click_search)
            suite.check(Case("operator moved the page, resume refused", Outcome.FAILURE), r)
            assert "page moved during handoff" in _refusal(r), _refusal(r)

            # Handing the session back is not the same as fixing it. The
            # operator changes nothing, the control is still unfindable, and the
            # step runs out of attempts. Success here would be the worst answer
            # available: the click was never sent and the step's own checkpoint
            # was never verified, so the run would report a flow it did not run.
            _reset()
            r = run_with_operator(page, early, {"member_id": "12345"}, None)
            suite.check(Case("operator resumed without fixing anything", Outcome.FAILURE), r)
            assert _refusal(r) == "", f"resume should have verified, got {_refusal(r)}"
            assert "did not complete" in r.message, r.message

            # The case resume exists for, run end to end. The app came back "no
            # member found", which this artifact carries no rule for, so the
            # checkpoint fails and a person is asked. They correct the value on
            # the screen the run was left on, so the last confirmed checkpoint
            # still holds, resume verifies, and the retried click arrives.
            _reset()
            corrected = copy.deepcopy(cap)
            corrected.name = "member_lookup_corrected"
            corrected.business_rules = []
            r = run_with_operator(page, corrected, {"member_id": "99999"},
                                  _corrects_on_the_third_try())
            suite.check(Case("operator corrected it, run completes", Outcome.SUCCESS), r)
            # The outcome alone does not prove the retry happened -- before the
            # attempt budget accounted for resumes, this reported success with
            # the corrected value never submitted.
            assert "member_id=12345" in page.url, page.url

            print("\ncheckpoints are case-sensitive:")
            _reset()
            # The screen really does say "Member Detail", so this holds.
            suite.check(
                Case("a phrase the screen actually shows", Outcome.SUCCESS),
                run(page, checkpoint_capability("Member Detail"), {"member_id": "12345"}))
            # It does not say "MEMBER DETAIL". The function-key bar says
            # "F7=Member Detail", and `get_by_text` is case-insensitive, so this
            # used to hold on chrome that is printed on every screen. Against
            # the real target that made signing on with the wrong password
            # report success, because every artifact's step 4 waits for
            # "MAIN MENU" and the bar reads "F5=Main Menu".
            suite.check(
                Case("the same phrase in the wrong case does not", Outcome.FAILURE),
                run(page, checkpoint_capability("MEMBER DETAIL"), {"member_id": "12345"}))
            # A phrase carrying a regex metacharacter still matches literally.
            suite.check(
                Case("a checkpoint with a metacharacter in it", Outcome.SUCCESS),
                run(page, checkpoint_capability("4812.55"), {"member_id": "12345"}))

            print("\nreading values back:")
            _reset()
            suite.check(
                Case("reads a balance", Outcome.SUCCESS,
                     outputs={"savings_balance": "4812.55"}, degraded=[]),
                run(page, read_capability(page, "4812.55", "savings_balance", "number"),
                    {"member_id": "12345"}))
            # A name where a number was declared. This is what a read that
            # slipped one cell looks like from the inside, and without the check
            # it completes with a person's name reported as their balance.
            suite.check(
                Case("a name declared as a number", Outcome.FAILURE),
                run(page, read_capability(page, "Dolores Abernathy", "savings_balance", "number"),
                    {"member_id": "12345"}))
            # Anything recorded before outputs carried a real type is "string",
            # so the check has to stay inert for it.
            suite.check(
                Case("string declaration asserts nothing", Outcome.SUCCESS,
                     outputs={"member_name": "Dolores Abernathy"}),
                run(page, read_capability(page, "Dolores Abernathy", "member_name", "string"),
                    {"member_id": "12345"}))

            # A read is the one action with nothing to contradict it, and the
            # declared type only catches a wrong cell when that cell holds the
            # wrong kind of value. A caption holds a string, and "string" asserts
            # nothing. Members whose balances differ are the only thing in the
            # system that can tell a read of the answer from a read of the label
            # beside it, and only held-out members do it: the recorded balance is
            # the recorded target, so the member it was recorded against agrees
            # with a capability pointed anywhere.
            balances = read_across_members(
                page, read_capability(page, "4812.55", "savings_balance", "number"),
                "savings_balance")
            suite.claim("a value read differs per member",
                        sorted(balances.values()) == ["119.02", "4812.55", "76230.18"],
                        repr(balances))

            # The same capability recorded one cell to the left. Every run
            # succeeds, the declared type holds, and all three members are handed
            # the caption as their balance. Worth running rather than describing:
            # it is what the case above exists to catch.
            captions = read_across_members(
                page, read_capability(page, "Savings Balance", "savings_balance", "string"),
                "savings_balance")
            suite.claim("a caption read is the same for everyone",
                        set(captions.values()) == {"Savings Balance"},
                        repr(captions))

            # A target that names the control in terms of the invocation. The
            # engine substituted into text and urls but never into targets, so a
            # flow that has to pick one row out of a table -- most of what a
            # servicing console does -- could not be recorded at all.
            _reset()
            by_param = copy.deepcopy(cap)
            by_param.name = "member_lookup_by_param"
            for step in by_param.steps:
                if step.action is ActionKind.TYPE and step.target:
                    step.target.candidates = [
                        replace(step.target.candidates[0], value="{field_label}"),
                        *step.target.candidates[1:]]
            by_param.params = [*by_param.params,
                               Param("field_label", "string", True, "the field's caption")]
            suite.check(Case("a target named by a parameter", Outcome.SUCCESS,
                             degraded=[]),
                        run(page, by_param,
                            {"member_id": "12345", "field_label": "Member ID"}))

            # Evidence is kept and reviewed, so what a step typed must not be a
            # way to read back a credential the params block took care to hide.
            _reset()
            ev_root = Path(tempfile.mkdtemp(prefix="agent-hands-evidence-"))
            ev = Evidence.start("member_lookup", root=ev_root,
                                params={"member_id": "12345"})
            Replay(page, Policy(apps=(AppAllowance(HOST),)), evidence=ev).run(
                cap, {"member_id": "12345"})
            typed = [json.loads(line).get("text") for line in
                     (ev.dir / "run.jsonl").read_text().splitlines()
                     if json.loads(line).get("action") == "type"]
            suite.claim("a typed parameter stays out of the evidence",
                        typed == ["{member_id}"], repr(typed))
            shutil.rmtree(ev_root, ignore_errors=True)

            # An optional parameter a step mentions was a required one in
            # disguise: bind_params left it unbound and substitute refused the
            # placeholder, so the run stopped before the browser opened.
            _reset()
            optional = copy.deepcopy(cap)
            optional.name = "member_lookup_optional"
            optional.params = [*optional.params,
                               Param("note", "string", False, "free text nobody has to give")]
            optional.steps[1].text = "{member_id}{note}"
            suite.check(Case("an optional parameter left out", Outcome.SUCCESS),
                        run(page, optional, {"member_id": "12345"}))

            # Choosing from a dropdown, end to end. Every step here is addressed
            # by its caption, on a form whose shape used to make that impossible.
            _reset()
            moving = transfer_capability(page)
            suite.check(Case("chooses from two dropdowns", Outcome.SUCCESS, degraded=[]),
                        run(page, moving, {"from_account": "AC-1", "to_account": "AC-2",
                                           "amount": "1.00"}))

            # The option's value, not the label a person reads. The labels here
            # carry a balance, so a flow that matched on them would break the
            # moment the money moved.
            by_label = copy.deepcopy(moving)
            by_label.name = "fixture_transfer_by_label"
            by_label.params[0] = replace(by_label.params[0], example="AC-1")
            suite.check(Case("an option chosen by its value", Outcome.SUCCESS),
                        run(page, by_label, {"from_account": "AC-2", "to_account": "AC-1",
                                             "amount": "2.00"}))

            print("\nhard failures:")
            for mode, label in [("timeout", "session expired"),
                                ("denied", "permission denied"),
                                ("error", "application error")]:
                _reset()
                suite.check(Case(label, Outcome.FAILURE),
                            run(page, direct_capability(mode), {"member_id": "12345"}))

            print("\ndegradation and staleness:")
            # Break only the primary strategy. The run should still succeed on a
            # lower-ranked candidate -- that is the point of recording a list --
            # and the evidence should say it degraded. A success here is the
            # right answer; a silent one would not be.
            degraded = copy.deepcopy(cap)
            degraded.name = "member_lookup_degraded"
            for step in degraded.steps:
                if step.action is ActionKind.CLICK and step.target:
                    primary = step.target.candidates[0]
                    primary.__dict__["value"] = primary.value.replace("Search", "Submit Query")
            suite.check(Case("primary strategy missed", Outcome.SUCCESS,
                             degraded=[(2, "role_name_in_region")]),
                        run(page, degraded, {"member_id": "12345"}))

            # The same broken primary on a flow that starts over. Re-entry
            # resolves every target again, so a degradation recorded per attempt
            # rather than per step would report this one step twice, and anything
            # counting them would read a recoverable interstitial as drift.
            _reset()
            lossy_degraded = form_capability(
                page, BASE + "/members/search?fail=dialog_lossy")
            lossy_degraded.name = "member_lookup_lossy_degraded"
            for step in lossy_degraded.steps:
                if step.action is ActionKind.CLICK and step.target:
                    primary = step.target.candidates[0]
                    primary.__dict__["value"] = primary.value.replace("Search", "Submit Query")
            suite.check(Case("degraded flow that starts over", Outcome.SUCCESS,
                             recovered=True, degraded=[(2, "role_name_in_region")]),
                        run(page, lossy_degraded, {"member_id": "12345"}))

            # Now break every strategy: the recording genuinely no longer matches
            # the application, and there is nothing left to fall back to.
            stale = copy.deepcopy(cap)
            stale.name = "member_lookup_stale"
            for step in stale.steps:
                if step.action is ActionKind.CLICK and step.target:
                    for cand in step.target.candidates:
                        broken = (cand.value
                                  .replace("Search", "Submit Query")
                                  .replace("link[0]", "link[9]")
                                  .replace("> a", "> button"))
                        cand.__dict__["value"] = broken
            suite.check(Case("every strategy missed", Outcome.FAILURE),
                        run(page, stale, {"member_id": "12345"}))

            print("\npolicy:")
            suite.check(Case("host not allowlisted", Outcome.FAILURE),
                        run(page, cap, {"member_id": "12345"},
                            Policy(apps=(AppAllowance("example.com"),))))
            draft = copy.deepcopy(cap); draft.approved = False
            suite.check(Case("unapproved draft", Outcome.FAILURE),
                        run(page, draft, {"member_id": "12345"}))
            suite.check(Case("read-only lane vs a write", Outcome.FAILURE),
                        run(page, cap, {"member_id": "12345"}, policy.read_only()))

            # A malformed call is the caller's bug, not a replay outcome, so it
            # raises rather than returning a FAILURE that would be filed among
            # the real automation failures.
            suite.ran += 1
            try:
                run(page, cap, {"membr_id": "12345"})
                suite.failures.append("mistyped parameter: expected ParamError")
                print("  FAIL mistyped parameter                 no error raised")
            except ParamError as exc:
                print(f"  ok   {'mistyped parameter':36} raised ParamError  {str(exc)[:34]}")

            browser.close()
    finally:
        if proc is not None:
            proc.terminate()

    print(f"\n{suite.ran - len(suite.failures)}/{suite.ran} passed")
    for failure in suite.failures:
        print(f"  FAILED  {failure}")
    return 1 if suite.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
