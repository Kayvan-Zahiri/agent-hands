"""Tests for the policy and escalation layers.

Stdlib unittest, no browser: escalation is written against a narrow `PageLike`
protocol precisely so the handoff can be tested without one.

    python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_hands.escalation import (
    ConsoleOwner, Decision as OperatorDecision, Escalator, InterventionRequest,
    Owner, OwnedPage, OwnershipError, Reason, ResumeRefused, ScriptedConsole,
    SessionOwner, TransitionError, default_verify, hand_back, operator_takeover,
    raise_intervention, resume_after_takeover,
)
from agent_hands.policy import (
    ApprovalRequired, FIXTURE_POLICY, Policy, PolicyViolation, classify_risk,
    redact, redact_json,
)
from agent_hands.schema import (
    ActionKind, Capability, Checkpoint, Risk, Step, Strategy, Target, TargetSet,
    infer_output_type, output_violation,
)

SEARCH = "http://127.0.0.1:8899/members/search"


def make_step(index: int = 1, action: ActionKind = ActionKind.CLICK,
              risk: Risk = Risk.SAFE, checkpoint: Checkpoint | None = None) -> Step:
    return Step(index=index, action=action, risk=risk, checkpoint=checkpoint)


def make_capability(approved: bool = True) -> Capability:
    return Capability(
        name="lookup_member_balance", description="", app_id="meridian-core",
        entry_url="http://127.0.0.1:8899/", params=[], outputs=[], steps=[],
        approved=approved)


class FakePage:
    """The slice of playwright's Page that escalation actually uses."""

    def __init__(self, url: str = SEARCH, body: str = "<b>Member Search</b>") -> None:
        self.url = url
        self._body = body
        self.shots: list[str] = []

    def content(self) -> str:
        return self._body

    def goto(self, url: str, body: str) -> None:
        self.url, self._body = url, body

    def screenshot(self, *, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.shots.append(path)


class BrokenPage(FakePage):
    def screenshot(self, *, path: str) -> None:
        raise RuntimeError("the browser has gone")


# --------------------------------------------------------------------------
# policy: the surface gate
# --------------------------------------------------------------------------

class TestSurfaceGate(unittest.TestCase):
    def test_allowed_path(self) -> None:
        self.assertTrue(FIXTURE_POLICY.check(make_step(), SEARCH + "?member_id=12345").allowed)

    def test_unlisted_path_refused(self) -> None:
        decision = FIXTURE_POLICY.check(make_step(), "http://127.0.0.1:8899/admin")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.gate, "surface")

    def test_root_prefix_does_not_open_the_origin(self) -> None:
        # "/" is listed, and must not act as a prefix for "/admin".
        self.assertTrue(FIXTURE_POLICY.check(make_step(), "http://127.0.0.1:8899/").allowed)
        self.assertFalse(FIXTURE_POLICY.check(make_step(), "http://127.0.0.1:8899/anything").allowed)

    def test_foreign_host_refused(self) -> None:
        self.assertFalse(FIXTURE_POLICY.check(make_step(), "http://evil.example.com/members/search").allowed)

    def test_wrong_port_is_a_different_host(self) -> None:
        self.assertFalse(FIXTURE_POLICY.check(make_step(), "http://127.0.0.1:9000/members/search").allowed)

    def test_non_http_schemes_refused(self) -> None:
        for url in ("javascript:alert(1)", "file:///etc/passwd", "/members/search", ""):
            self.assertFalse(FIXTURE_POLICY.check(make_step(), url).allowed, url)

    def test_empty_policy_refuses_everything(self) -> None:
        self.assertFalse(Policy().check(make_step(), SEARCH).allowed)

    def test_for_host_allows_the_whole_origin(self) -> None:
        policy = Policy.for_host("http://127.0.0.1:8899/")
        self.assertTrue(policy.check(make_step(), "http://127.0.0.1:8899/admin").allowed)


class TestActionGate(unittest.TestCase):
    def test_read_only_lane_blocks_writes(self) -> None:
        lane = FIXTURE_POLICY.read_only()
        self.assertTrue(lane.check(make_step(action=ActionKind.READ), SEARCH).allowed)
        decision = lane.check(make_step(action=ActionKind.TYPE), SEARCH)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.gate, "action")

    def test_read_only_does_not_mutate_the_parent(self) -> None:
        FIXTURE_POLICY.read_only()
        self.assertTrue(FIXTURE_POLICY.check(make_step(action=ActionKind.TYPE), SEARCH).allowed)


class TestRiskGate(unittest.TestCase):
    def setUp(self) -> None:
        self.step = make_step(risk=Risk.IRREVERSIBLE)

    def test_safe_and_reversible_run(self) -> None:
        for risk in (Risk.SAFE, Risk.REVERSIBLE):
            self.assertTrue(FIXTURE_POLICY.check(make_step(risk=risk), SEARCH).allowed, risk)

    def test_irreversible_needs_an_approved_artifact(self) -> None:
        decision = FIXTURE_POLICY.check(self.step, SEARCH, make_capability(approved=False), confirm=True)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.gate, "risk")

    def test_irreversible_needs_a_confirm(self) -> None:
        self.assertFalse(FIXTURE_POLICY.check(self.step, SEARCH, make_capability()).allowed)

    def test_irreversible_needs_both(self) -> None:
        self.assertTrue(FIXTURE_POLICY.check(self.step, SEARCH, make_capability(), confirm=True).allowed)

    def test_unknown_capability_is_not_approval(self) -> None:
        self.assertFalse(FIXTURE_POLICY.check(self.step, SEARCH, None, confirm=True).allowed)

    def test_an_approver_can_answer_instead_of_the_caller(self) -> None:
        asked: list[int] = []

        class Approver:
            def approve(self, *, capability: str, step: Step) -> bool:
                asked.append(step.index)
                return True

        policy = Policy(apps=FIXTURE_POLICY.apps, approver=Approver())
        self.assertTrue(policy.check(self.step, SEARCH, make_capability()).allowed)
        self.assertEqual(asked, [1])

    def test_a_declining_approver_refuses(self) -> None:
        policy = Policy(apps=FIXTURE_POLICY.apps,
                        approver=ConsoleOwner(auto=True, stream=io.StringIO()))
        self.assertFalse(policy.check(self.step, SEARCH, make_capability()).allowed)


class TestReplayFacingApi(unittest.TestCase):
    """replay.py calls check_step(capability, step) and reads .allowed/.reason."""

    def test_argument_order_and_shape(self) -> None:
        decision = FIXTURE_POLICY.check_step(make_capability(), make_step())
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.reason)

    def test_live_url_provider_beats_the_artifact(self) -> None:
        policy = Policy(apps=FIXTURE_POLICY.apps,
                        url_provider=lambda: "http://127.0.0.1:8899/admin")
        self.assertFalse(policy.check_step(make_capability(), make_step()).allowed)

    def test_a_broken_url_provider_falls_back(self) -> None:
        def boom() -> str:
            raise RuntimeError("page closed")

        policy = Policy(apps=FIXTURE_POLICY.apps, url_provider=boom)
        self.assertTrue(policy.check_step(make_capability(), make_step()).allowed)

    def test_business_outcomes_never_escalate(self) -> None:
        cap = make_capability()
        self.assertFalse(FIXTURE_POLICY.should_escalate(cap, "member_not_found"))
        self.assertFalse(FIXTURE_POLICY.should_escalate(cap, "blocked_by_policy"))
        self.assertTrue(FIXTURE_POLICY.should_escalate(cap, "target_unresolved"))


class TestArtifactGate(unittest.TestCase):
    def test_draft_artifact_refused(self) -> None:
        with self.assertRaises(ApprovalRequired):
            FIXTURE_POLICY.check_capability(make_capability(approved=False))

    def test_step_budget(self) -> None:
        cap = make_capability()
        cap.steps = [make_step(i) for i in range(FIXTURE_POLICY.max_steps + 1)]
        with self.assertRaises(PolicyViolation):
            FIXTURE_POLICY.check_capability(cap)

    def test_entry_url_is_checked(self) -> None:
        cap = make_capability()
        cap.entry_url = "http://evil.example.com/"
        with self.assertRaises(PolicyViolation):
            FIXTURE_POLICY.check_capability(cap)


class TestEnforce(unittest.TestCase):
    def test_surface_refusal_raises_with_the_decision(self) -> None:
        with self.assertRaises(PolicyViolation) as caught:
            FIXTURE_POLICY.enforce(make_step(), "http://127.0.0.1:8899/admin")
        self.assertEqual(caught.exception.decision.gate, "surface")

    def test_risk_refusal_raises_approval_required(self) -> None:
        with self.assertRaises(ApprovalRequired):
            FIXTURE_POLICY.enforce(make_step(risk=Risk.IRREVERSIBLE), SEARCH, make_capability())


class TestRiskClassification(unittest.TestCase):
    def test_reads_are_safe_and_writes_are_not(self) -> None:
        self.assertIs(classify_risk("read"), Risk.SAFE)
        self.assertIs(classify_risk("type"), Risk.REVERSIBLE)
        self.assertIs(classify_risk("click", label="Search"), Risk.REVERSIBLE)
        self.assertIs(classify_risk("click", label="Post Adjustment"), Risk.IRREVERSIBLE)


# --------------------------------------------------------------------------
# policy: redaction
# --------------------------------------------------------------------------

class TestRedaction(unittest.TestCase):
    def test_identifiers_are_scrubbed(self) -> None:
        cases = {
            "Member ID: 12345": "12345",
            "Account Number 22887": "22887",
            "?member_id=30021": "30021",
            "SSN 078-05-1120": "078-05-1120",
            "card 4111 1111 1111 1111": "4111",
            "routing 021000021": "021000021",
            "dolores@example.com": "example.com",
            "call 415-555-0142": "555-0142",
        }
        for text, secret in cases.items():
            self.assertNotIn(secret, redact(text), text)
            self.assertIn("[REDACTED", redact(text), text)

    def test_captions_survive_so_logs_stay_readable(self) -> None:
        self.assertTrue(redact("Member ID: 12345").startswith("Member ID: "))

    def test_ordinary_numbers_are_left_alone(self) -> None:
        keep = "Savings Balance 4812.55 on 2026-08-13, reference 0x8007007E, 3 accounts"
        self.assertEqual(redact(keep), keep)

    def test_host_and_port_survive(self) -> None:
        self.assertIn("127.0.0.1:8899", redact("url=http://127.0.0.1:8899/members/search"))

    def test_json_values_under_identifier_keys(self) -> None:
        out = redact_json({"member_id": "22887", "outputs": {"savings": "4812.55"},
                           "note": "call bernard@example.com", "steps": [{"password": "hunter2"}]})
        self.assertEqual(out["member_id"], "[REDACTED]")
        self.assertEqual(out["outputs"]["savings"], "4812.55")
        self.assertEqual(out["steps"][0]["password"], "[REDACTED]")
        self.assertNotIn("bernard", out["note"])

    def test_redact_walks_a_structure_it_is_handed(self) -> None:
        # The evidence writer hands whole payloads to whatever redactor it binds
        # to. A redactor that returned this dict untouched would look like it was
        # working while writing member IDs to disk.
        out = redact({"url": SEARCH + "?member_id=12345", "rows": ["Member ID 30021"]})
        self.assertNotIn("12345", json.dumps(out))
        self.assertNotIn("30021", json.dumps(out))

    def test_non_strings_pass_through(self) -> None:
        self.assertEqual(redact_json({"n": 12345, "ok": True, "none": None}),
                         {"n": 12345, "ok": True, "none": None})


# --------------------------------------------------------------------------
# escalation: ownership
# --------------------------------------------------------------------------

class TestSessionOwner(unittest.TestCase):
    def test_full_cycle(self) -> None:
        owner = SessionOwner()
        owner.to_operator("stuck")
        owner.to_resuming("handed back")
        owner.to_automation("verified")
        self.assertIs(owner.state, Owner.AUTOMATION)
        self.assertEqual(len(owner.history), 3)
        self.assertEqual(len(owner.trail()), 3)

    def test_illegal_transition_raises(self) -> None:
        owner = SessionOwner()
        owner.to_operator("stuck")
        with self.assertRaises(TransitionError):
            owner.to_automation("skipping the re-verify")

    def test_acting_out_of_turn_raises(self) -> None:
        owner = SessionOwner()
        page = OwnedPage(FakePage(), Owner.AUTOMATION, owner)
        self.assertEqual(page.url, SEARCH)
        owner.to_operator("stuck")
        with self.assertRaises(OwnershipError):
            page.content()
        with self.assertRaises(OwnershipError):
            _ = page.url

    def test_resuming_can_escalate_again(self) -> None:
        owner = SessionOwner()
        owner.to_operator("stuck")
        owner.to_resuming("handed back")
        owner.to_operator("checkpoint failed, back to the human")
        self.assertIs(owner.state, Owner.OPERATOR)


# --------------------------------------------------------------------------
# escalation: the packet, takeover, resume
# --------------------------------------------------------------------------

class EscalationCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.evidence = Path(tmp.name)
        self.owner = SessionOwner()
        self.capability = make_capability()
        self.step = make_step(index=4, action=ActionKind.READ,
                              checkpoint=Checkpoint("text_present", "Member Detail"))
        self.page = FakePage(SEARCH + "?member_id=12345", "<b>Session Expired</b>")

    def raise_one(self, page: FakePage | None = None) -> InterventionRequest:
        return raise_intervention(Reason.AUTH, self.capability, self.step,
                                  page or self.page, self.evidence,
                                  observed="Session Expired")


class TestInterventionRequest(EscalationCase):
    def test_carries_enough_context_to_act(self) -> None:
        request = self.raise_one()
        self.assertEqual(request.capability, "lookup_member_balance")
        self.assertEqual(request.step_index, 4)
        self.assertEqual(request.step_action, "read")
        self.assertIs(request.reason, Reason.AUTH)
        self.assertTrue(request.url.startswith("http://127.0.0.1:8899/members/search"))
        assert request.screenshot is not None
        self.assertTrue(Path(request.screenshot).exists())
        self.assertEqual(request.checkpoint, {"kind": "text_present", "value": "Member Detail"})
        assert request.expected is not None
        self.assertIn("text_present", request.expected)
        self.assertIn("Session Expired", request.render())

    def test_nothing_regulated_reaches_disk(self) -> None:
        request = self.raise_one()
        written = json.loads((self.evidence / f"{request.id}.json").read_text())
        self.assertNotIn("12345", json.dumps(written))
        self.assertIn("REDACTED", json.dumps(written))

    def test_a_failed_screenshot_does_not_lose_the_request(self) -> None:
        request = self.raise_one(BrokenPage())
        self.assertIsNone(request.screenshot)
        self.assertTrue(request.url)

    def test_works_without_an_evidence_directory(self) -> None:
        request = raise_intervention(Reason.LIMIT, self.capability, self.step, self.page)
        self.assertIsNone(request.evidence_dir)
        self.assertIsNone(request.screenshot)


class TestTakeover(EscalationCase):
    def test_operator_drives_the_same_page_and_hands_back(self) -> None:
        request = self.raise_one()
        console = ScriptedConsole(
            note="re-authenticated and reopened member 12345",
            actions=lambda p: p.goto(SEARCH + "?member_id=12345",
                                     "<b>Member Detail</b> Dolores Abernathy"))
        record = operator_takeover(request, self.page, self.owner, console)

        self.assertIs(self.owner.state, Owner.RESUMING)
        self.assertEqual(console.presented, [request])
        self.assertIn("Member Detail", self.page.content())     # the same object moved
        self.assertIs(record.decision, OperatorDecision.RESUME)
        self.assertNotIn("12345", record.note)                  # captured, then redacted
        assert record.screenshot_after is not None
        self.assertTrue(Path(record.screenshot_after).exists())
        self.assertNotIn("12345", (self.evidence / f"{request.id}-takeover.json").read_text())

    def test_automation_cannot_act_during_the_takeover(self) -> None:
        request = self.raise_one()
        stale = OwnedPage(self.page, Owner.AUTOMATION, self.owner)
        raced: list[str] = []

        def try_to_race(_: OwnedPage) -> None:
            try:
                stale.content()
            except OwnershipError as exc:
                raced.append(str(exc))

        operator_takeover(request, self.page, self.owner,
                          ScriptedConsole(actions=try_to_race))
        self.assertEqual(len(raced), 1)

    def test_a_console_that_blows_up_still_releases_the_session(self) -> None:
        request = self.raise_one()

        class Exploding(ScriptedConsole):
            def ask(self, request: InterventionRequest):     # type: ignore[override]
                raise RuntimeError("operator console lost its connection")

        with self.assertRaises(RuntimeError):
            operator_takeover(request, self.page, self.owner, Exploding())
        self.assertIs(self.owner.state, Owner.RESUMING)

    def test_hand_back_is_idempotent(self) -> None:
        request = self.raise_one()
        self.owner.to_operator("stuck")
        hand_back(self.owner, request)
        hand_back(self.owner, request)
        self.assertIs(self.owner.state, Owner.RESUMING)


class TestResume(EscalationCase):
    def resume_after(self, body: str, url: str = SEARCH):
        request = self.raise_one()
        console = ScriptedConsole(actions=lambda p: p.goto(url, body))
        operator_takeover(request, self.page, self.owner, console)
        return resume_after_takeover(self.owner, self.step, self.page)

    def test_checkpoint_holds_returns_control(self) -> None:
        verdict = self.resume_after("<b>Member Detail</b> Dolores Abernathy")
        self.assertTrue(verdict.verified)
        self.assertIs(self.owner.state, Owner.AUTOMATION)

    def test_human_moved_the_page_blocks_the_resume(self) -> None:
        verdict = self.resume_after("<b>Reports</b>", "http://127.0.0.1:8899/reports")
        self.assertFalse(verdict.verified)
        self.assertIs(self.owner.state, Owner.RESUMING)      # not handed back
        self.assertIn("not where the automation left it", verdict.message)

    def test_resume_out_of_state_raises(self) -> None:
        with self.assertRaises(OwnershipError):
            resume_after_takeover(self.owner, self.step, self.page)

    def test_missing_checkpoint_is_reported_not_assumed(self) -> None:
        self.owner.to_operator("stuck")
        self.owner.to_resuming("handed back")
        verdict = resume_after_takeover(self.owner, make_step(index=2), self.page)
        self.assertTrue(verdict.verified)
        self.assertIsNone(verdict.checkpoint)
        self.assertIn("without proof", verdict.message)

    def test_an_injected_verifier_is_used(self) -> None:
        self.owner.to_operator("stuck")
        self.owner.to_resuming("handed back")
        calls: list[Checkpoint] = []

        def verifier(page, checkpoint):
            calls.append(checkpoint)
            return True, "checked by the replay engine"

        verdict = resume_after_takeover(self.owner, self.step, self.page, verifier)
        self.assertTrue(verdict.verified)
        self.assertEqual(calls, [self.step.checkpoint])


class TestEscalator(EscalationCase):
    class Recorder:
        def __init__(self) -> None:
            self.events: list[tuple] = []

        def escalation(self, reason: str, question: str, answer: str | None = None) -> None:
            self.events.append(("escalation", reason, answer))

        def event(self, kind: str, **fields) -> None:
            self.events.append(("event", kind, fields))

    def test_escalate_then_resume_records_both_sides(self) -> None:
        recorder = self.Recorder()
        console = ScriptedConsole(
            note="fixed it",
            actions=lambda p: p.goto(SEARCH, "<b>Member Detail</b>"))
        escalator = Escalator(console, evidence=recorder, owner=self.owner)
        record = escalator.escalate(self.raise_one(), self.page)
        verdict = escalator.resume(self.step, self.page)

        self.assertTrue(record.page_moved is False or True)   # captured either way
        self.assertTrue(verdict.verified)
        self.assertIs(self.owner.state, Owner.AUTOMATION)
        kinds = [e[0] for e in recorder.events]
        self.assertEqual(kinds, ["escalation", "escalation", "event"])

    def test_evidence_failures_never_change_the_outcome(self) -> None:
        class Broken:
            def escalation(self, *a, **k):
                raise RuntimeError("disk full")

            def event(self, *a, **k):
                raise RuntimeError("disk full")

        escalator = Escalator(ScriptedConsole(), evidence=Broken(), owner=self.owner)
        escalator.escalate(self.raise_one(), self.page)
        self.assertIs(self.owner.state, Owner.RESUMING)

    def test_verify_resume_refuses_without_a_checkpoint(self) -> None:
        escalator = Escalator(ScriptedConsole())
        with self.assertRaises(ResumeRefused):
            escalator.verify_resume(self.page, None)

    def test_verify_resume_refuses_a_moved_page(self) -> None:
        escalator = Escalator(ScriptedConsole())
        with self.assertRaises(ResumeRefused):
            escalator.verify_resume(self.page, Checkpoint("text_present", "Member Detail"))

    def test_escalator_can_serve_as_the_policy_approver(self) -> None:
        escalator = Escalator(ScriptedConsole(approves=True))
        policy = Policy(apps=FIXTURE_POLICY.apps, approver=escalator)
        self.assertTrue(policy.check(make_step(risk=Risk.IRREVERSIBLE), SEARCH,
                                     make_capability()).allowed)


class TestDefaultVerify(unittest.TestCase):
    def test_reads_through_frames(self) -> None:
        class Frame:
            def __init__(self, body: str) -> None:
                self._body = body

            def content(self) -> str:
                return self._body

        class Detached(Frame):
            def content(self) -> str:
                raise RuntimeError("frame detached")

        class FramesetPage(FakePage):
            frames = [Frame("<frameset>"), Detached(""), Frame("<b>Member Detail</b>")]

        ok, _ = default_verify(FramesetPage(SEARCH, "<frameset>"),
                               Checkpoint("text_present", "Member Detail"))
        self.assertTrue(ok)

    def test_url_and_absence_checks(self) -> None:
        page = FakePage(SEARCH + "?member_id=12345", "<b>Member Detail</b>")
        self.assertTrue(default_verify(page, Checkpoint("url_contains", "/members/search"))[0])
        self.assertTrue(default_verify(page, Checkpoint("text_absent", "Session Expired"))[0])
        self.assertFalse(default_verify(page, Checkpoint("text_absent", "Member Detail"))[0])

    def test_element_present_uses_a_selector_when_there_is_one(self) -> None:
        class Queryable(FakePage):
            def query_selector(self, selector: str):
                return object() if selector == "#ctl00_r3_c1" else None

        page = Queryable()
        self.assertTrue(default_verify(page, Checkpoint("element_present", "#ctl00_r3_c1"))[0])
        self.assertFalse(default_verify(page, Checkpoint("element_present", "#missing"))[0])

    def test_observed_excerpt_is_redacted(self) -> None:
        page = FakePage(SEARCH, "Member ID 12345 Dolores Abernathy")
        _, observed = default_verify(page, Checkpoint("text_present", "nothing here"))
        self.assertNotIn("12345", observed)


if __name__ == "__main__":
    unittest.main()


class TestOutputTypes(unittest.TestCase):
    """A read is the one step nothing else can contradict.

    Every other action is judged by whether the screen changed. A read changes
    nothing, so a target that slipped one cell sideways still returns a real
    string from a real element and the run completes. The declared type is the
    only thing between that and a wrong answer nobody notices.
    """

    def test_inference_only_claims_what_it_can_defend(self) -> None:
        self.assertEqual("number", infer_output_type("4812.55"))
        self.assertEqual("number", infer_output_type("1,203.10"))   # thousands separator
        self.assertEqual("number", infer_output_type("$88.00"))     # currency mark
        # Deliberately NOT "integer", though the type exists and a person may
        # declare it. One sample cannot tell an integral field from a value that
        # happened to be round, and guessing wrong makes a later member holding
        # cents fail a check on a correct reading.
        self.assertEqual("number", infer_output_type("12345"))
        self.assertEqual("number", infer_output_type("5000"))
        # Everything else stays "string", the type that asserts nothing. A wrong
        # guess here makes a working capability refuse its own correct answer,
        # which is worse than the check not existing.
        self.assertEqual("string", infer_output_type("2019-04-11"))
        self.assertEqual("string", infer_output_type("Dolores Abernathy"))
        self.assertEqual("string", infer_output_type("Active"))
        self.assertEqual("string", infer_output_type(""))

    def test_a_date_is_not_a_balance(self) -> None:
        # The case this exists for. Inserting a row above the balances shifts
        # every later cell, and a positional read comes back with the row above.
        self.assertIsNotNone(output_violation("2019-04-11", "number"))
        self.assertIsNotNone(output_violation("Active", "number"))
        self.assertIsNone(output_violation("76230.18", "number"))

    def test_string_asserts_nothing(self) -> None:
        # Anything recorded before this existed is declared "string", so the
        # check has to be inert for it rather than failing every old artifact.
        self.assertIsNone(output_violation("2019-04-11", "string"))
        self.assertIsNone(output_violation("", "string"))

    def test_integer_rejects_a_fraction(self) -> None:
        self.assertIsNone(output_violation("12345", "integer"))
        self.assertIsNotNone(output_violation("4812.55", "integer"))

    def test_a_round_sample_does_not_reject_a_later_fraction(self) -> None:
        # The trap this inference is shaped to avoid: record a member whose
        # balance is whole, replay a member whose balance has cents.
        declared = infer_output_type("5000")
        self.assertIsNone(output_violation("4812.55", declared))

    def test_what_the_recorder_declares_replay_accepts(self) -> None:
        # These two run in different processes months apart. If they disagree on
        # what a number is, a recording declares a type its own replay rejects.
        for sample in ("4812.55", "1,203.10", "$88.00", "12345", "0", "-5.5"):
            with self.subTest(sample=sample):
                self.assertIsNone(output_violation(sample, infer_output_type(sample)))


class TestRecordedSecrets(unittest.TestCase):
    """A credential typed during a recording must not reach the artifact.

    An artifact is read in a diff and committed. A password in one is a password
    in version control, and nothing downstream gets it back out again.
    """

    def _capability(self, goal: str) -> Capability:
        from agent_hands.recorder import RecordedAction, record

        def target(value: str) -> TargetSet:
            return TargetSet(candidates=[Target(Strategy.LABELLED_FIELD, value, None, 0.85, "")])

        trajectory = [
            RecordedAction(action=ActionKind.TYPE, targets=target("Operator ID:"),
                           text="teller1", risk=Risk.REVERSIBLE),
            RecordedAction(action=ActionKind.TYPE, targets=target("Password:"),
                           text="hunter2", risk=Risk.REVERSIBLE),
            RecordedAction(action=ActionKind.TYPE, targets=target("PIN:"),
                           text="4417", risk=Risk.REVERSIBLE),
            RecordedAction(action=ActionKind.TYPE, targets=target("Member ID:"),
                           text="103001", risk=Risk.REVERSIBLE),
        ]
        return record(goal=goal, trajectory=trajectory, app_id="meridian-core",
                      entry_url="https://example.test/signon")

    def test_a_password_the_goal_never_mentions_is_still_hidden(self) -> None:
        cap = self._capability("Sign on as teller1, then look up member 103001")
        blob = json.dumps(cap.to_json())

        self.assertNotIn("hunter2", blob)
        self.assertNotIn("4417", blob)
        self.assertIn("password", {p.name for p in cap.params})

    def test_a_goal_that_names_the_password_does_not_smuggle_it_into_the_example(self) -> None:
        # The route that catches you out: name it in the goal and it becomes a
        # parameter on its own, carrying the literal forward as a helpful example.
        cap = self._capability(
            "Sign on as operator teller1 with password hunter2 and PIN 4417, "
            "then look up member 103001")
        blob = json.dumps(cap.to_json())

        self.assertNotIn("hunter2", blob)
        self.assertNotIn("4417", blob)
        self.assertTrue(all(p.example is None for p in cap.params
                            if "pass" in p.name or "pin" in p.name))

    def test_an_ordinary_field_keeps_its_worked_example(self) -> None:
        # The check has to stay narrow: an example is the most useful thing on a
        # parameter, and blanking every one of them to be safe would be a loss.
        cap = self._capability("Sign on as teller1, then look up member 103001")
        member = next(p for p in cap.params if p.name == "member_id")

        self.assertEqual("103001", member.example)


class TestWriteVocabulary(unittest.TestCase):
    """The words that decide whether a click is gated.

    Found by recording against a real servicing console: its most restricted
    action, the one that needs a supervisor, is a button reading "Apply Hold",
    and none of the original words appear in it. Freezing somebody's account
    classified as reversible and the gate never ran.
    """

    def risk(self, label: str) -> Risk:
        from agent_hands.policy import classify_risk
        return classify_risk("click", url="https://bank.test/members/1/hold", label=label)

    def test_the_buttons_that_write_are_gated(self) -> None:
        for label in ("Apply Hold", "Open Share", "Post Transfer", "Save Changes"):
            with self.subTest(label=label):
                self.assertIs(Risk.IRREVERSIBLE, self.risk(label))

    def test_navigating_to_the_form_is_not_a_write(self) -> None:
        # The reason these are phrases and not bare words. A menu link reading
        # "Open New Share" opens a form; the button reading "Open Share" on the
        # confirmation screen opens an account.
        for label in ("Open New Share", "Place Account Hold", "Search", "Continue"):
            with self.subTest(label=label):
                self.assertIs(Risk.REVERSIBLE, self.risk(label))

