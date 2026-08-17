"""Command line entry points: record, review, approve, replay.

The commands are separate on purpose, and the separation is the argument. You
cannot record-and-run in one step, because approval sits between them and an
approval you can skip is not one. The sequence a person actually performs is:

    record   -- a model drives the app once, producing a draft artifact
    show     -- read what it recorded, including the strategies it rejected
    approve  -- sign it off; this is the only thing that makes it replayable
    replay   -- run it, repeatedly, with no model involved

`record` is the only command that needs an API key, and the only one that costs
anything per run. That asymmetry is the point of the whole system.
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

    Narrowed to the origin the artifact was recorded against. A capability is
    not a general-purpose browser, and the surface gate is what stops it
    becoming one when something later goes wrong.
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


def _console(unattended: bool) -> Any:
    """A terminal operator, or a refusal to guess when nobody is watching.

    The unattended console never answers RESUME and never approves. An automatic
    yes would leave the escalation path untested in exactly the way that
    matters, and would wave through the writes it exists to gate.
    """
    from .escalation import Decision
    if unattended:
        return ScriptedConsole(decision=Decision.ABORT, note="no operator available",
                               approves=False)
    return ConsoleOwner()


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_record(args: argparse.Namespace) -> int:
    from .discover import DiscoveryError, discover

    play, browser, page = _browser(args.headed)
    evidence = Evidence.start(f"record-{args.name or 'capability'}",
                              params={"goal": args.goal, "url": args.url})
    try:
        result = discover(
            goal=args.goal, entry_url=args.url, page=page,
            policy=_policy(args.url), app_id=args.app_id,
            app_variant=args.variant, evidence=evidence,
            on_step=lambda m: print(m, file=sys.stderr),
        )
    except DiscoveryError as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 2
    finally:
        browser.close(); play.stop()

    evidence.event("discovery_finished", ok=result.ok, turns=result.turns,
                   stopped_because=result.stopped_because)
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

    The rejected strategies are shown, not hidden. "Why is this matching by
    position?" is the first question a reviewer asks, and the answer -- the
    control has no accessible name -- is a fact about the application that they
    should see before they sign.
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
        if step.target:
            for i, cand in enumerate(step.target.candidates):
                mark = "->" if i == 0 else "  "
                print(f"        {mark} {cand.strategy.value:20} {cand.value!r} "
                      f"({cand.confidence:.2f}) {cand.note}")
            for rej in step.target.rejected:
                print(f"         x  {rej.strategy.value:20} {rej.value!r} -- {rej.note}")
        if step.checkpoint:
            print(f"        assert {step.checkpoint.kind}={step.checkpoint.value!r}")
        if step.extracts:
            print(f"        extracts -> {step.extracts}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Sign off an artifact so replay will run it.

    Approval is recorded against the artifact version. Any later edit bumps the
    version and clears it, because an approval that survives an edit approves
    something nobody read.
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
    escalator = Escalator(_console(args.unattended), evidence=evidence)
    policy = _policy(cap.entry_url, read_only=args.read_only)
    policy.approver = escalator

    try:
        result = Replay(page, policy, evidence=evidence, escalator=escalator).run(cap, params)
    finally:
        browser.close(); play.stop()

    print(json.dumps(result.to_json(), indent=2))
    # Exit codes distinguish the three outcomes, so a shell caller can branch on
    # them the same way the type does: 0 worked, 3 answered "no", 1 broke.
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
    rec.set_defaults(func=cmd_record)

    show = sub.add_parser("show", help="print an artifact for review")
    show.add_argument("capability")
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
