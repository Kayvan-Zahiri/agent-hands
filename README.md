# agent-hands

Record a back-office web flow once with a model driving. Replay it thousands of
times with no model involved. Pointed at a real credit-union servicing console
with no API, where the only way in is the screen.

The bet is that exploration and execution are different problems. Exploration is
expensive, non-deterministic, and worth a person's attention. Execution is none
of those. So a model drives the application exactly once, at record time, and
what it produces is a reviewable artifact that a deterministic engine runs.

## Quickstart

**Every command is in [RUN.md](RUN.md).** The short version, from a clean
clone, Python 3.10 or newer:

```bash
git clone https://github.com/Kayvan-Zahiri/agent-hands.git
cd agent-hands

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

export PYTHONPATH=.

# the demo console -- open http://127.0.0.1:8080/ when it starts
.venv/bin/python -m agent_hands.api --port 8080 --headed \
  --confirmable meridian_signon_recorded,meridian_inquiry_recorded,\
meridian_balance_recorded,meridian_transfer_recorded,\
meridian_open_share_recorded,meridian_update_recorded,meridian_place_hold_recorded
```

That needs **no credentials**. It drives the hosted target and replays recorded
capabilities against it. In the console: **Jobs** → pick one → **Overview** →
*Run the recording*.

To also use *Run with the AI*, which records a fresh capability by letting a
model drive the application, add a key first:

```bash
cp .env.example .env          # put your key in it
set -a; . ./.env; set +a      # then start the console as above
```

The tests, which also need no credentials:

```bash
export PYTHONPATH=.
.venv/bin/python -m pytest tests -q        # 130
.venv/bin/python tests/test_replay.py      # 31, real browser
```

And one capability from the command line:

```bash
.venv/bin/python -m agent_hands replay capabilities/meridian/meridian_balance_recorded.json \
  --param operator=teller1 --param password=password --param branch=MAIN-001 \
  --param member_id=103001 --unattended
```

Recording needs `ANTHROPIC_API_KEY`. Replay does not, and that asymmetry is the
whole point. Put the key in `.env` (gitignored; see `.env.example`) and load it
only for the command that needs it:

```bash
set -a; . ./.env; set +a
```

## The demo, against the hosted target

Everything below runs against **MERIDIAN CORE**
([web-sample.interface-hiring.com](https://web-sample.interface-hiring.com/)), a
credit-union servicing console with no API: server-rendered tables, no test ids,
and a hidden per-transaction token on every write.

Seven capabilities are committed under `capabilities/meridian/`, each in two
versions — one a model recorded, one written by hand — and all of them approved.
**No key is needed to run any of this.**

```bash
export PYTHONPATH=.
alias agent-hands=".venv/bin/python -m agent_hands"

# read a balance off the live system, with no model in the loop
agent-hands replay capabilities/meridian/meridian_balance_recorded.json \
  --param operator=teller1 --param password=password --param branch=MAIN-001 \
  --param member_id=103001 --unattended

# a teller attempting a supervisor-only action: an answer, not a crash (exit 3)
agent-hands replay capabilities/meridian/meridian_place_hold.json \
  --param operator=teller1 --param password=password --param branch=MAIN-001 \
  --param member_id=103001 --param share_id=103001-MMKT-3 \
  --param reason=LEGAL --param notes=demo --unattended
```

### The console

One page: ask for a job in plain words, watch it drive the real application, and
read back what it did step by step. It also puts a real recorded session beside
real replays of the file that session produced, which is the argument for the
whole design in two columns.

```bash
cd agent-hands
export PYTHONPATH=.

# only needed for the "Run with the AI" button; everything else works without it
set -a; . ./.env; set +a

.venv/bin/python -m agent_hands.api --port 8080 --headed \
  --confirmable meridian_signon_recorded,meridian_inquiry_recorded,\
meridian_balance_recorded,meridian_transfer_recorded,\
meridian_open_share_recorded,meridian_update_recorded,meridian_place_hold_recorded
```

Then open <http://127.0.0.1:8080/>. **Jobs** → pick one → **Overview** → two
buttons: *Run with the AI* records it live (20–45s, a window opens and you watch
the model click), *Run the recording* replays it (about a second).

`--headed` is what makes the browser visible; without it the work happens off
screen. `--confirmable` names the capabilities whose irreversible steps a caller
may authorize — four of the seven write, and leaving one off that list makes it
stop and ask for a person instead. The confirmation carries a digest of the exact
arguments, so changing the amount invalidates it.

### The API underneath it

```bash
curl -s localhost:8080/capabilities | jq '.capabilities[].id'

curl -s -X POST localhost:8080/capabilities/meridian_member_balance/invocations \
  -H 'Content-Type: application/json' -d '{"args":{
    "operator":"teller1","password":"password","branch":"MAIN-001",
    "member_id":"103001","share_id":"103001-MMKT-3"}}' | jq '.result.outputs'
```

All three outcomes are HTTP 200 and carry `outcome` in the body. A caller that
reads "no such member" as an HTTP error has thrown away the distinction the
engine exists to make. Only the caller's own mistakes are 4xx.

### Recording one yourself

Needs a key. `--allow-writes` is a person at the keyboard saying yes for one
session; without it a flow that writes cannot be recorded at all, because the
gate replay uses asks for an approved artifact and there is not one yet.

```bash
set -a; . ./.env; set +a
agent-hands record \
  --goal "Sign on as operator teller1 with password password at branch MAIN-001, \
then look up member 103001 and read the savings balance" \
  --url https://web-sample.interface-hiring.com/signon \
  --app-id meridian-core --name my_capability --max-turns 20 \
  --out /tmp/my_capability.json

agent-hands show    /tmp/my_capability.json     # what it recorded, and what it rejected
agent-hands approve /tmp/my_capability.json     # nothing replays until this
```

`tools/build_meridian.py` regenerates the hand-authored seven, deriving every
target from the live screens with the same code the recorder uses.

## Against the local fixture

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
| a step that failed, on an artifact with no earlier check | refused: *no confirmed checkpoint* |
| a step waiting on approval | verified against the screen it asked about |

The third row is a real limitation, not a quirk. Recording attaches a check to
the **final** step only, so when an earlier step fails there is nothing
confirmed to compare against. `REPORT.md` covers it under Cuts.

The fourth is the exception, and it is not a loosening. An approval handoff has
not lost its place: nothing failed, the page has not moved, and the step needing
a yes has not run. So it anchors on the URL it asked about, and a person who
navigates away during the handoff is refused exactly as before. All three rows are asserted in
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
| `agent_hands/api.py` | the capability catalog over HTTP, and the run registry |
| `agent_hands/dashboard.html` | the console: ask for a job, watch it, read what it did |
| `tools/build_meridian.py` | authors the hand-written seven from the live screens |
| `capabilities/meridian/` | seven functions, each recorded by a model and by hand |
| `fixture/app.py` | a local stand-in, with switchable failure modes |

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

161 in total, all against the real fixture with a real browser. No mocked pages:
every bug worth finding here was a disagreement between what the code assumed a
page would do and what it did.

```bash
# 120 unit tests: perception (starts its own fixture), policy, escalation, recorder
PYTHONPATH=. .venv/bin/python -m unittest tests.test_perception tests.test_policy_escalation

# 10 more, covering the CLI
PYTHONPATH=. .venv/bin/python -m unittest tests.test_cli

# 31 end-to-end behaviors; starts its own fixture if one is not running
PYTHONPATH=. .venv/bin/python tests/test_replay.py
```

`tests/test_replay.py` collects nothing under pytest. Those 31 cases run only
when the file is called directly, as above. `pytest tests -q` reports 130,
which is the same 120 and 10 counted together.

| suite | covers |
|---|---|
| `test_perception.py` | frame crossing, the unnamed field, caption-not-id targeting, ambiguity demotion, `POINT` never offered, the second tenant |
| `test_policy_escalation.py` | surface/action/risk gates, redaction, ownership transitions, resume re-verification |
| `test_replay.py` | the three outcomes, recovery, degradation vs staleness, policy refusals |

The one to read is `test_replay.py`. It asserts *which* of the three outcomes
came back, not just that the run completed, and every real bug in this project
was a classification bug that only that catches.

See `REPORT.md` for the design argument and what was deliberately left out.
