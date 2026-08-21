"""Perception checked against the real fixture, not a mock of it.

A mock of a frameset is a frameset that behaves. The whole point of this layer
is the part where the application does not, so every assertion here runs against
`fixture.app` in a subprocess with a real browser attached.

    python -m unittest tests.test_perception     # assertions only
    python tests/test_perception.py              # the demo output, then the assertions
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Browser, Frame, Page, Playwright, sync_playwright  # noqa: E402

from agent_hands.perception import (  # noqa: E402
    Node, Observation, TargetResolutionError, derive_targets, observe,
    resolve, resolve_detail,
)
from agent_hands.schema import Strategy, Target, TargetSet  # noqa: E402
from agent_hands.discover import _perform  # noqa: E402
from agent_hands.policy import AppAllowance, Policy  # noqa: E402
from agent_hands.recorder import _outputs  # noqa: E402


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


def _frame(obs: Observation, node: Node) -> Frame:
    """The frame a node was seen in. `frame_for` returns None when a frame has
    gone since the observation, which here means a broken fixture, not a case
    the test is about."""
    frame = obs.frame_for(node)
    assert frame is not None, f"no frame {node.frame!r} in the observation"
    return frame


def _detail(page: Page) -> Observation:
    """Walk the flow to the detail screen, rather than loading its URL.

    Loading it directly gives a page with no frameset, so every node lands in
    frame "main" and a target derived there resolves against nothing during a
    real replay.
    """
    frame = [f for f in page.frames if f.name == "content"][0]
    frame.get_by_role("textbox").first.fill("12345")
    frame.get_by_role("link", name="Search", exact=True).click()
    page.wait_for_timeout(600)
    return observe(page)


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
    _fixture_cm: Fixture
    _pw: Playwright

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
        # The premise of the whole LABELED_FIELD strategy. If this ever fails,
        # the fixture grew a label and the interesting case went away.
        field = _search_field(observe(self.page))
        self.assertEqual("", field.name)
        self.assertEqual("content", field.frame)

    # -- addressable nodes --------------------------------------------------

    def test_a_value_cell_is_offered_alongside_the_controls(self) -> None:
        # read_value takes a control number, so a value the model cannot number
        # is a value it can never return. Interactive-only numbering made every
        # read unreachable on a screen that is pure output, which the detail
        # screen is.
        obs = _detail(self.page)
        names = [n.name for n in obs.addressable()]
        self.assertIn("4812.55", names)
        self.assertIn("1203.10", names)
        # The nav frame keeps its links, so scope this to the screen itself.
        self.assertEqual([], [n for n in obs.interactive() if n.frame == "content"],
                         "detail screen gained a control")

    def test_a_cell_wrapping_other_cells_is_not_offered(self) -> None:
        # The detail table sits inside an outer cell whose accessible name is
        # every value run together. Offering it means the model can pick a
        # target that reads "Savings Balance 4812.55 Checking Balance 1203.10"
        # and call that an answer.
        obs = _detail(self.page)
        for n in obs.addressable():
            self.assertNotIn("Savings Balance 4812.55", n.name)

    def test_the_model_and_the_resolver_number_alike(self) -> None:
        # _render shows the list and _node_at reads a number back out of it.
        # They were two hand-kept copies of one filter; if they ever disagree,
        # every recorded control number points somewhere else.
        from agent_hands.discover import _node_at, _render
        obs = _detail(self.page)
        shown = [ln for ln in _render(obs).splitlines() if ln.strip().startswith("6:")]
        self.assertTrue(shown, "control 6 was not offered")
        self.assertIn(repr(_node_at(obs, 6).name), shown[0])

    # -- targeting ----------------------------------------------------------

    def test_field_is_targeted_by_caption_not_by_id(self) -> None:
        obs = observe(self.page)
        field = _search_field(obs)
        targets = derive_targets(field, _frame(obs, field))

        self.assertIs(Strategy.LABELED_FIELD, targets.primary.strategy)
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
        detail = resolve_detail(self.page, derive_targets(field, _frame(obs, field)))

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
        targets = derive_targets(node, _frame(obs, node))

        self.assertIs(Strategy.ROLE_NAME_IN_REGION, targets.primary.strategy)
        self.assertEqual("content", targets.primary.frame)
        rejected = {t.strategy: t.note for t in targets.rejected}
        self.assertIn("ambiguous", rejected[Strategy.ROLE_NAME])

    def test_point_is_recorded_but_never_offered_as_a_candidate(self) -> None:
        obs = observe(self.page)
        field = _search_field(obs)
        targets = derive_targets(field, _frame(obs, field))
        strategies = [t.strategy for t in targets.candidates]
        self.assertNotIn(Strategy.POINT, strategies)
        point = [t for t in targets.rejected if t.strategy is Strategy.POINT]
        self.assertEqual(1, len(point), "coordinates should still be on the record")

    # -- resolution failure -------------------------------------------------

    def test_unresolvable_target_set_reports_every_attempt(self) -> None:
        stale = TargetSet(candidates=[
            Target(Strategy.ROLE_NAME, 'button "Submit"', None, 0.95, "gone"),
            Target(Strategy.LABELED_FIELD, "Sort Code", "content", 0.85, "gone"),
            Target(Strategy.DOM_PATH, "body > form > input", "content", 0.2, "gone"),
        ])
        with self.assertRaises(TargetResolutionError) as caught:
            resolve(self.page, stale)
        attempts = caught.exception.attempts
        self.assertEqual(3, len(attempts), "every candidate must be tried and logged")
        self.assertTrue(all(a.matched == 0 for a in attempts))
        self.assertIn("Sort Code", str(caught.exception))

    def test_missing_frame_is_a_reason_not_a_crash(self) -> None:
        target = Target(Strategy.LABELED_FIELD, "Member ID", "sidebar", 0.85, "")
        with self.assertRaises(TargetResolutionError) as caught:
            resolve(self.page, TargetSet(candidates=[target]))
        self.assertIn("not present", caught.exception.attempts[0].reason)


class StackedFormTest(unittest.TestCase):
    """A form with several caption/field rows, which the search screen is not.

    The caption strategy used to look in two directions at once and treat two
    matches as a layout too ambiguous to guess at. On a stacked form the second
    direction does not find the same field from another angle -- it finds the
    next row's field -- so every field matched twice and was refused, and every
    control fell through to being counted by position.

    The search screen has exactly one caption/field row followed by a row holding
    an anchor, so the second direction never matched there and the bug stayed
    invisible to every test in this file. This screen exists so it cannot.
    """

    fixture: Fixture
    browser: Browser
    _fixture_cm: Fixture
    _pw: Playwright

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
        self.page = _open(self.browser, self.fixture.url + "/transfer")

    def tearDown(self) -> None:
        self.page.close()

    def _fields(self) -> list[TargetSet]:
        # A list, not a dict keyed by name: none of these controls has an
        # accessible name, which is the whole reason the caption strategy exists.
        obs = observe(self.page)
        return [derive_targets(n, _frame(obs, n))
                for n in obs.nodes if n.role in ("textbox", "combobox", "searchbox")]

    def test_every_field_is_addressed_by_its_caption(self) -> None:
        found = {t.primary.value for t in self._fields()}

        self.assertEqual({"From Account", "To Account", "Amount", "Memo"}, found)

    def test_none_of_them_fall_through_to_counting(self) -> None:
        # The failure this screen exists to catch: refusing the caption strategy
        # leaves position, which is the brittleness the module exists to avoid.
        for targets in self._fields():
            with self.subTest(caption=targets.primary.value):
                self.assertIs(Strategy.LABELED_FIELD, targets.primary.strategy)

    def test_a_caption_resolves_to_the_control_beside_it(self) -> None:
        # Matching one element is not enough; it has to be the right one.
        fields = self._fields()
        for caption, want in (("From Account", "from"), ("To Account", "to"),
                              ("Amount", "amount"), ("Memo", "memo")):
            targets = next(t for t in fields if t.primary.value == caption)
            with self.subTest(caption=caption):
                self.assertEqual(want, resolve(self.page, targets).get_attribute("name"))


class VariantTest(unittest.TestCase):
    """The same code against a second tenant of the same vendor product.

    Nothing in perception knows about Westfield. The caption strategy carries
    over because a caption is what the screen means, and the second tenant
    renames the caption rather than restructuring the form.
    """

    fixture: Fixture
    browser: Browser
    _fixture_cm: Fixture
    _pw: Playwright

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
        targets = derive_targets(field, _frame(obs, field))
        self.assertIs(Strategy.LABELED_FIELD, targets.primary.strategy)
        self.assertEqual("Account Number", targets.primary.value)
        self.assertEqual("member_id", resolve(page, targets).get_attribute("name"))
        page.close()

    def test_the_other_tenants_caption_fails_loudly(self) -> None:
        # The failure mode worth guarding is not "no match", it is "wrong
        # match". A target recorded against Meridian must find nothing here
        # rather than land on whatever field happens to be first.
        page = _open(self.browser, self.fixture.url)
        target = Target(Strategy.LABELED_FIELD, "Member ID", "content", 0.85, "")
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
        targets = derive_targets(field, _frame(obs, field))
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


class RecordedOutputTest(unittest.TestCase):
    """The recording path, end to end, minus the model.

    A read is typed from what it actually pulled off the screen at record time.
    That value passes through three hands -- the tool call, the recorded action,
    the output declaration -- and dropping it anywhere in between costs nothing
    visible: the artifact still saves, still replays, and still returns the value.
    It just declares "string", which asserts nothing, so the type check has
    nothing left to enforce. Testing the pieces separately cannot see that, which
    is why this drives the real path instead.
    """

    fixture: Fixture
    browser: Browser
    _fixture_cm: Fixture
    _pw: Playwright
    policy: Policy

    @classmethod
    def setUpClass(cls) -> None:
        cls._fixture_cm = Fixture("default")
        cls.fixture = cls._fixture_cm.__enter__()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()
        cls.policy = Policy(apps=(AppAllowance(cls.fixture.url.split("//")[1].rstrip("/")),))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        cls._fixture_cm.__exit__()

    def _read(self, on_screen: str, out: str) -> list:
        """Drive the detail screen and perform one read_value, as discovery does."""
        page = _open(self.browser, self.fixture.url)
        try:
            frame = [f for f in page.frames if f.name == "content"][0]
            frame.get_by_role("textbox").first.fill("12345")
            frame.get_by_role("link", name="Search", exact=True).click()
            page.wait_for_timeout(600)
            index = next(i for i, n in enumerate(observe(page).addressable())
                         if n.name == on_screen)
            call = SimpleNamespace(id="t", name="read_value",
                                   input={"control": index, "name": out, "why": "the answer"})
            trajectory: list = []
            result = _perform(call, page, self.policy, trajectory, None, lambda _: None)
            self.assertTrue(result.startswith("read"), result)
            return trajectory
        finally:
            page.close()

    def test_a_read_carries_its_value_back(self) -> None:
        trajectory = self._read("4812.55", "savings_balance")
        # Without this the recorder types the output from an empty string.
        self.assertEqual("4812.55", trajectory[0].observed)

    def test_a_balance_is_declared_a_number(self) -> None:
        outputs = _outputs(self._read("4812.55", "savings_balance"))
        self.assertEqual(["savings_balance"], [o.name for o in outputs])
        self.assertEqual("number", outputs[0].type)
        # The sample is what lets a reviewer see what this returns without
        # running it, and it comes from the same value.
        self.assertIn("4812.55", outputs[0].description)

    def test_a_name_stays_a_string(self) -> None:
        # The conservative half. Guessing a type for something that is not
        # obviously one would make a working capability refuse its own answer.
        outputs = _outputs(self._read("Dolores Abernathy", "member_name"))
        self.assertEqual("string", outputs[0].type)
