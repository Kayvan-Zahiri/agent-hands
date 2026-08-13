"""Perception checked against the real fixture, not a mock of it.

A mock of a frameset is a frameset that behaves. The whole point of this layer
is the part where the application does not, so every assertion here runs against
`fixture.app` in a subprocess with a real browser attached.

    python -m unittest tests.test_perception     # assertions only
    python tests/test_perception.py              # the demo output, then the assertions
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402

from agent_hands.perception import (  # noqa: E402
    Node, Observation, TargetResolutionError, derive_targets, observe,
    resolve, resolve_detail,
)
from agent_hands.schema import Strategy, Target, TargetSet  # noqa: E402


# --------------------------------------------------------------------------
# fixture process
# --------------------------------------------------------------------------

class Fixture:
    """The target app in a subprocess. A subprocess because the fixture keeps
    its variant in a module global, so two variants cannot share an interpreter.
    """

    def __init__(self, variant: str = "default") -> None:
        self.variant = variant
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "Fixture":
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "fixture.app",
             "--port", str(self.port), "--variant", self.variant],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _wait_for_port(self.port)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"fixture did not come up on {port}")


def _open(browser: Browser, url: str) -> Page:
    page = browser.new_page()
    page.goto(url)
    page.wait_for_load_state("networkidle")
    return page


def _search_field(obs: Observation) -> Node:
    """The one control on the search form that has no accessible name."""
    fields = [n for n in obs.find(role="textbox") if not n.name]
    assert len(fields) == 1, f"expected one unnamed textbox, got {len(fields)}"
    return fields[0]


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

class PerceptionTest(unittest.TestCase):

    fixture: Fixture
    browser: Browser

    @classmethod
    def setUpClass(cls) -> None:
        cls._fixture_cm = Fixture("default")
        cls.fixture = cls._fixture_cm.__enter__()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        cls._fixture_cm.__exit__()

    def setUp(self) -> None:
        self.page = _open(self.browser, self.fixture.url)

    def tearDown(self) -> None:
        self.page.close()

    # -- observation --------------------------------------------------------

    def test_observation_crosses_frames(self) -> None:
        obs = observe(self.page)
        self.assertEqual(obs.unparsed, 0, "snapshot format changed")
        self.assertEqual({"main", "nav", "content"}, set(obs.frames))
        # The main document of a frameset contains no controls at all. Anything
        # that stopped at the top document would see an empty page.
        self.assertEqual([], [n for n in obs.interactive() if n.frame == "main"])
        self.assertTrue(obs.find(role="link", name="Reports", frame="nav"))
        self.assertTrue(obs.find(role="link", name="Search", frame="content"))

    def test_search_field_has_no_accessible_name(self) -> None:
        # The premise of the whole LABELLED_FIELD strategy. If this ever fails,
        # the fixture grew a label and the interesting case went away.
        field = _search_field(observe(self.page))
        self.assertEqual("", field.name)
        self.assertEqual("content", field.frame)

    # -- targeting ----------------------------------------------------------

    def test_field_is_targeted_by_caption_not_by_id(self) -> None:
        obs = observe(self.page)
        field = _search_field(obs)
        targets = derive_targets(field, obs.frame_for(field))

        self.assertIs(Strategy.LABELLED_FIELD, targets.primary.strategy)
        self.assertEqual("Member ID", targets.primary.value)

        # The server-generated id is in the DOM and must not be in the artifact.
        every_value = " ".join(t.value for t in targets.candidates + targets.rejected)
        self.assertNotIn("ctl00", every_value)

        # Both role strategies were considered and rejected for a stated reason.
        rejected = {t.strategy: t.note for t in targets.rejected}
        self.assertIn(Strategy.ROLE_NAME, rejected)
        self.assertIn("no accessible name", rejected[Strategy.ROLE_NAME])

    def test_resolve_lands_on_the_real_input(self) -> None:
        obs = observe(self.page)
        field = _search_field(obs)
        detail = resolve_detail(self.page, derive_targets(field, obs.frame_for(field)))

        self.assertEqual(0, detail.rank)
        self.assertFalse(detail.degraded)
        self.assertEqual("member_id", detail.locator.get_attribute("name"))
        # Found by caption, but it is the element the id also points at.
        self.assertEqual("ctl00_r3_c1", detail.locator.get_attribute("id"))

        detail.locator.fill("12345")
        self.assertEqual("12345", detail.locator.input_value())

    def test_ambiguous_name_demotes_role_name_to_the_frame_scoped_variant(self) -> None:
        # A second control with the same accessible name appears in another
        # frame. Page-wide role+name is now ambiguous and must not be recorded;
        # the frame-scoped variant is still exact and takes over as primary.
        nav = [f for f in self.page.frames if f.name == "nav"][0]
        nav.evaluate("""() => {
            const a = document.createElement('a');
            a.href = '#'; a.textContent = 'Search';
            document.body.appendChild(a);
        }""")
        obs = observe(self.page)
        node = obs.find(role="link", name="Search", frame="content")[0]
        targets = derive_targets(node, obs.frame_for(node))

        self.assertIs(Strategy.ROLE_NAME_IN_REGION, targets.primary.strategy)
        self.assertEqual("content", targets.primary.frame)
        rejected = {t.strategy: t.note for t in targets.rejected}
        self.assertIn("ambiguous", rejected[Strategy.ROLE_NAME])

    def test_point_is_recorded_but_never_offered_as_a_candidate(self) -> None:
        obs = observe(self.page)
        field = _search_field(obs)
        targets = derive_targets(field, obs.frame_for(field))
        strategies = [t.strategy for t in targets.candidates]
        self.assertNotIn(Strategy.POINT, strategies)
        point = [t for t in targets.rejected if t.strategy is Strategy.POINT]
        self.assertEqual(1, len(point), "coordinates should still be on the record")

    # -- resolution failure -------------------------------------------------

    def test_unresolvable_target_set_reports_every_attempt(self) -> None:
        stale = TargetSet(candidates=[
            Target(Strategy.ROLE_NAME, 'button "Submit"', None, 0.95, "gone"),
            Target(Strategy.LABELLED_FIELD, "Sort Code", "content", 0.85, "gone"),
            Target(Strategy.DOM_PATH, "body > form > input", "content", 0.2, "gone"),
        ])
        with self.assertRaises(TargetResolutionError) as caught:
            resolve(self.page, stale)
        attempts = caught.exception.attempts
        self.assertEqual(3, len(attempts), "every candidate must be tried and logged")
        self.assertTrue(all(a.matched == 0 for a in attempts))
        self.assertIn("Sort Code", str(caught.exception))

    def test_missing_frame_is_a_reason_not_a_crash(self) -> None:
        target = Target(Strategy.LABELLED_FIELD, "Member ID", "sidebar", 0.85, "")
        with self.assertRaises(TargetResolutionError) as caught:
            resolve(self.page, TargetSet(candidates=[target]))
        self.assertIn("not present", caught.exception.attempts[0].reason)


class VariantTest(unittest.TestCase):
    """The same code against a second tenant of the same vendor product.

    Nothing in perception knows about Westfield. The caption strategy carries
    over because a caption is what the screen means, and the second tenant
    renames the caption rather than restructuring the form.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._fixture_cm = Fixture("westfield")
        cls.fixture = cls._fixture_cm.__enter__()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        cls._fixture_cm.__exit__()

    def test_caption_strategy_survives_a_rebrand(self) -> None:
        page = _open(self.browser, self.fixture.url)
        obs = observe(page)
        field = _search_field(obs)
        targets = derive_targets(field, obs.frame_for(field))
        self.assertIs(Strategy.LABELLED_FIELD, targets.primary.strategy)
        self.assertEqual("Account Number", targets.primary.value)
        self.assertEqual("member_id", resolve(page, targets).get_attribute("name"))
        page.close()

    def test_the_other_tenants_caption_fails_loudly(self) -> None:
        # The failure mode worth guarding is not "no match", it is "wrong
        # match". A target recorded against Meridian must find nothing here
        # rather than land on whatever field happens to be first.
        page = _open(self.browser, self.fixture.url)
        target = Target(Strategy.LABELLED_FIELD, "Member ID", "content", 0.85, "")
        with self.assertRaises(TargetResolutionError):
            resolve(page, TargetSet(candidates=[target]))
        page.close()


# --------------------------------------------------------------------------
# demo
# --------------------------------------------------------------------------

def demo() -> None:
    """Print what the agent sees, and how it decides to name the search field."""
    import json

    with Fixture("default") as fx, sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = _open(browser, fx.url)
        obs = observe(page)

        print("=" * 72)
        print("OBSERVATION")
        print("=" * 72)
        print(obs.render())

        field = _search_field(obs)
        targets = derive_targets(field, obs.frame_for(field))
        print()
        print("=" * 72)
        print("TARGETS for the unnamed textbox in frame 'content'")
        print("=" * 72)
        print(json.dumps(targets.to_json(), indent=2))

        detail = resolve_detail(page, targets)
        print()
        print("=" * 72)
        print("RESOLUTION")
        print("=" * 72)
        print(f"won:  {detail.winner.strategy.value} = {detail.winner.value!r}")
        print(f"is:   <input name={detail.locator.get_attribute('name')!r} "
              f"id={detail.locator.get_attribute('id')!r}>")
        print("note: the id was never recorded; the field was found by the "
              "caption in the cell to its left.")
        browser.close()


if __name__ == "__main__":
    demo()
    print()
    unittest.main(argv=[sys.argv[0], "-v"])
