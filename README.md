# agent-hands

Record a back-office web flow once with a model driving. Replay it thousands of
times with no model involved.

The bet is that exploration and execution are different problems. Exploration is
expensive, non-deterministic, and worth a person's attention. Execution is none
of those. So a model drives the application exactly once, at record time, and
what it produces is a reviewable artifact that a deterministic engine runs.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install playwright anthropic
.venv/bin/playwright install chromium
```

Recording needs `ANTHROPIC_API_KEY`. Replay does not, and that asymmetry is the
whole point.

## Run it

Start the fixture, a deliberately hostile legacy app (frameset, table layout, no
`<label>` elements, server-generated ids):

```bash
PYTHONPATH=. .venv/bin/python -m fixture.app --port 8899
```

Then:

```bash
export PYTHONPATH=.
alias agent-hands=".venv/bin/python -m agent_hands"

# 1. a model drives the app once and writes a draft artifact
agent-hands record --goal "Look up member 12345 and open their detail screen" \
                   --url http://127.0.0.1:8899/

# 2. read what it recorded, including the strategies it rejected and why
agent-hands show capabilities/member_lookup.json

# 3. sign it off. nothing replays until this happens
agent-hands approve capabilities/member_lookup.json

# 4. run it, with different inputs, no model
agent-hands replay capabilities/member_lookup.json --param member_id=12345
agent-hands replay capabilities/member_lookup.json --param member_id=99999
```

Exit codes distinguish the three outcomes the way the type does:
`0` success, `3` business outcome (the app answered, and the answer was "no"),
`1` failure.

```
member_id=12345  exit=0  success           flow completed
member_id=99999  exit=3  business_outcome  member_not_found
member_id=abcde  exit=3  business_outcome  invalid_identifier
```

## Layout

| file | what it does |
|---|---|
| `agent_hands/schema.py` | the capability artifact and the result contract |
| `agent_hands/perception.py` | accessibility-tree observation, ranked targeting |
| `agent_hands/discover.py` | the model-driven recording loop (the only LLM code) |
| `agent_hands/recorder.py` | trajectory to artifact, including parameterisation |
| `agent_hands/replay.py` | deterministic execution. imports nothing that reaches a model |
| `agent_hands/policy.py` | surface, action and risk gates; redaction |
| `agent_hands/escalation.py` | session ownership and the handoff to a person |
| `agent_hands/evidence.py` | per-run log, screenshots, accessibility snapshots |
| `fixture/app.py` | the target application, with switchable failure modes |

## The fixture

It is hostile on purpose, because the interesting problems only appear on a
surface that fights back:

- a **frameset**, so anything assuming one document per page sees nothing
- **table layout with no `<label>`**, so a field's caption is a `<td>` to its
  left and has to be recovered from geometry
- **server-generated ids** (`ctl00_r3_c1`) that change per render
- the submit control is an **`<a>` styled as a button**

Failure modes are switchable so replay can be driven into each branch:

```
?fail=notfound     the member does not exist   (a business outcome)
?fail=validation   the form rejects the input  (a business outcome)
?fail=denied       permission denied           (a hard failure)
?fail=timeout      session expired             (escalates; credentials are never handled)
?fail=slow         a 3 second stall            (recoverable)
?fail=dialog       an interstitial             (recoverable)
?fail=error        an unhandled app error      (a hard failure)
```

A second tenant runs the same vendor product with a different brand, a different
field caption and a different route:

```bash
PYTHONPATH=. .venv/bin/python -m fixture.app --port 8900 --variant westfield
```

## Evidence

Every run writes a directory: `run.jsonl` (append-only, flushed per line, so a
process killed mid-step still leaves what came before), `result.json`, and on
failure a screenshot paired with the accessibility snapshot. The screenshot is
what a person sees; the snapshot is what the targeting layer saw. Most confusing
failures are a disagreement between the two.

## Tests

```bash
PYTHONPATH=. .venv/bin/python tests/test_replay.py
```

Drives the engine against the live fixture and asserts the outcome of 16 cases:
success, both business outcomes, three recoverable conditions, three hard
failures, strategy degradation vs a genuinely stale recording, and four policy
refusals. It starts the fixture itself if one is not already running.

See `REPORT.md` for the design argument and what was deliberately left out.
