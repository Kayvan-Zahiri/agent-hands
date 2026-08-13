"""What the agent can see, and how it names what it sees.

Everything here is built on the accessibility tree rather than on CSS. The
reason is not purity. A CSS selector encodes how a page was *built*: which
wrapper divs the templating engine emitted, which class names the stylesheet
happened to need, which server-generated id came back this render. An
accessibility node encodes what a control *is* -- a textbox whose caption reads
"Member ID" -- which is the thing that stays true when the vendor reskins the
app. It is also what a screen reader consumes, so it is a view the application
is already obliged to keep honest, and the same abstraction exists on native
desktop surfaces (UIA on Windows, AX on macOS), so the targeting vocabulary
carries over to the green-screen and thick-client half of this problem.

Three functions, in the order they are used:

    observe(page)                -> Observation   what is on screen, per frame
    derive_targets(node, frame)  -> TargetSet     every way to find one node
    resolve(page, target_set)    -> Locator       walk the ways until one holds

The awkward case this layer exists for is the fixture's search field. It has no
<label>, no name, no stable id, and no accessible name of any kind -- its
caption is a <td> sitting to the left of it in a layout table. So the
accessibility tree alone cannot name it, and `LABELLED_FIELD` has to recover the
label from table geometry. See `_labelled_field`.

The handles in an Observation (`Node.ref`) are valid only for that observation.
The durable handle is the TargetSet, which is the whole point of recording one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page

from .schema import Strategy, Target, TargetSet

log = logging.getLogger(__name__)

# How long any single probe is allowed to block. Perception runs against a page
# the caller has already decided is settled, so waiting here hides bugs rather
# than fixing them.
PROBE_TIMEOUT_MS = 1_000

# Roles that carry no meaning of their own. Keeping them would triple the size
# of an observation without telling a reader or a planner anything.
_STRUCTURAL = frozenset({"generic", "none", "presentation", "rowgroup", "list"})

# Roles a person can act on. Anything in here is kept even with no name, because
# an unnamed control is exactly the case worth showing a human.
_INTERACTIVE = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "switch", "slider", "spinbutton", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab", "treeitem",
})

# Controls a caption can sit beside in a layout table.
_FIELD_ROLES = frozenset({
    "textbox", "searchbox", "combobox", "listbox", "checkbox", "radio",
    "spinbutton", "slider",
})

# Roles Playwright's role engine will accept in get_by_role. The snapshot can
# emit tokens outside this set ("text", "iframe"), and asking for those raises
# rather than returning nothing, so they are filtered before use.
_QUERYABLE_ROLES = _INTERACTIVE | frozenset({
    "heading", "cell", "columnheader", "rowheader", "row", "table", "grid",
    "gridcell", "img", "paragraph", "region", "form", "navigation", "main",
    "banner", "contentinfo", "dialog", "alert", "status", "document",
})


# --------------------------------------------------------------------------
# observation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Node:
    """One accessibility node, flattened out of the tree.

    `ref` is Playwright's aria-ref handle. It addresses this element for as long
    as the page is not re-rendered, which is long enough to derive targets from
    and no longer.
    """

    role: str
    name: str
    ref: str
    frame: str
    depth: int
    text: str = ""

    @property
    def interactive(self) -> bool:
        return self.role in _INTERACTIVE

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role, "name": self.name, "ref": self.ref,
            "frame": self.frame, "depth": self.depth, "text": self.text,
        }


@dataclass
class Observation:
    """A whole-page accessibility view, frames included.

    `frames` maps the key stamped on each node back to the live Frame. Callers
    should go through `frame_for`, so that a frame disappearing between observe
    and use is a handled `None` rather than a KeyError three layers down.
    """

    url: str
    title: str
    nodes: list[Node]
    frames: dict[str, Frame] = field(default_factory=dict)
    captured_at: str = ""
    # Lines the snapshot parser did not understand. Non-zero means Playwright
    # changed its snapshot format, which is a thing to notice, not to crash on.
    unparsed: int = 0

    def frame_for(self, node: Node) -> Frame | None:
        return self.frames.get(node.frame)

    def find(self, *, role: str | None = None, name: str | None = None,
             frame: str | None = None) -> list[Node]:
        """Nodes matching every supplied criterion. Name match is exact."""
        return [
            n for n in self.nodes
            if (role is None or n.role == role)
            and (name is None or n.name == name)
            and (frame is None or n.frame == frame)
        ]

    def interactive(self) -> Iterator[Node]:
        return (n for n in self.nodes if n.interactive)

    def render(self) -> str:
        """The compact human view. This is what goes in an evidence bundle."""
        head = (f"{self.url}  {self.title!r}  "
                f"{len(self.frames)} frame(s), {len(self.nodes)} node(s)")
        lines = [head]
        current = None
        for n in self.nodes:
            if n.frame != current:
                current = n.frame
                lines.append(f"[frame {current}]")
            bits = [" " * (n.depth * 2) + n.role]
            if n.name:
                bits.append(f'"{n.name}"')
            if n.text and n.text != n.name:
                bits.append(f"= {n.text!r}")
            if n.interactive and not n.name:
                bits.append("<- no accessible name")
            lines.append("  " + " ".join(bits))
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url, "title": self.title, "captured_at": self.captured_at,
            "frames": sorted(self.frames), "unparsed": self.unparsed,
            "nodes": [n.to_json() for n in self.nodes],
        }


def observe(page: Page) -> Observation:
    """Snapshot the accessibility tree of the page and every frame in it.

    One snapshot call covers the whole frame tree: Playwright descends through
    iframe and frame elements and prefixes each frame's refs. That matters here
    because the fixture is a frameset -- the main document contains nothing but
    two frames, and a one-document view of it is empty.
    """
    raw = page.locator(":root").aria_snapshot(mode="ai", timeout=PROBE_TIMEOUT_MS)
    nodes, unparsed, frames = _parse_snapshot(page, raw)
    return Observation(
        url=page.url,
        title=page.title(),
        nodes=nodes,
        frames=frames,
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        unparsed=unparsed,
    )


# Playwright emits the ARIA tree as indented YAML-ish lines:
#   - cell "Member ID" [ref=f2e16]
#   - generic [ref=f2e11]: Member Search
#   - link "Search" [ref=f2e22] [cursor=pointer]:
#       - /url: "#"
_LINE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_NODE = re.compile(
    r"""^(?P<role>[A-Za-z][A-Za-z0-9_-]*)      # role token
        (?:\ "(?P<name>(?:[^"\\]|\\.)*)")?     # optional quoted accessible name
        (?P<attrs>(?:\ \[[^\]]*\])*)           # [ref=..] [active] [cursor=..]
        (?::(?P<inline>.*))?$                  # optional inline text after ':'
    """,
    re.VERBOSE,
)
_REF = re.compile(r"\[ref=([^\]]+)\]")


def _parse_snapshot(page: Page, raw: str) -> tuple[list[Node], int, dict[str, Frame]]:
    """Flatten the snapshot text, attributing each node to the frame it lives in.

    Frame attribution comes from the tree shape, not from the ref prefix:
    everything indented under an `iframe` node belongs to that iframe's content
    frame. A stack of (depth, frame key) tracks it, so nesting works to any
    depth without the parser knowing how Playwright numbers its refs.
    """
    main_key = frame_key(page.main_frame)
    frames: dict[str, Frame] = {main_key: page.main_frame}
    stack: list[tuple[int, str]] = [(-1, main_key)]
    # Depth is reported relative to each frame's own root, so a control three
    # frames deep still reads as top level in the frame a person is looking at.
    base: dict[str, int] = {main_key: 0}
    nodes: list[Node] = []
    unparsed = 0

    for line in raw.splitlines():
        m = _LINE.match(line)
        if not m:
            unparsed += 1 if line.strip() else 0
            continue
        body = m.group("body")
        if body.startswith("/"):
            continue  # a property line such as "/url: /members/search"
        node_m = _NODE.match(body)
        if not node_m:
            unparsed += 1
            continue

        raw_depth = len(m.group("indent")) // 2
        while stack and stack[-1][0] >= raw_depth:
            stack.pop()
        key = stack[-1][1]
        ref_m = _REF.search(node_m.group("attrs") or "")
        node = Node(
            role=node_m.group("role"),
            name=_unescape(node_m.group("name") or ""),
            ref=ref_m.group(1) if ref_m else "",
            frame=key,
            depth=max(0, raw_depth - base.get(key, 0)),
            text=_clean_inline(node_m.group("inline") or ""),
        )
        if node.role == "iframe" and node.ref:
            child = _content_frame(page, node.ref)
            if child is not None:
                child_key = frame_key(child)
                frames[child_key] = child
                base[child_key] = raw_depth + 1
                stack.append((raw_depth, child_key))
        if _keep(node):
            nodes.append(node)

    return nodes, unparsed, frames


def _keep(node: Node) -> bool:
    """Interactive or text-bearing, per the brief. Pure scaffolding is dropped."""
    if node.role in _STRUCTURAL and not node.name and not node.text:
        return False
    return bool(node.interactive or node.name or node.text or node.role == "iframe")


def _unescape(name: str) -> str:
    return name.replace('\\"', '"').replace("\\\\", "\\")


def _clean_inline(inline: str) -> str:
    text = inline.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = _unescape(text[1:-1])
    return " ".join(text.split())


def _content_frame(page: Page, ref: str) -> Frame | None:
    try:
        handle = page.locator(f"aria-ref={ref}").element_handle(timeout=PROBE_TIMEOUT_MS)
        return handle.content_frame() if handle else None
    except PlaywrightError as exc:  # a frame that went away mid-snapshot
        log.debug("could not enter frame at %s: %s", ref, exc)
        return None


def frame_key(frame: Frame) -> str:
    """A name for a frame that a human can read in an artifact.

    Frame names are the app's own vocabulary ("nav", "content") and legacy
    framesets almost always set them, because their own links target them.
    Where there is no name, the URL path is the next most stable thing; the
    object identity is not, since it changes every navigation.
    """
    if frame.parent_frame is None:
        return "main"
    if frame.name:
        return frame.name
    path = frame.url.split("?", 1)[0].rsplit("/", 1)[-1]
    return f"frame:{path or 'unnamed'}"


def frame_by_key(page: Page, key: str | None) -> Frame | None:
    """Find the frame an artifact named. None means it is not on the page now."""
    if key is None or key == "main":
        return page.main_frame
    for frame in page.frames:
        if frame_key(frame) == key:
            return frame
    return None


# --------------------------------------------------------------------------
# targeting
# --------------------------------------------------------------------------

class PerceptionError(RuntimeError):
    """The page did not contain what the observation said it contained."""


# Each proposal returns (target, reason-it-was-not-produced). Exactly one of the
# two is set, which keeps the rejection note next to the code that knows why.
# The reason is prefixed with its strategy name so the rejection can be filed
# without the caller having to remember which proposal it came from.
_Proposal = tuple[Target | None, str]

_REASON_STRATEGY = {
    "role_name": Strategy.ROLE_NAME,
    "role_name_in_region": Strategy.ROLE_NAME_IN_REGION,
    "labelled_field": Strategy.LABELLED_FIELD,
    "nth_of_role": Strategy.NTH_OF_ROLE,
    "dom_path": Strategy.DOM_PATH,
}


def derive_targets(node: Node, frame: Frame) -> TargetSet:
    """Every way this node could be found, ranked, with the failures kept.

    Each candidate is *checked against the live page before it is recorded*: it
    must match exactly one element, and that element must be this node. A
    strategy that matched nothing, matched several things, or matched the wrong
    thing goes into `rejected` with the count that disqualified it. Recording an
    unverified fallback is worse than recording none, because replay will take
    it and quietly act on the wrong control.
    """
    element = _element(frame, node.ref)
    if element is None:
        raise PerceptionError(f"node {node.ref} is no longer on the page")

    proposals = [
        _role_name(node),
        _role_name_in_region(node),
        _labelled_field(node, element),
        _nth_of_role(node, frame, element),
        _dom_path(node, element),
    ]
    candidates: list[Target] = []
    rejected: list[Target] = []
    for target, reason in proposals:
        if target is None:
            rejected.append(_stub(reason))
            continue
        verdict = _verify(frame, target, element)
        (candidates if verdict is None else rejected).append(
            target if verdict is None else _demote(target, verdict))

    candidates.sort(key=lambda t: t.confidence, reverse=True)

    # Coordinates are not a fallback, they are a confession. They are only worth
    # writing down when nothing else held, so that a reviewer opening the
    # artifact sees the control was findable no other way.
    point = _point(node, element)
    if point is not None:
        (candidates if not candidates else rejected).append(point)

    if not candidates:
        raise PerceptionError(
            f"no strategy identified {node.role} {node.name!r} in frame {node.frame}")
    return TargetSet(candidates=candidates, rejected=rejected)


def _stub(reason: str) -> Target:
    """A rejected target for a strategy that could not even produce a value."""
    key, _, note = reason.partition(": ")
    return Target(strategy=_REASON_STRATEGY[key], value="", confidence=0.0, note=note)


def _demote(target: Target, verdict: str) -> Target:
    return Target(strategy=target.strategy, value=target.value, frame=target.frame,
                  confidence=0.0, note=verdict)


def _role_name(node: Node) -> _Proposal:
    """Role plus accessible name, searched across every frame on the page.

    Frame-independent on purpose: when a vendor moves a control between frames,
    this is the candidate that still finds it. It only survives verification
    when the name is unique page-wide, which is the condition that makes
    ignoring the frame safe.
    """
    if node.role not in _QUERYABLE_ROLES:
        return None, f"role_name: role {node.role!r} is not addressable by role"
    if not node.name:
        return None, "role_name: control has no accessible name"
    return Target(
        strategy=Strategy.ROLE_NAME,
        value=_role_value(node),
        frame=None,
        confidence=0.95,
        note="unique by role and name across the whole page",
    ), ""


def _role_name_in_region(node: Node) -> _Proposal:
    """The same query, pinned to the frame it was recorded in.

    In a frameset the frame name is part of the application's structure, not an
    implementation detail, so pinning it is cheap insurance against a duplicate
    name appearing elsewhere later. The natural extension is a sub-frame region
    -- a named panel or fieldset -- which the same value grammar would carry.
    """
    if node.role not in _QUERYABLE_ROLES:
        return None, f"role_name_in_region: role {node.role!r} is not addressable by role"
    if not node.name:
        return None, "role_name_in_region: control has no accessible name"
    return Target(
        strategy=Strategy.ROLE_NAME_IN_REGION,
        value=_role_value(node),
        frame=node.frame,
        confidence=0.9,
        note=f"role and name, scoped to frame {node.frame!r}",
    ), ""


def _labelled_field(node: Node, element: Any) -> _Proposal:
    """The input whose caption sits in the cell to its left.

    This is the strategy the fixture exists to force. The field has no <label>,
    no title, no aria-label and no placeholder, so it has no accessible name at
    all -- the accessibility tree reports a bare `textbox`. What a person reads
    as its label is a sibling <td> to the left of it in a layout table, and that
    is a fact about reading order rather than about markup.

    So the caption is recovered from table geometry: walk up to the containing
    cell, take the cells before it in the same row, and use the nearest one with
    text. Left first because that is the dominant convention in this generation
    of app; the cell directly above is the fallback for stacked forms, and the
    direction that won is written into the note so a reviewer can see which
    assumption the artifact is resting on.

    What survives here is the caption string, which is exactly what a human
    would say the field is, and what a re-render of the page cannot change
    without also changing what the screen means.
    """
    if node.role not in _FIELD_ROLES:
        return None, f"labelled_field: role {node.role!r} is not a form field"
    found = element.evaluate(_JS_CAPTION)
    if not found:
        return None, "labelled_field: no caption cell to the left of or above the field"
    caption, direction = found["caption"], found["direction"]
    return Target(
        strategy=Strategy.LABELLED_FIELD,
        value=caption,
        frame=node.frame,
        # Below role+name because a real accessible name is checked by the
        # browser; above everything else because a caption is what the screen
        # means, and a person would notice if it changed.
        confidence=0.85,
        note=f"caption cell {direction} the field in the layout table",
    ), ""


def _nth_of_role(node: Node, frame: Frame, element: Any) -> _Proposal:
    """Position among same-role controls in the frame.

    Survives a control being renamed. Does not survive one being inserted before
    it, which is why it sits below everything that knows what the control is.
    """
    if node.role not in _QUERYABLE_ROLES:
        return None, f"nth_of_role: role {node.role!r} is not addressable by role"
    peers = frame.get_by_role(node.role)  # type: ignore[arg-type]
    total = peers.count()
    index = next((i for i in range(total) if _same(frame, peers.nth(i), element)), None)
    if index is None:
        return None, "nth_of_role: control is not reachable by its role in this frame"
    return Target(
        strategy=Strategy.NTH_OF_ROLE,
        value=f"{node.role}[{index}]",
        frame=node.frame,
        confidence=0.4,
        note=f"index {index} of {total} {node.role} controls in the frame; "
             f"breaks if the page reorders",
    ), ""


def _dom_path(node: Node, element: Any) -> _Proposal:
    """Structural path. Recorded because it is sometimes the only thing left."""
    path = element.evaluate(_JS_DOM_PATH)
    if not path:
        return None, "dom_path: element has no addressable ancestry"
    return Target(
        strategy=Strategy.DOM_PATH,
        value=path,
        frame=node.frame,
        confidence=0.2,
        note="structural path; any layout change invalidates it",
    ), ""


def _point(node: Node, element: Any) -> Target | None:
    box = element.bounding_box()
    if not box:
        return None
    x = round(box["x"] + box["width"] / 2)
    y = round(box["y"] + box["height"] / 2)
    return Target(
        strategy=Strategy.POINT,
        value=f"{x},{y}",
        frame=node.frame,
        confidence=0.05,
        note="viewport coordinates; not resolvable, recorded for human review only",
    )


def _role_value(node: Node) -> str:
    """The value grammar for the two role strategies: `link "Search"`."""
    return f'{node.role} "{node.name}"'


def _verify(frame: Frame, target: Target, element: Any) -> str | None:
    """None if the target holds. Otherwise the reason it does not, for `rejected`."""
    locator, _count, reason = _match(frame.page, target)
    if locator is None:
        return reason
    if not _same(frame, locator, element):
        return "matched a different element than the one observed"
    return None


def _element(frame: Frame, ref: str) -> Any | None:
    if not ref:
        return None
    try:
        return frame.locator(f"aria-ref={ref}").element_handle(timeout=PROBE_TIMEOUT_MS)
    except PlaywrightError:
        return None


def _same(frame: Frame, locator: Locator, element: Any) -> bool:
    """Identity, not equality. Two locators can both match and still disagree."""
    try:
        other = locator.element_handle(timeout=PROBE_TIMEOUT_MS)
    except PlaywrightError:
        return False
    if other is None:
        return False
    try:
        return bool(frame.evaluate("([a, b]) => a === b", [element, other]))
    except PlaywrightError:
        return False


_JS_CAPTION = """
el => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const cell = el.closest('td, th');
  if (!cell) return null;
  const row = cell.closest('tr');
  if (!row) return null;
  const cells = [...row.children].filter(c => /^(TD|TH)$/.test(c.tagName));
  const i = cells.indexOf(cell);

  // Left first: the caption is whichever earlier cell in this row has text.
  for (let j = i - 1; j >= 0; j--) {
    const t = norm(cells[j].innerText);
    if (t) return { caption: t, direction: 'left of' };
  }

  // Stacked forms put the caption in the same column one row up.
  const prev = row.previousElementSibling;
  if (prev) {
    const above = [...prev.children].filter(c => /^(TD|TH)$/.test(c.tagName))[i];
    const t = norm(above && above.innerText);
    if (t) return { caption: t, direction: 'above' };
  }
  return null;
}
"""

_JS_DOM_PATH = """
el => {
  const parts = [];
  let n = el;
  while (n && n.nodeType === 1 && n.tagName !== 'HTML') {
    const tag = n.tagName.toLowerCase();
    const parent = n.parentElement;
    const sibs = parent ? [...parent.children].filter(c => c.tagName === n.tagName) : [];
    const nth = ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')';
    parts.unshift(sibs.length > 1 ? tag + nth : tag);
    n = parent;
  }
  return parts.join(' > ');
}
"""


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

@dataclass
class Attempt:
    strategy: Strategy
    value: str
    frame: str | None
    # How many elements the target matched. None when the question could not be
    # asked -- a missing frame is not the same failure as an empty page.
    matched: int | None
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value, "value": self.value,
            "frame": self.frame, "matched": self.matched, "reason": self.reason,
        }


@dataclass
class Resolution:
    """Which candidate won, and what the ones before it did.

    Replay writes this into the evidence bundle. "Found by DOM path on the third
    try" is the early warning that the app has drifted, and it is only visible
    if the losers are kept.
    """

    locator: Locator
    winner: Target
    rank: int
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """True when the primary strategy did not hold. Worth surfacing."""
        return self.rank > 0

    def to_json(self) -> dict[str, Any]:
        return {
            "winner": self.winner.to_json(), "rank": self.rank,
            "degraded": self.degraded,
            "attempts": [a.to_json() for a in self.attempts],
        }


class TargetResolutionError(LookupError):
    """No candidate resolved. Carries the attempt log so a human can read it."""

    def __init__(self, target_set: TargetSet, attempts: list[Attempt]) -> None:
        self.target_set = target_set
        self.attempts = attempts
        lines = [f"  {a.strategy.value} {a.value!r} -> {a.reason}" for a in attempts]
        super().__init__("no target resolved:\n" + "\n".join(lines))


def resolve(page: Page, target_set: TargetSet) -> Locator:
    """The first candidate that matches exactly one element, best first."""
    return resolve_detail(page, target_set).locator


def resolve_detail(page: Page, target_set: TargetSet) -> Resolution:
    """As `resolve`, but returns the attempt log with it.

    Kept separate so the common call site stays a one-liner while replay, which
    has to justify itself afterwards, can have the whole story.
    """
    attempts: list[Attempt] = []
    for rank, target in enumerate(target_set.candidates):
        locator, count, reason = _match(page, target)
        attempts.append(Attempt(target.strategy, target.value, target.frame,
                                count, reason))
        if locator is not None:
            if rank > 0:
                log.warning("target degraded to %s (%r); primary was %s",
                            target.strategy.value, target.value,
                            target_set.primary.strategy.value)
            return Resolution(locator=locator, winner=target, rank=rank, attempts=attempts)
    raise TargetResolutionError(target_set, attempts)


def _match(page: Page, target: Target) -> tuple[Locator | None, int | None, str]:
    """Count what a target matches across every place it could match.

    Counting is summed rather than short-circuited on purpose. A target that
    matches one element in the content frame and one more in a frame nobody was
    looking at is ambiguous, and acting on the first is how automation clicks
    the wrong Delete button.

    A count of None means the question could not be asked at all -- the frame is
    gone, or the target is not expressible as a locator. That is a different
    fact from "asked, and nothing matched", and replay treats it differently.
    """
    try:
        locators = locators_for(page, target)
    except PerceptionError as exc:
        return None, None, str(exc)
    if not locators:
        return None, None, f"frame {target.frame!r} is not present"
    hit: Locator | None = None
    total = 0
    for locator in locators:
        try:
            count = locator.count()
        except PlaywrightError as exc:
            return None, None, str(exc).splitlines()[0]
        total += count
        if count == 1:
            hit = locator
    if total == 1 and hit is not None:
        return hit, 1, "matched exactly one"
    if total == 0:
        return None, 0, "matched nothing"
    return None, total, f"ambiguous: matched {total} elements"


def locators_for(page: Page, target: Target) -> list[Locator]:
    """Every locator this target could mean -- at most one per frame.

    A Playwright locator never crosses a frame boundary, so "find this control
    wherever it now lives" cannot be one locator; it is one per frame, counted
    together. `frame is None` is what says the target is not pinned to a frame.
    """
    if target.strategy is Strategy.ROLE_NAME and target.frame is None:
        role, name = _parse_role_value(target.value)
        return [f.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
                for f in page.frames]
    frame = frame_by_key(page, target.frame)
    if frame is None:
        return []
    return [locator_in(frame, target)]


def locator_in(frame: Frame, target: Target) -> Locator:
    """Turn one recorded target into a Locator inside one known frame."""
    if target.strategy in (Strategy.ROLE_NAME, Strategy.ROLE_NAME_IN_REGION):
        role, name = _parse_role_value(target.value)
        return frame.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
    if target.strategy is Strategy.LABELLED_FIELD:
        return frame.locator(f"xpath={_caption_xpath(target.value)}")
    if target.strategy is Strategy.NTH_OF_ROLE:
        role, index = _parse_nth_value(target.value)
        return frame.get_by_role(role).nth(index)  # type: ignore[arg-type]
    if target.strategy is Strategy.DOM_PATH:
        return frame.locator(target.value)
    if target.strategy is Strategy.POINT:
        # Deliberate. Clicking a coordinate is a different action, not a
        # different locator, and it needs a human to authorise it.
        raise PerceptionError("point targets are not resolvable to an element")
    raise PerceptionError(f"unknown strategy {target.strategy!r}")


_ROLE_VALUE = re.compile(r'^(?P<role>[A-Za-z][A-Za-z0-9_-]*) "(?P<name>.*)"$', re.S)
_NTH_VALUE = re.compile(r"^(?P<role>[A-Za-z][A-Za-z0-9_-]*)\[(?P<index>\d+)\]$")


def _parse_role_value(value: str) -> tuple[str, str]:
    m = _ROLE_VALUE.match(value)
    if not m:
        raise PerceptionError(f"malformed role target {value!r}")
    return m.group("role"), _unescape(m.group("name"))


def _parse_nth_value(value: str) -> tuple[str, int]:
    m = _NTH_VALUE.match(value)
    if not m:
        raise PerceptionError(f"malformed nth target {value!r}")
    return m.group("role"), int(m.group("index"))


def _caption_xpath(caption: str) -> str:
    """The caption strategy, expressed as a locator rather than a DOM walk.

    XPath because it can say "the cell whose text is X, then the control in the
    next cell that has one" in one expression, with no page mutation and no
    handle round-trip. `normalize-space` is what makes it survive the <font>
    tags and the whitespace this generation of markup is full of.

    Both reading directions are unioned rather than tried in order. If a page is
    laid out so that both the cell to the left and the row above name the same
    field, the layout is genuinely ambiguous, and two matches means the target
    is rejected rather than guessed.
    """
    lit = _xpath_literal(caption)
    cell = "*[self::td or self::th]"
    control = "*[self::input or self::select or self::textarea]"
    has_control = "[.//input or .//select or .//textarea]"
    anchor = f"//{cell}[normalize-space(.)={lit}]"
    left = f"{anchor}/following-sibling::{cell}{has_control}[1]//{control}"
    above = f"{anchor}/ancestor::tr[1]/following-sibling::tr[1]//{control}"
    return f"{left} | {above}"


def _xpath_literal(text: str) -> str:
    """XPath 1.0 has no escape character, so a quote has to be concat()ed in."""
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    parts = text.split('"')
    return "concat(" + ", '\"', ".join(f'"{p}"' for p in parts) + ")"
