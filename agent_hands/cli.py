"""Command line entry points: record, review, approve, replay.

The commands are separate so a human approval sits between recording and
running. There is no record-and-run step, because an approval you can skip
does not gate anything. The usual sequence is:

    record   -- a model drives the app once, producing a draft artifact
    show     -- read what it recorded, including the strategies it rejected
    approve  -- sign it off; nothing replays until this happens
    replay   -- run it, repeatedly, with no model involved

Only `record` needs an API key, and only `record` costs anything per run. That
asymmetry is the point of the whole system.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .escalation import ConsoleOwner, Escalator, ScriptedConsole
from .evidence import Evidence
from .policy import AppAllowance, Policy
from .recorder import load, save
from .replay import Replay
from .schema import Capability, Outcome

DEFAULT_APP = "http://127.0.0.1:8899"


# --------------------------------------------------------------------------
# shared setup
# --------------------------------------------------------------------------

def _policy(entry_url: str, *, read_only: bool = False, unattended: bool = False) -> Policy:
    """The allowlist for a run.

    Narrowed to the origin the artifact was recorded against, so a capability
    cannot turn into a general-purpose browser when something later goes wrong.
    """
    from urllib.parse import urlparse
    host = urlparse(entry_url).netloc
    policy = Policy(apps=(AppAllowance(host=host),), name=host)
    if read_only:
        policy = policy.read_only()
    return policy


def _browser(headed: bool) -> Any:
    from playwright.sync_api import sync_playwright
    play = sync_playwright().start()
    browser = play.chromium.launch(headless=not headed)
    return play, browser, browser.new_page()


def _console(unattended: bool, *, headed: bool = False) -> Any:
    """A terminal operator, or a console that refuses to answer with nobody there.

    The unattended console never answers RESUME and never approves. Saying yes
    automatically would wave through the writes this gate exists to catch, and
    would leave the escalation path untested.
    """
    from .escalation import Decision
    if unattended:
        return ScriptedConsole(decision=Decision.ABORT, note="no operator available",
                               approves=False)
    return ConsoleOwner(headed=headed)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

class _WatchingOperator:
    """The person at the keyboard during a recording.

    Replay gates an irreversible step on the artifact having been reviewed and
    approved. At record time there is no artifact yet -- it is what the session
    is trying to produce -- so that gate refuses every write, and a transfer can
    never be recorded at all. The chicken-and-egg is real, and the way out is
    that the two moments are gated by different things: replay asks whether a
    reviewer signed the file, recording asks whether a person is watching.

    `--allow-writes` is that person saying yes, once, for this session, on the
    command line. Every approval it grants is printed, because a recording that
    quietly posted a transfer would be the worst version of this.
    """

    def __init__(self, say: Any) -> None:
        self.say = say
        self.granted: list[int] = []

    def approve(self, *, capability: str, step: Any) -> bool:
        self.granted.append(step.index)
        self.say(f"  ~ allowing an irreversible {step.action.value} "
                 f"because --allow-writes was given")
        return True


def cmd_record(args: argparse.Namespace) -> int:
    from .discover import DiscoveryError, discover

    say = lambda m: print(m, file=sys.stderr)          # noqa: E731
    policy = _policy(args.url)
    operator = None
    if args.allow_writes:
        operator = _WatchingOperator(say)
        policy.approver = operator
        say("recording with writes allowed: this session may post real transactions")

    play, browser, page = _browser(args.headed)
    evidence = Evidence.start(f"record-{args.name or 'capability'}",
                              params={"goal": args.goal, "url": args.url})
    try:
        result = discover(
            goal=args.goal, entry_url=args.url, page=page,
            policy=policy, app_id=args.app_id,
            app_variant=args.variant, evidence=evidence,
            **({"max_turns": args.max_turns} if args.max_turns else {}),
            on_step=say,
        )
    except DiscoveryError as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 2
    finally:
        browser.close(); play.stop()

    evidence.event("discovery_finished", ok=result.ok, turns=result.turns,
                   stopped_because=result.stopped_because,
                   writes_allowed=bool(operator and operator.granted))
    if not result.ok:
        print(f"\nno artifact produced: {result.stopped_because}", file=sys.stderr)
        print(f"evidence: {evidence.dir}", file=sys.stderr)
        return 1

    cap = result.capability
    assert cap is not None
    if args.name:
        cap.name = args.name
    out = Path(args.out or f"capabilities/{cap.name}.json")
    save(cap, out)
    print(f"\nrecorded {cap.name!r} in {result.turns} turns -> {out}")
    print(f"evidence: {evidence.dir}")
    print(f"\nthis is a DRAFT. review it with:  agent-hands show {out}")
    print(f"then approve it with:            agent-hands approve {out}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print the artifact the way a reviewer needs to read it.

    Two readers want different things. Whoever signs the artifact off needs to
    know what it does and what it cannot undo; the targeting is noise to them,
    so `--brief` drops it. Whoever debugs it later needs the ranking and the
    rejected strategies: "why is this matching by position?" is answered by
    "the control has no accessible name", a fact about the application rather
    than a shortcut the recorder took. That is the default view.
    """
    cap = load(args.capability)
    print(f"{cap.name}  v{cap.artifact_version}  "
          f"[{'APPROVED' if cap.approved else 'DRAFT'}]")
    print(f"  {cap.description}")
    print(f"  app      : {cap.app_id}" + (f" / {cap.app_variant}" if cap.app_variant else ""))
    print(f"  entry    : {cap.entry_url}")
    print(f"  recorded : {cap.recorded_by} {cap.recorded_at}")
    if cap.params:
        print("  params   : " + ", ".join(
            f"{p.name}:{p.type}{'' if p.required else '?'}" for p in cap.params))
    if cap.outputs:
        print("  outputs  : " + ", ".join(f"{o.name}:{o.type}" for o in cap.outputs))
    if cap.business_rules:
        print("  business : " + ", ".join(f"{b.code}<-{b.when_text!r}" for b in cap.business_rules))
    print()
    for step in cap.steps:
        head = f"  {step.index}. {step.action.value:9} [{step.risk.value}]"
        detail = step.url or step.text or ""
        print(f"{head} {detail}")
        if step.target and not args.brief:
            for i, cand in enumerate(step.target.candidates):
                mark = "->" if i == 0 else "  "
                print(f"        {mark} {cand.strategy.value:20} {cand.value!r} "
                      f"({cand.durability:.2f}) {cand.note}")
            for rej in step.target.rejected:
                print(f"         x  {rej.strategy.value:20} {rej.value!r} -- {rej.note}")
        if step.checkpoint:
            print(f"        assert {step.checkpoint.kind}={step.checkpoint.value!r}")
        if step.extracts:
            print(f"        extracts -> {step.extracts}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Sign off an artifact so replay will run it.

    Approval is recorded against the artifact version. A later edit bumps the
    version and clears the approval, so nobody replays a change no one read.
    """
    cap = load(args.capability)
    if cap.approved:
        print(f"{cap.name} v{cap.artifact_version} is already approved")
        return 0
    cap.approved = True
    cap.recorded_by = cap.recorded_by or "unknown"
    save(cap, args.capability)
    print(f"approved {cap.name} v{cap.artifact_version} -> {args.capability}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    cap = load(args.capability)
    params = dict(p.split("=", 1) for p in args.param)

    play, browser, page = _browser(args.headed)
    evidence = Evidence.start(cap.name, params=params)
    escalator = Escalator(_console(args.unattended, headed=args.headed), evidence=evidence)
    policy = _policy(cap.entry_url, read_only=args.read_only)
    policy.approver = escalator

    try:
        result = Replay(page, policy, evidence=evidence, escalator=escalator).run(cap, params)
    finally:
        browser.close(); play.stop()

    print(json.dumps(result.to_json(), indent=2))
    # Exit codes split the three outcomes so a shell caller can branch on them
    # like the Outcome type does: 0 worked, 3 answered "no", 1 broke.
    return {Outcome.SUCCESS: 0, Outcome.BUSINESS_OUTCOME: 3, Outcome.FAILURE: 1}[result.outcome]


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-hands",
        description="Record a back-office web flow once; replay it deterministically.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="drive the app with a model, produce a draft artifact")
    rec.add_argument("--goal", required=True, help="what to achieve, in plain language")
    rec.add_argument("--url", default=DEFAULT_APP, help="entry point")
    rec.add_argument("--app-id", default="meridian-core")
    rec.add_argument("--variant", default=None, help="tenant variant, if any")
    rec.add_argument("--name", default=None, help="override the derived capability name")
    rec.add_argument("--out", default=None)
    rec.add_argument("--headed", action="store_true", help="show the browser")
    rec.add_argument("--allow-writes", action="store_true",
                     help="let this recording perform irreversible actions. Without it a "
                          "write flow cannot be recorded, because the gate replay uses "
                          "asks for an approved artifact and there is not one yet")
    rec.add_argument("--max-turns", type=int, default=None,
                     help="how many turns the model gets; a long write flow needs more")
    rec.set_defaults(func=cmd_record)

    show = sub.add_parser("show", help="print an artifact for review")
    show.add_argument("capability")
    show.add_argument("--brief", action="store_true",
                      help="what it does and what it can undo, without the targeting detail")
    show.set_defaults(func=cmd_show)

    app = sub.add_parser("approve", help="mark an artifact replayable")
    app.add_argument("capability")
    app.set_defaults(func=cmd_approve)

    rep = sub.add_parser("replay", help="run an approved artifact; no model involved")
    rep.add_argument("capability")
    rep.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    rep.add_argument("--headed", action="store_true")
    rep.add_argument("--read-only", action="store_true",
                     help="run the same artifact with its writes disabled")
    rep.add_argument("--unattended", action="store_true",
                     help="no operator is watching; escalations abort rather than wait")
    rep.set_defaults(func=cmd_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
