# Run it

**What you are about to start:** a web page on your own machine that drives a
real banking application by clicking through its screens, the way a person
would. Pick a job, fill in what it needs, watch it work, read the answer back.

The clicking is done by ordinary code following a recipe that an AI wrote down
once, earlier. Running it needs no AI and no API key. Only §5, where you record
a *new* recipe, does.

Every command below is in order. Python 3.10 or newer.

## 1. Set up

```bash
git clone https://github.com/Kayvan-Zahiri/agent-hands.git
cd agent-hands

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## 2. Start the console

You are already in the repo from §1 — stay there.

```bash
export PYTHONPATH=.

.venv/bin/python -m agent_hands.api --port 8080 --headed \
  --confirmable meridian_signon_recorded,meridian_inquiry_recorded,\
meridian_balance_recorded,meridian_transfer_recorded,\
meridian_open_share_recorded,meridian_update_recorded,meridian_place_hold_recorded
```

Open <http://127.0.0.1:8080/>.

Stop it with `Ctrl-C`, or from another terminal:

```bash
pkill -f "agent_hands.api"
```

## 3. Drive it

| in the console | what happens |
|---|---|
| **Jobs** → click one | loads it into **Overview** |
| **Run the recording** | replays it against the live system, about a second, no model |
| **Run with the AI** | a model works the job out from scratch, 20–45s (needs a key, see §5) |
| **Artifact** | the file a recording produced, including the raw JSON |
| **Operator** | the same job with nobody authorizing the write: it stops and asks |
| **Service Desk** | the end-user surface: pick a task, fill in a form, read the answer |
| **Runs** | every run, with its steps and what it read back |

`--headed` opens a browser window you can watch. Share your whole desktop, not a
single window, or your audience will not see it.

**Operator** is the handoff. The run drives to the step that moves money and
stops, because nothing authorized the write. The browser stays on the
confirmation screen, and **Resume** posts it or **Abort** walks away with
nothing written.

Answer it in the console. Clicking in the browser window does not reach the
run, and doing the step by hand means resume is refused -- otherwise the engine
would do it a second time. Three ways it ends without you: closing the browser
window ends it in about a second, nobody answering ends it after three minutes,
and either way nothing is written. The run says which one it was.

`--operator` on the server does the same for *every* run, including callers
that are not the console. The tab does not need it.

**Service Desk** is the same engine with the machinery taken off. It builds a
form from the capability's declared parameters, reads the answer back in plain
words, and keeps three endings apart: MERIDIAN accepted it, MERIDIAN answered
no, or it could not finish. Anything that changes a record is read back for
confirmation first. Where two artifacts do the same job it takes the one with
business rules, because an artifact without them turns "you are not authorized"
into a broken automation.

Four of the seven jobs write to the target: **Move money**, **Open an account**,
**Freeze an account**, **Update contact details**. Each click posts a real
transaction against the demo host.

## 4. Run one from the command line

```bash
export PYTHONPATH=.

# read a balance
.venv/bin/python -m agent_hands replay capabilities/meridian/meridian_balance_recorded.json \
  --param operator=teller1 --param password=password --param branch=MAIN-001 \
  --param member_id=103001 --unattended

# a teller attempting a supervisor-only action: an answer, not a crash. Exit 3.
.venv/bin/python -m agent_hands replay capabilities/meridian/meridian_place_hold.json \
  --param operator=teller1 --param password=password --param branch=MAIN-001 \
  --param member_id=103001 --param share_id=103001-MMKT-3 \
  --param reason=LEGAL --param notes=demo --unattended
```

Exit codes: `0` it worked, `3` the application answered no, `1` it broke.

## 5. Only if you want to record (needs a key)

```bash
cp .env.example .env      # then put your Anthropic key in it
set -a; . ./.env; set +a  # then start the console as in §2
```

Without this, **Run with the AI** returns `no Anthropic credentials found` and
everything else still works.

From the command line instead of the console:

```bash
set -a; . ./.env; set +a
export PYTHONPATH=.

.venv/bin/python -m agent_hands record \
  --goal "Sign on as operator teller1 with password password at branch MAIN-001, \
then look up member 103001 and read the savings balance" \
  --url https://web-sample.interface-hiring.com/signon \
  --app-id meridian-core --name my_capability --max-turns 20 \
  --out /tmp/my_capability.json

.venv/bin/python -m agent_hands show    /tmp/my_capability.json
.venv/bin/python -m agent_hands approve /tmp/my_capability.json
```

`--allow-writes` lets a recording perform irreversible actions. Without it a
flow that writes cannot be recorded, because the gate replay uses asks for an
approved artifact and there is not one yet.

## 6. Tests

```bash
export PYTHONPATH=.
.venv/bin/python -m pytest tests -q        # 134
.venv/bin/python tests/test_replay.py      # 34, real browser
```

Both start their own local fixture. Neither needs a key.

`tests/test_replay.py` collects nothing under pytest — it is a script with its
own runner, so it always gets its own line.

## 7. The API on its own

```bash
curl -s localhost:8080/capabilities | jq '.capabilities[].id'
curl -s localhost:8080/invocations  | jq '.invocations[0]'

curl -s -X POST localhost:8080/capabilities/meridian_member_balance/invocations \
  -H 'Content-Type: application/json' -d '{"args":{
    "operator":"teller1","password":"password","branch":"MAIN-001",
    "member_id":"103001","share_id":"103001-MMKT-3"}}' | jq '.result.outputs'
```

All three outcomes are HTTP 200 and carry `outcome` in the body. Only the
caller's own mistakes are 4xx.

## When something looks wrong

**The console loads but every panel is empty.** The server is not running. Check
with `curl -s -o /dev/null -w "%{http_code}" localhost:8080/`.

**`Address already in use`.** An older server still holds the port:
`pkill -f "agent_hands.api"`.

**A job comes back `declined`.** It writes, and its name is not in
`--confirmable`. See §2.

**Everything against the target fails.** Check it is up:
`curl -s -o /dev/null -w "%{http_code}" https://web-sample.interface-hiring.com/signon`

**`no tests ran`, or a count below 134 / 34.** You are not in the repo root.
pytest exits `4` and looks almost green.
