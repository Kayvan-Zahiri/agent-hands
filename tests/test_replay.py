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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from agent_hands.escalation import Decision as OpDecision
from agent_hands.escalation import Escalator, ScriptedConsole
from agent_hands.perception import derive_targets, observe
from agent_hands.policy import AppAllowance, Policy
from agent_hands.replay import ParamError, Replay
from agent_hands.schema import (
    ActionKind, BusinessRule, Capability, Checkpoint, Outcome, Output, Param, Risk, Step,
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
        status = "ok  " if not problems else "FAIL"
        detail = result.business_code or result.message or ""
        print(f"  {status} {case.label:36} {result.outcome.value:17} {detail[:44]}")
        for line in result.recovered:
            print(f"       recovered: {line}")
        if problems:
            self.failures.append(f"{case.label}: {'; '.join(problems)}")


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
            Step(1, ActionKind.TYPE, target=derive_targets(field, obs.frame_for(field)),
                 text="{member_id}", risk=Risk.REVERSIBLE),
            Step(2, ActionKind.CLICK, target=derive_targets(button, obs.frame_for(button)),
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


def read_capability(page: Any, on_screen: str, out: str, declared: str) -> Capability:
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
            Step(1, ActionKind.READ, target=derive_targets(node, obs.frame_for(node)),
                 extracts=out, risk=Risk.SAFE),
        ])


def run(page: Any, cap: Capability, params: dict, policy: Policy | None = None) -> Any:
    """One replay with no operator available, so escalations abort rather than wait."""
    console = ScriptedConsole(decision=OpDecision.ABORT, note="unattended", approves=False)
    escalator = Escalator(console)
    policy = policy or Policy(apps=(AppAllowance(HOST),))
    return Replay(page, policy, escalator=escalator).run(cap, params)


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

            print("\nreading values back:")
            _reset()
            suite.check(
                Case("reads a balance", Outcome.SUCCESS,
                     outputs={"savings_balance": "4812.55"}),
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
            suite.check(Case("primary strategy missed", Outcome.SUCCESS),
                        run(page, degraded, {"member_id": "12345"}))

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
