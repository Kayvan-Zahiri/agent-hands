# agent-hands

Record a back-office web flow once with a model driving. Replay it thousands of
times with no model involved.

The bet is that exploration and execution are different problems. Exploration is
expensive, non-deterministic, and worth a person's attention. Execution is none
of those. So a model drives the application exactly once, at record time, and
what it produces is a reviewable artifact that a deterministic engine runs.

## Setup

Python 3.10 or newer.

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
#    the only step that needs ANTHROPIC_API_KEY
agent-hands record --goal "Look up member 12345 and open their detail screen" \
                   --url http://127.0.0.1:8899/ --out /tmp/demo.json

# 2. read what it recorded, including the strategies it rejected and why
agent-hands show /tmp/demo.json

# 3. sign it off. nothing replays until this happens
agent-hands approve /tmp/demo.json

# 4. run it, on a member the model never saw, with no model
agent-hands replay /tmp/demo.json --param member_id=30021
```

**No API key?** Skip step 1. `capabilities/member_lookup.json` is committed and
already approved, so the rest works against it unchanged:

```bash
agent-hands show    capabilities/member_lookup.json
agent-hands replay  capabilities/member_lookup.json --param member_id=12345
agent-hands replay  capabilities/member_lookup.json --param member_id=99999
```

Replay never reads the key. Unset it and everything below still runs.

Exit codes distinguish the three outcomes the way the type does:
`0` success, `3` business outcome (the app answered, and the answer was "no"),
`1` failure.

```
member_id=12345  exit=0  success           flow completed
member_id=99999  exit=3  business_outcome  member_not_found
member_id=abcde  exit=3  business_outcome  invalid_identifier
```

## The human handoff

When replay cannot safely continue, it stops and gives a person the *same* live
session, rather than failing or starting over. To see it, use `--headed` so there
is a window to hand over, and leave off `--unattended`, which is the flag that
says nobody is watching:

```bash
agent-hands replay capabilities/member_lookup_stale.json \
    --param member_id=12345 --headed
```

`member_lookup_stale.json` is a recording that no longer matches the app: the
submit control was renamed. It stops at step 2 and prints:

```
HANDOFF int-...  (unresolved_element)
  capability : member_lookup_stale
  step       : 2 (click)
  expected   : role_name='link "Submit Query"'
  screenshot : evidence/.../int-...-before.png

  the browser is yours. fix the page, then:  [r]esume  [a]bort
  >
```

Fix the page in that window, then answer `r` and say what you did. Headless
refuses to hand over rather than pretending it can:

```
the browser is headless, so there is no window to fix this in.
```

Handing control back is not automatic. The engine re-confirms the last screen it
knew it was on before it will act again:

| you do | what happens |
|---|---|
| fix the page, leave it where it was | resume verified, the step is retried |
| finish the step yourself | refused: *page moved during handoff* |
| anything, on any artifact in `capabilities/` | refused: *no confirmed checkpoint* |

The last row is a real limitation, not a quirk. Recording attaches a check to the
**final** step only, so when an earlier step fails there is nothing confirmed to
compare against. `REPORT.md` covers it under Cuts. All three rows are asserted in
`tests/test_replay.py` under "escalation, resume and handback", which is the
place to look for the working case without driving a browser by hand.

## Layout

| file | what it does |
|---|---|
| `agent_hands/schema.py` | the capability artifact and the result contract |
| `agent_hands/perception.py` | accessibility-tree observation, ranked targeting |
| `agent_hands/discover.py` | the model-driven recording loop (the only LLM code) |
| `agent_hands/recorder.py` | trajectory to artifact, including parameterization |
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
?fail=dialog       an interstitial that resumes the request      (recoverable)
?fail=dialog_lossy an interstitial that discards it             (recoverable, but
                                                only by re-entering the flow)
?fail=error        an unhandled app error      (a hard failure)
```

The two interstitials are separate modes because they need opposite recoveries.
One can be dismissed and the step continued; the other has taken the typed input
with it, so continuing would submit an empty form.

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

114 in total, all against the real fixture with a real browser. No mocked pages:
every bug worth finding here was a disagreement between what the code assumed a
page would do and what it did.

```bash
# 82 unit tests: perception (needs the fixture up), policy, escalation
PYTHONPATH=. .venv/bin/python -m unittest tests.test_perception tests.test_policy_escalation

# 10 more, covering the CLI
PYTHONPATH=. .venv/bin/python -m unittest tests.test_cli

# 22 end-to-end behaviors; starts its own fixture if one is not running
PYTHONPATH=. .venv/bin/python tests/test_replay.py
```

`tests/test_replay.py` collects nothing under pytest. Those 22 cases run only
when the file is called directly, as above.

| suite | covers |
|---|---|
| `test_perception.py` | frame crossing, the unnamed field, caption-not-id targeting, ambiguity demotion, `POINT` never offered, the second tenant |
| `test_policy_escalation.py` | surface/action/risk gates, redaction, ownership transitions, resume re-verification |
| `test_replay.py` | the three outcomes, recovery, degradation vs staleness, policy refusals |

The one to read is `test_replay.py`. It asserts *which* of the three outcomes
came back, not just that the run completed, and every real bug in this project
was a classification bug that only that catches.

See `REPORT.md` for the design argument and what was deliberately left out.
