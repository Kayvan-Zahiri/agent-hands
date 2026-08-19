# agent_hands

A map of this package. Nine files, and the thing worth knowing first is that
they split into two halves that never meet.

```
schema.py       the nouns          what a step, a target, a risk IS
                                   everything below imports it

── runs ONCE, with a model ────────────────────────────────────────────
perception.py   the eyes           reads the screen, never the HTML
discover.py     the brain          the only file that calls a model
recorder.py     the note-taker     writes down every way to find a control

── runs FOREVER, with no model ────────────────────────────────────────
replay.py       the driver         follows the notes and checks each step
escalation.py   the handover       gives the wheel to a person, takes it back

── used by both ───────────────────────────────────────────────────────
policy.py       the rulebook       allowlists, risk, approval
evidence.py     the camera         run.jsonl, result.json, screenshots
cli.py          the buttons        record / show / approve / replay
```

## The line between the halves

The split is between `recorder.py` and `replay.py`. Above it a model is
involved; below it, never. That is the design, not an optimisation, and it is
why `record` needs `ANTHROPIC_API_KEY` and `replay` does not.

It is also checkable rather than asserted:

```bash
grep -rn "anthropic\|ANTHROPIC" agent_hands/ | grep -v discover.py   # empty
```

If a change makes that command print something, replay can reach a model and the
central claim is gone.

## Why the sizes came out this way

`replay.py` (959 lines) and `perception.py` (813) are the two largest. Doing
something reliably ten thousand times is harder than doing it once, and reading
a screen well is harder than reading its markup. `discover.py`, the file that
actually talks to a model, is one of the smallest at 498, because the model is
given four tools and a numbered list and nothing else.

## Where to start reading

`schema.py`. It is the smallest of the substantive files and every other module
imports it, so nothing else parses properly until those types are familiar.
After that, follow a single run: `cli.py` → `replay.py` → `perception.py`.

`REPORT.md` in the repo root has the design rationale, and `README.md` has the
commands.
