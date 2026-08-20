# SUMMARY

Orientation for anyone (or any agent) picking this repo up cold. `README.md` is
how to run it, `REPORT.md` is why it is shaped this way. This file is the map.

## The one idea

Exploration and execution are different problems, so they get different machinery.

A model drives the application **once**, at record time, and writes a reviewable
artifact. A deterministic engine then replays that artifact many times **with no
model in the loop**. Recording needs `ANTHROPIC_API_KEY`; replay does not, and
that asymmetry is the design, not an optimisation.

## The invariant

`replay.py` must never be able to reach a model. Checked by:

    grep -rn "anthropic\|ANTHROPIC" agent_hands/*.py | grep -v discover.py   # must be empty

`discover.py` is the only module that imports `anthropic`. If a change makes
replay depend on it, the central claim is gone. Everything else is negotiable.

Two more that are load-bearing:

- **The model picks a control *number*, never a selector.** It sees an
  accessibility view (`_render` in `discover.py`), chooses an integer, and
  `_node_at` resolves it against the same list. It cannot express a selector, so
  it cannot express a bad one. This is also the injection boundary: page text
  arrives as the content of a numbered row, not as instructions.
- **`POINT` is recorded and never resolved.** Screenshot coordinates go into the
  artifact for a human reviewer and are structurally unresolvable at replay.
  `test_perception.py` asserts they are never offered as a live strategy.

## Modules

| file | lines | what it owns |
|---|---|---|
| `replay.py` | 959 | the deterministic engine: resolve targets, act, checkpoint, recover, classify the outcome |
| `perception.py` | 813 | the accessibility view, and deriving ranked targets from a live element |
| `escalation.py` | 775 | handing a live session to a person and taking it back; ownership state machine |
| `policy.py` | 534 | allowlists, risk classification, approval gating, redaction |
| `discover.py` | 498 | the record-time agent loop. **The only file that touches a model** |
| `recorder.py` | 475 | turning a live action into a `Step` with a ranked `TargetSet` |
| `evidence.py` | 375 | `run.jsonl`, `result.json`, screenshots, a11y snapshots, intervention packets |
| `schema.py` | 325 | the artifact types. Read this first |
| `cli.py` | 245 | `record` / `show` / `approve` / `replay` |
| `fixture/app.py` | 266 | the hostile legacy app, and its switchable failure modes |

## Data model (`schema.py`)

- **`Strategy`** — ranked, best first:
  `ROLE_NAME` → `ROLE_NAME_IN_REGION` → `LABELLED_FIELD` → `NTH_OF_ROLE` →
  `DOM_PATH` → `POINT`. Recording captures *every* way a control could be found
  plus the ones **rejected and why**; replay walks the list and records which
  rank won.
- **`Risk`** — `SAFE` / `REVERSIBLE` / `IRREVERSIBLE`. Classified at record time.
  `IRREVERSIBLE` is default-deny and needs approval bound to the artifact version.
- **`Outcome`** — three-way, not success/exception. `SUCCESS`,
  `BUSINESS_OUTCOME` (the app answered, and the answer was "no such member"),
  `FAILURE`. Exit codes `0` / `3` / `1`. Reporting a correct negative as a crash
  is what makes automation useless for the case it exists to serve.
- **`Param`** — a literal that appeared in the goal *and* in the trajectory
  becomes a parameter. Done at record time because that is when provenance still
  exists; by replay you would be guessing which digit string is a session id.

## Running it

    PYTHONPATH=. .venv/bin/python -m pytest tests -q        # 70 unit
    PYTHONPATH=. .venv/bin/python tests/test_replay.py      # 16 e2e, real browser
    PYTHONPATH=. .venv/bin/python -m fixture.app --port 8899 &

Both suites start their own fixture. Replay needs one on **8899**, because the
artifact hardcodes that `entry_url`. Verified from a clean clone: 70 pass, 16/16
pass, and replay returns 0 / 3 / 3 with `ANTHROPIC_API_KEY` unset.

Fixture failure modes, by query param: `?fail=notfound`, `denied`, `timeout`,
`dialog`, `dialog_lossy`.

## Bugs found by running code that had never been executed

Guardrails. Do not reintroduce these.

1. **Retry after a recovery re-ran the step's action.** On an `IRREVERSIBLE`
   step that is a duplicate payment. Fixed by asking whether the recovery already
   satisfied the step's goal before acting again.
2. **The two interstitials need opposite recoveries.** `dialog` merely covers the
   screen: dismiss and continue. `dialog_lossy` *discards the request* and takes
   the typed input with it, so continuing submits an empty form and then blames
   the checkpoint. They are told apart by testing whether the checkpoint already
   holds.
3. **`approve` silently dropped business rules.** `save` serialised the whole
   capability; `load` rebuilt it field by field, so a field the loader did not
   know about vanished. Approval round-trips through the loader. Surfaced much
   later as an unrelated-looking checkpoint failure.
4. **A keyword collision in the evidence writer** that only fired on the error path.
5. **`OperatorConsole` is a `Protocol` and cannot be instantiated** (`deaf162`).
   `cli.py` did `OperatorConsole()`, so interactive replay raised `TypeError`.
   Tests missed it because they inject their own console.

The pattern: every one was invisible to reading and obvious to running.

## Where to extend

`REPORT.md` "Cuts" is the honest list, roughly in order of value:

- **Identity and audit.** The policy layer gates *what* runs, not *who* asked.
- **A real operator console.** The ownership machine, the intervention packet and
  the resume re-verification are real; the console is a terminal stub.
  `OperatorConsole` is the seam, and nothing above it changes when a queue and a
  review UI drop in.
- **Degradation aggregation.** A run that passes on a 0.4-confidence fallback is
  detected, logged and recorded in the evidence. Nothing rolls that up across
  runs to notice a capability has been limping for a week.
- **Variant target overrides.** `app_variant` is in the schema; the merge that
  would let one artifact carry per-tenant overrides is not written.
- **Discovery explores one path.** It records the successful route and stops, so
  business rules are still hand-authored. Having the agent try one bad input
  during recording is the cheapest high-value addition, and replaying the draft
  once before handing it over would be nearly free.
- **Native desktop.** The abstraction was chosen because UIA and AX expose the
  same role-and-name vocabulary. No backend exists, so that is a claim.
- **Concurrency.** One session, one run.

## Conventions

- Comments earn their line. Match the surrounding density; no explanatory
  paragraphs over self-evident code.
- Tests run against a real browser and the real fixture. Nothing mocks a page.
- Frame-scoped checkpoints, never page-wide text assertions: the nav frame
  repeats the content frame's words, so a page-wide match is a coincidence.
- Condition-based waits, never sleeps.
