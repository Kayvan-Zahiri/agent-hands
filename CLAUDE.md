# agent-hands

**Read `SUMMARY.md` first.** It is the map: modules, data model, how to run,
bugs already found, and where the code is meant to be extended. `README.md` is
usage, `REPORT.md` is the design rationale.

## The invariant, before anything else

A model drives the app **once** at record time and writes a reviewable artifact.
A deterministic engine replays it **with no model in the loop**. Recording needs
`ANTHROPIC_API_KEY`; replay must never need it.

    grep -rn "anthropic\|ANTHROPIC" agent_hands/*.py | grep -v discover.py   # must stay empty

`discover.py` is the only module allowed to import `anthropic`. If a change makes
replay depend on a model, the whole design claim is gone. Check this before
proposing anything that touches `replay.py`.

Also load-bearing: the model picks a control **number**, never a selector; and
`POINT` (screenshot coordinates) is recorded for human review and never resolved
at replay.

## Running it

    PYTHONPATH=. .venv/bin/python -m pytest tests -q       # 80 unit
    PYTHONPATH=. .venv/bin/python tests/test_replay.py     # 16 e2e, real browser
    pkill -f "fixture.app"; sleep 1        # a stale one holds the port
    PYTHONPATH=. .venv/bin/python -m fixture.app --port 8899 &
    PYTHONPATH=. .venv/bin/python -m fixture.app --port 8900 --variant westfield &

Both suites start their own fixture. Replay needs one on **8899** specifically,
because the artifact hardcodes that `entry_url`. The venv is already built.

## House style

Comments earn their line; match the surrounding density. Tests use a real browser
and the real fixture, never a mocked page. Frame-scoped checkpoints, never
page-wide text assertions. Condition-based waits, never sleeps. Keep diffs
minimal and run both suites before claiming anything works.
