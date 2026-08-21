"""The CLI surface, exercised as a subprocess rather than by calling the handlers.

Everything else in the suite reaches past `cli.py` and constructs `Replay` or
`Policy` directly, which is how a `TypeError` on an un-instantiable Protocol
survived 86 passing tests: nothing ran the command. These tests invoke the
module the way a person does.

    python -m unittest tests.test_cli
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "capabilities" / "member_lookup.json"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_hands", *args],
        cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)},
    )


class ShowTest(unittest.TestCase):
    """`show` has two readers: whoever signs it, and whoever debugs it later."""

    def test_default_view_carries_the_targeting(self) -> None:
        out = run("show", str(ARTIFACT)).stdout
        self.assertIn("labeled_field", out)
        self.assertIn("dom_path", out)
        # the rejected ones too, with their reason
        self.assertIn("control has no accessible name", out)

    def test_brief_drops_the_targeting(self) -> None:
        out = run("show", "--brief", str(ARTIFACT)).stdout
        for jargon in ("labeled_field", "role_name", "dom_path", "nth_of_role"):
            self.assertNotIn(jargon, out)

    def test_brief_keeps_what_a_signer_needs(self) -> None:
        out = run("show", "--brief", str(ARTIFACT)).stdout
        self.assertIn("member_lookup", out)
        self.assertIn("APPROVED", out)
        self.assertIn("{member_id}", out)          # what varies
        self.assertIn("reversible", out)           # what it can undo
        self.assertIn("Member Detail", out)        # what proves it worked

    def test_both_views_list_every_step(self) -> None:
        for args in (("show", str(ARTIFACT)), ("show", "--brief", str(ARTIFACT))):
            with self.subTest(args=args):
                out = run(*args).stdout
                for line in ("0. navigate", "1. type", "2. click"):
                    self.assertIn(line, out)


class ArgumentTest(unittest.TestCase):

    def test_record_refuses_without_a_goal(self) -> None:
        """A recording with no goal parameterizes nothing, so it is a transcript."""
        r = run("record", "--url", "http://127.0.0.1:8899/")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--goal", r.stderr)

    def test_no_subcommand_is_not_a_crash(self) -> None:
        r = run()
        self.assertIn("record", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class OperatorConsoleTest(unittest.TestCase):
    """The console reads two lines. Cancelling at the second must not resume.

    An earlier fix tried to keep a choice that had already been typed when the
    note prompt hit EOF. That turned Ctrl-C into "resume", which is backwards:
    Ctrl-C is how a person cancels, and this module never guesses for them.
    """

    def _console(self):
        import io
        from agent_hands.escalation import ConsoleOwner
        return ConsoleOwner(headed=True, stream=io.StringIO())

    def _request(self):
        from agent_hands.escalation import InterventionRequest, Reason
        return InterventionRequest(
            id="int-test", reason=Reason.UNRESOLVED, capability="c", step_index=0,
            step_action="click", question="q", expected="e", observed="o", url="u")

    def test_ctrl_c_after_a_choice_still_aborts(self) -> None:
        from unittest.mock import patch
        from agent_hands.escalation import Decision
        with patch("builtins.input", side_effect=["r", KeyboardInterrupt()]):
            r = self._console().ask(self._request())
        self.assertIs(r.decision, Decision.ABORT)
        self.assertEqual(r.note, "no input available")

    def test_eof_aborts(self) -> None:
        from unittest.mock import patch
        from agent_hands.escalation import Decision
        with patch("builtins.input", side_effect=[EOFError()]):
            self.assertIs(self._console().ask(self._request()).decision, Decision.ABORT)

    def test_a_real_answer_resumes(self) -> None:
        from unittest.mock import patch
        from agent_hands.escalation import Decision
        with patch("builtins.input", side_effect=["r", "renamed the button back"]):
            r = self._console().ask(self._request())
        self.assertIs(r.decision, Decision.RESUME)
        self.assertEqual(r.note, "renamed the button back")

    def test_headless_never_prompts(self) -> None:
        """No window to fix anything in, so it must not ask."""
        import io
        from unittest.mock import patch
        from agent_hands.escalation import ConsoleOwner, Decision
        out = io.StringIO()
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            r = ConsoleOwner(headed=False, stream=out).ask(self._request())
        self.assertIs(r.decision, Decision.ABORT)
        self.assertIn("headless", out.getvalue())
