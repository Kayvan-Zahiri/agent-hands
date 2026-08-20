# Commands

Everything runs from `~/Desktop/agent-hands`. Every command here was run and
checked on 2026-08-19. The venv and Chromium are already installed; nothing
needs downloading.

```bash
cd ~/Desktop/agent-hands
export PYTHONPATH=.
```

---

## 0. Start of day, in order

```bash
cd ~/Desktop/agent-hands
export PYTHONPATH=.

# ALWAYS kill first. A fixture left over from earlier holds the port, and the
# new one dies with "OSError: [Errno 48] Address already in use".
pkill -f "fixture.app"; sleep 1

# both banks, same terminal. westfield MUST be on 8900 or its artifact fails for
# the wrong reason. Logs redirected so their output does not tangle with yours.
.venv/bin/python -m fixture.app --port 8899 > /tmp/fx1.log 2>&1 &
.venv/bin/python -m fixture.app --port 8900 --variant westfield > /tmp/fx2.log 2>&1 &

# prove it is alive
curl -s -o /dev/null -w "8899 %{http_code}\n" http://127.0.0.1:8899/
curl -s -o /dev/null -w "8900 %{http_code}\n" http://127.0.0.1:8900/

# then the tests, before you touch anything
.venv/bin/python -m pytest tests -q          # expect 80 passed
.venv/bin/python tests/test_replay.py        # expect 16/16 passed
```

If someone asks for the model key:

```bash
export ANTHROPIC_API_KEY=$(grep '^ANTHROPIC_API_KEY=' ~/Desktop/jobs/.env | cut -d= -f2-)
```

---

## 1. The 30-second demo

```bash
# no key needed for any of this
unset ANTHROPIC_API_KEY

.venv/bin/python -m agent_hands replay capabilities/member_lookup.json --param member_id=12345
echo "exit=$?"     # 0
```

Then the same recipe on a member the model never saw:

```bash
.venv/bin/python -m agent_hands replay capabilities/member_lookup.json --param member_id=30021
```

---

## 2. The three outcomes

```bash
for m in 12345 99999 abcde; do
  .venv/bin/python -m agent_hands replay capabilities/member_lookup.json \
      --param member_id=$m --unattended > /dev/null 2>&1
  echo "member_id=$m exit=$?"
done
```

Expected, and verified:

```
member_id=12345 exit=0     success
member_id=99999 exit=3     business_outcome  member_not_found
member_id=abcde exit=3     business_outcome  invalid_identifier
```

Do NOT pipe to `tail` when reading exit codes. `$?` reports `tail`.

---

## 3. Reading an artifact

```bash
# the signer's view: what it does, what it can undo
.venv/bin/python -m agent_hands show --brief capabilities/member_lookup.json

# the debugger's view: the full target ranking, and the rejected strategies
.venv/bin/python -m agent_hands show capabilities/member_lookup.json

# the raw file
cat capabilities/member_lookup.json
```

---

## 4. Recording live (needs the key)

```bash
export ANTHROPIC_API_KEY=$(grep '^ANTHROPIC_API_KEY=' ~/Desktop/jobs/.env | cut -d= -f2-)

.venv/bin/python -m agent_hands record \
  --goal "Look up member 12345 and open their detail screen" \
  --url http://127.0.0.1:8899/ \
  --name demo --out /tmp/demo.json

.venv/bin/python -m agent_hands show /tmp/demo.json        # approved: false
.venv/bin/python -m agent_hands approve /tmp/demo.json
.venv/bin/python -m agent_hands replay /tmp/demo.json --param member_id=30021
```

Takes about 10 seconds and 3 turns. Watch the browser with `--headed`.

**Note:** a fresh recording has no business rules, so `member_id=99999` comes back
as `failure`, not `member_not_found`. That is the discovery-explores-one-path gap,
and it is worth showing on purpose.

---

## 5. Failure, on demand

```bash
# a recording that no longer matches the app: every strategy misses
.venv/bin/python -m agent_hands replay capabilities/member_lookup_stale.json \
    --param member_id=12345 --unattended
echo "exit=$?"    # 1

# the evidence it leaves
ls -la $(ls -dt evidence/*stale* | head -1)
```

Eight files: `run.jsonl`, `result.json`, `step-02-failure.png`,
`step-02-failure-a11y.json`, and the intervention packet with its
before/after images and takeover record.

```bash
# the second bank: succeeds, but degraded
.venv/bin/python -m agent_hands replay capabilities/member_lookup_westfield.json \
    --param member_id=12345 --unattended

# only the log shows it limped
grep degraded_to_rank $(ls -dt evidence/*/ | head -1)/run.jsonl
```

---

## 6. The fixture's failure modes

Switchable by query parameter, in a browser or with curl:

```
http://127.0.0.1:8899/members/search?member_id=12345&fail=notfound
                                                     &fail=denied
                                                     &fail=timeout
                                                     &fail=dialog
                                                     &fail=dialog_lossy
```

`dialog` and `dialog_lossy` look identical and need opposite handling. The lossy
one throws your typed input away, so continuing submits an empty form.

---

## 7. The invariant

```bash
grep -rn "anthropic\|ANTHROPIC" agent_hands/*.py | grep -v discover.py
```

Must print nothing. `discover.py` is the only module that can reach a model. If
that ever prints something, replay can reach a model and the design claim is gone.

Show it with the key unset, then replay, to prove it:

```bash
unset ANTHROPIC_API_KEY
.venv/bin/python -m agent_hands replay capabilities/member_lookup.json --param member_id=12345
```

---

## 8. What the model actually sees

```bash
.venv/bin/python -c "
from playwright.sync_api import sync_playwright
from agent_hands.perception import observe
from agent_hands.discover import _render
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page()
    pg.goto('http://127.0.0.1:8899/', wait_until='load'); pg.wait_for_timeout(400)
    print(_render(observe(pg)))
    b.close()
"
```

Five numbered controls, no HTML. That is the whole message.

The raw accessibility tree underneath it:

```bash
.venv/bin/python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page()
    pg.goto('http://127.0.0.1:8899/', wait_until='load'); pg.wait_for_timeout(400)
    print(pg.locator(':root').aria_snapshot(mode='ai'))
    b.close()
"
```

And the HTML it never sees:

```bash
curl -s http://127.0.0.1:8899/members/search | head -25
```

`id="ctl00_r3_c1"` is in there and reaches nothing.

---

## 9. Prove it survives a change

Rename the button in `fixture/app.py`:

```bash
sed -i '' 's/>Search<\/font><\/a>/>Submit Query<\/font><\/a>/' fixture/app.py
pkill -f "fixture.app --port 8899"; .venv/bin/python -m fixture.app --port 8899 &
sleep 2
.venv/bin/python -m agent_hands replay capabilities/member_lookup.json --param member_id=12345 --unattended
grep degraded_to_rank $(ls -dt evidence/*/ | head -1)/run.jsonl
```

Still succeeds, on a lower-ranked target. Put it back:

```bash
git checkout fixture/app.py
pkill -f "fixture.app --port 8899"; .venv/bin/python -m fixture.app --port 8899 &
```

---

## 10. Housekeeping

```bash
git status --short
git log --oneline -6
git clean -fdq evidence/          # drop test runs, keep the committed six
jobs                              # what is still running in this terminal
pkill -f "fixture.app"            # stop both fixtures
cat /tmp/fx1.log                  # if a fixture is misbehaving
```

---

## If something is wrong

**Everything fails at once** — a fixture is not running, or westfield is missing
from 8900. Check with the two curls in section 0.

**`ERR_CONNECTION_REFUSED`** — same thing.

**`OSError: [Errno 48] Address already in use`** — a fixture from an earlier run
is still holding the port. `pkill -f "fixture.app"; sleep 1` and start again. To
see what has it: `lsof -nP -iTCP:8899 -sTCP:LISTEN`.

**A replay is refused before starting** — the artifact is not approved, or the URL
is not on the allowlist. `FIXTURE_POLICY` in `policy.py` pins `127.0.0.1:8899`, so
any other port is refused by design.

**`exit=0` when you expected 1** — you piped to `tail` or `head`. Redirect to a
file instead.

**Escalation prompt appears and you want out** — Ctrl-C. It aborts, by design,
even mid-answer.
