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
every test passed.

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

## What recording against it actually found

Every function in §2.1 now has a model-produced artifact as well as a
hand-authored one. Getting there needed two changes to the recorder, and turned
up three faults that only appear when a model drives a system with money in it.

**The discovery loop could not use a dropdown.** Four of the seven flows need
one, so the model could never have completed them however well it reasoned.
`select_option` takes the dropdown's number and the option as it reads on screen,
and the value that reaches the artifact is looked up from the DOM rather than
taken from the model — because an option's label here carries live data,
`"MMKT-3 - Money Market ($7.97)"`, and recording the label would bake one
member's balance into a capability meant to run for everybody.

**A flow that writes could not be recorded at all.** An irreversible step is
gated on the artifact having been approved, and during a recording the artifact
is what the session is trying to produce. The two moments are asking different
questions: replay asks whether a reviewer signed the file, recording asks whether
a person is watching. `--allow-writes` is that person saying yes for one session.

**The recorder wrote the password into the artifact.** Signing on types a
credential, and the recorder's job is to write down what happened. An artifact is
read in a diff and committed, so that is a credential in version control. Two
routes, and the second is the one that catches you out: name the password in the
goal and it becomes a parameter on its own, carrying the literal forward as a
helpful `example`. Both are closed and the description is scrubbed too.

**Freezing an account did not count as a write.** `classify_risk` knew *submit,
save, delete, post, transfer*. This target's most restricted action — the one the
brief singles out as needing a supervisor — is a button reading **"Apply Hold"**,
and opening an account is **"Open Share"**. Neither matched, so both classified
as reversible and the gate that exists for exactly these two never ran. Fixed
with phrases rather than bare words, because a menu link reading "Open New Share"
navigates and writes nothing while the button on the confirmation screen opens an
account.

Recording also corrected three things I had guessed wrong when authoring by hand:
Update Member has no review step at all, the hold's post button reads "Apply
Hold" and not "Place Hold", and open-share refuses a deposit under five dollars,
which is a business answer rather than a fault.

## The failure worth reading

`evidence/...211103Z_meridian-balance-recorded_2955f5` returns `success` and a
plausible number, and it is wrong.

The capability is named `savings_balance`. It was recorded against member
103001's **Money Market** account and replayed for member 100234, where it
returned `$198.04` — that member's **Share Draft (Checking)** balance.

| | recorded member | a different member |
|---|---|---|
| returned | `$22.37` | `$198.04` |
| which account | Money Market | **Share Draft (Checking)** |
| outcome | `success` | `success` |
| declared type holds | yes | yes |
| `degraded` | `[]` | `role_name` → `nth_of_role` |

Nothing in the run contradicts it. The checkpoint held. The declared type is
`number` and `$198.04` is a number. The recorded target for that read was
`cell "$22.37"` — the answer from the recording — so it can only ever match the
member it was recorded against, and every other member falls through to counting
cells in a table whose length varies per member.

One field separates the two runs. That is the argument for having added it, and
it is also the argument for label-relative reads being the next thing to build:
detection is not the same as prevention.

The transfer has the same shape structurally. Its confirmation read recorded
`cell "CN480134"` as the primary target, and every run issues a new number, so
that read degrades on every replay by construction.

## What I left out, and what I would build next

- **Reads have no label-relative target in the engine.** The failure above is
  this, and it is the first thing I would build. The idea already exists for form
  fields as `LABELLED_FIELD`; a value cell needs the same thing anchored to the
  row it is in. The artifacts carry an xpath as a workaround, which works and
  does not generalize.
- **No regression test at home for the caption fix or the dropdown tool**,
  because the local fixture has no stacked-caption form and no `<select>` at all.
  This is the cut I like least: both bugs were hidden by the fixture's shape, and
  I have not changed that shape.
- **The condition table is still a module constant.** `timeout` and `permission`
  remain misclassified against this target because the engine looks for "session
  has expired" and MERIDIAN says "your session has timed out". Making that table
  per-app configuration is what turns "we edited the engine for the second
  target" into "we adapted by configuration", and it is the largest remaining
  piece of that argument.
- **`classify_risk` over-classifies on the transfer.** Every click on a page
  whose URL contains "transfer" is irreversible, which is fail-safe and noisy.
- **The confirmation gate is a deliberate-action gate, not an authorization
  one.** Nothing knows who is asking. Real authorization needs identity, which
  this system does not have.
- **The operator console is still stubbed.** The escalation path is real and
  demonstrated — a transfer with nobody authorized to approve it stops at the
  post step and leaves an intervention packet with screenshots — but a person
  answers it at a terminal, not in the console.

## Running it

```bash
# no credentials needed for any of this
PYTHONPATH=. .venv/bin/python -m agent_hands.api --port 8080 \
    --confirmable meridian_funds_transfer      # then open localhost:8080

PYTHONPATH=. .venv/bin/python tools/build_meridian.py   # re-author the seven
```

Suites: 97 unit, 29 end-to-end against a real browser, nothing mocked. The rule
the design rests on still holds — replay cannot reach a model:

```bash
grep -rn "anthropic\|ANTHROPIC" agent_hands/*.py | grep -v discover.py   # empty
```
