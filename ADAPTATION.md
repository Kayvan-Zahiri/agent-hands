# Adapting agent-hands to MERIDIAN CORE

The core is unchanged in shape: a model drives an application once and writes a
reviewable artifact; a deterministic engine replays it with no model in the loop.
Pointing it at MERIDIAN CORE took two edits to shared code, one new module, and
seven artifacts. This is what each of those was, and what I got wrong.

## What the adaptation actually took

**Nothing about hosts, ports or URLs.** The engine never knew them. The allowlist
is derived from the artifact's own entry URL, and business rules already lived in
the artifact rather than the engine, on the argument that a shared engine
hard-coding one tenant's error strings is wrong for the next tenant. That
argument paid off.

**The per-transaction token was a non-event.** MERIDIAN puts a hidden `_token` in
every POST form, and the brief calls reading it the headline challenge. Clicking
the real submit control sends it, so no step reads it and it never becomes a
parameter. It is per-session rather than per-transaction, and tampering with it
is accepted at `/review` and refused at `/post`. This only stays free if you
never navigate straight to a `/review` or `/post` URL, so the artifacts enter
write flows at the form and click through. That is an argument for driving a real
browser rather than an HTTP client, and it is the clearest one this project has.

**Two core changes, both because the core was under-tested rather than wrong.**

The first is the one that mattered. `_caption_xpath` addressed a form field by
the caption in the cell beside it, and unioned two reading directions — the cell
to the left and the row below — treating two matches as a layout too ambiguous to
guess at. On a form that stacks caption/field rows, the second branch does not
find the same field from another direction. It finds the *next row's* field. So
every field on every MERIDIAN screen matched twice, was rejected, and fell
through to counting controls by position at durability 0.4 — which is exactly the
brittleness the design exists to avoid. Its docstring defended the behavior, and
92 tests passed.

It was invisible because of the *shape* of the old fixture, not because of
anything in the engine. That fixture's search form is a single row, and the row
after it holds an anchor rather than an input, so the second branch never matched
and the union never fired. Keeping only the left-hand cell, and excluding
controls nobody would call a field, takes all 17 fields across six MERIDIAN
screens from positional counting to caption addressing. Every MERIDIAN run since
reports `degraded: []`.

The second: parameter substitution reached a step's text and URL but never its
target, so a step could say what to type but not *which control*. A target
meaning "the row whose first cell is `{share_id}`" went to the page with the
braces intact. Any flow that picks one row out of a table needs this, which is
most of what a servicing console does.

## The capability API

`agent_hands/api.py`. Five routes on a threaded stdlib server — threads because
the engine drives Playwright's synchronous API, which raises inside an event
loop, and stdlib because that adds no dependency.

Most of the contract already existed. `ReplayResult.to_json` is the response
body; `bind_params` is already a real typed validator. The API adds a catalog
projection, a run registry, and `typed_outputs` — the outputs again as their
declared types, served alongside the raw strings so the response and the
`result.json` on disk agree about what was read.

Two decisions worth defending:

**The catalog drops `steps`, `entry_url` and every target.** A caller that knows
the flow starts depending on it, and the point is that the flow can be re-recorded
without the caller changing.

**Status codes describe the invocation, never the business answer.** All three
outcomes are 200 and carry `outcome` in the body. A caller that reads "no such
member" as an HTTP error has thrown away the distinction the engine exists to
make. Only the caller's own mistakes are 4xx.

Building the catalog surfaced a real bug: two artifacts both declared
`name: "member_lookup"` and differed only by `app_variant`, so a catalog keyed on
name served whichever loaded last and silently dropped the other. Capabilities
are addressed `name@variant` now, and a genuine duplicate is refused rather than
overwritten.

## Driving the UI, and its exceptional states

Targets are a ranked fallback list and replay records which rank won. After the
caption fix every MERIDIAN target resolves on its primary, so `degraded` is empty
on a healthy run and any entry in it is a real signal rather than noise.

Feeding all six injected faults through the engine, against the real host:

| `?inject=` | outcome | code |
|---|---|---|
| `validation` | business_outcome | `transaction_rejected` |
| `notfound` | business_outcome | `member_not_found` |
| `permission` | business_outcome | `supervisor_required` |
| `timeout` | **failure** | — `checkpoint failed` |
| `maintenance` | **failure** | — `interstitial kept reappearing` |
| `server` | failure | `app_error` |

Four are right. Two are not, and both are worth being precise about.

`timeout` should be recognized as an expired session and recovered or escalated.
It is not, because the engine's condition table is a module constant that looks
for "session has expired" while MERIDIAN says "your session has timed out". The
same is true of "you do not have permission" against "supervisor override
required". Two of six exceptional states are dead code against this target, and
the fix is to make that table per-app configuration — which is what
`BusinessRule` already is, and for exactly this reason.

`maintenance` is detected. The engine recognizes the interstitial and clicks its
Continue link, but the injected fault fires on every request, so it reappears and
the run gives up after the dismissal budget. Against a one-shot interstitial it
recovers; against a permanent one, giving up and saying so is the right answer.

A teller attempting Place Hold is refused at the review step, before anything is
written, and comes back `business_outcome / supervisor_required`. I classified
that as a business outcome rather than a failure because the application is
answering "not you" rather than faulting. It is arguable, and the stronger
version routes it to a supervisor instead.

## Safety, evidence and escalation through the new surface

The wrapper does not become a way around the gates. The request carries arguments
and a deadline and nothing else; the allowlist is derived server-side from the
artifact; an artifact whose entry URL is templated is refused at load, because
substitution would let an invocation move the target host and the allowlist that
guards it. `Policy`, `Evidence` and the browser are built per request, because
`Policy.approver` is a mutable slot and sharing it would leak one caller's
approval into another caller's irreversible step.

Irreversible steps take a confirmation bound to the capability *and* to a digest
of the arguments the server computed itself. It is not a boolean, so it cannot be
a default, and it cannot be replayed against a different amount:

| request | result |
|---|---|
| no confirmation | `failure` at the post step — declined |
| confirmation for a different amount | `failure` at the post step — declined |
| confirmation bound to these arguments | `success`, confirmation `CN480069` |

Nothing is confirmable unless a server-side flag names it, and Place Hold is
deliberately left out: it is the restricted function, and a supervisor deciding
is the point of it. This is a deliberate-action gate, not an authorization one —
nothing here knows who is asking. Real authorization needs identity, which this
system does not have.

Driving a system that has credentials found a leak the old fixture could not.
The invocation's parameters were redacted in the evidence, but the step record
wrote what was *typed*, so the operator's password appeared in the clear in
`run.jsonl` — all of the cost of redaction and none of the benefit. Steps now
record the template (`{password}`) rather than the value.

Escalation is stubbed at the seam it was designed for. `OperatorConsole` is a
two-method protocol and everything above it is real — the ownership state
machine, the intervention packet, the re-check on resume. The API uses the
unattended console: stop, and say a person was needed. Approving automatically
would wave through the steps the gate exists for.

## What I left out, and what I would build next

- **No recording against MERIDIAN.** No API credential was available, and
  `discover.py` has no `select` tool at all, which four of the seven flows need.
  The artifacts are hand-authored trajectories whose *targets* are still derived
  from the live screens by the same code the recorder uses, and `recorded_by`
  says so on every one. The missing tool is three lines; the reason it is a cut
  rather than an oversight is that the parameter must be the option value, not
  the visible label, because the labels embed live balances.
- **Reads still have no label-relative target in the engine.** A value cell's
  derived primary target is the value itself, so it only ever matches the member
  it was recorded against. The artifacts carry an xpath as a workaround. This is
  the first thing I would build, and it is the same idea `LABELLED_FIELD` already
  implements for form fields.
- **No regression test at home for the caption fix**, because the fixture has no
  stacked-caption form. This is the cut I like least: the bug was hidden by the
  fixture's shape, and I have not changed that shape.
- **The condition table is still a module constant.** Making it per-app
  configuration is what turns "we edited the engine for the second target" into
  "we adapted by configuration".
- **Open Share, Update Member and Place Hold are authored but only lightly run.**
  Sign-on, inquiry, balance and transfer are exercised end to end.
- **No chatbot and no dashboard.** The dashboard is mostly a view over the
  evidence directory, and the join key already exists: a run id *is* the evidence
  directory name.

## Running it

```bash
PYTHONPATH=. .venv/bin/python tools/build_meridian.py        # author the seven
PYTHONPATH=. .venv/bin/python -m agent_hands.api --port 8080 \
    --confirmable meridian_funds_transfer
```

Suites: 92 unit, 28 end-to-end against a real browser, nothing mocked. The rule
the design rests on still holds — replay cannot reach a model:

```bash
grep -rn "anthropic\|ANTHROPIC" agent_hands/*.py | grep -v discover.py   # empty
```
