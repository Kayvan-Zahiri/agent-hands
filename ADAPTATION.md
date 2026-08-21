# Adapting agent-hands to MERIDIAN CORE

**In one paragraph:** agent-hands lets an AI click through an application once
and write down what it did. After that, ordinary code follows the written-down
recipe and the AI is never used again. MERIDIAN CORE is a credit-union banking
application — a realistic stand-in built for this exercise, not a real bank —
with no API, so the only way in is the screen. This is the story of pointing one
at the other.

The test was whether that would be a configuration job or a rewrite. It was
mostly configuration: a handful of edits to shared code, two new files, and
seven recipes. What follows is each of those, and the things I got wrong.

> New here? `README.md` explains the idea and defines every term this document
> uses — capability, artifact, replay, checkpoint, escalate.

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

`timeout` was not recognized as an expired session, because the engine's
condition table was a module constant looking for "session has expired" while
MERIDIAN says "your session has timed out". Two of six exceptional states were
dead code against this target. The table is per-app configuration now --
`AppProfile`, keyed on the artifact's own `app_id`, which is what `BusinessRule`
already was and for exactly this reason. The table above is the run that found
this; a unit test covers each application recognizing its own wording and
neither recognizing the other's, but I have not re-run the six injected faults
end to end since, so treat that row as the diagnosis rather than the retest. "Supervisor override required" is
deliberately not in it: the application is answering, not faulting, so it stays
a `BusinessRule` and comes back as an outcome the caller can act on. A condition
is for a state the engine can retry, re-enter or dismiss.

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

Nothing is confirmable unless a server-side flag names it. Place Hold should be
left out of that flag for the same reason it is gated at all — a supervisor
deciding is the point of it — and the `--confirmable` list printed in the README
currently includes it, which is a contradiction between the doc and the command
and is called out in the cuts below. Place Hold is: it is the restricted function, and a supervisor deciding
is the point of it. This is a deliberate-action gate, not an authorization one —
nothing here knows who is asking. Real authorization needs identity, which this
system does not have.

Driving a system that has credentials found a leak the old fixture could not.
The invocation's parameters were redacted in the evidence, but the step record
wrote what was *typed*, so the operator's password appeared in the clear in
`run.jsonl` — all of the cost of redaction and none of the benefit. Steps now
record the template (`{password}`) rather than the value.

Escalation now runs through the API. `OperatorConsole` is two methods precisely
so the terminal version and a review queue can stand in for each other, and
`QueueConsole` is the second one: the run parks, the browser stays open on the
screen it stopped at, and a person answers over HTTP. Everything above it was
already real -- the ownership state machine, the intervention packet, the
re-check before control comes back. Who answers is decided per run, not per
process: an invocation says whether anybody is waiting on it, so one server
serves both and there is no flag to forget.
`--operator` still exists and still means every run on that server may park,
including callers that are not the console. It only asks to be asked -- it
cannot approve anything or widen what a run may do, and what it displaces is
giving up. The default stays the unattended answer, and an unanswered handoff
aborts after three minutes, which is that same ending by a slower route. So
does closing the browser window, which the run notices in about a second --
somebody who closes it has stopped watching, and often did the step by hand
first, so there is no session left to hand back either way.

Building it found the escalation path's own dead spot. `verify_resume` refuses
without a confirmed checkpoint, which is right when a run broke before
confirming anything -- but an approval is not that case. Nothing failed, the
page has not moved, and the step needing a yes has not run. Since every artifact
the recorder produces carries a check only on its last step, that refusal fired
on every approval resume: a path demonstrable by hand and dead against real
artifacts. An approval handoff now anchors on the URL it asked about, so a
person who navigates away is still refused. Which question gets asked changed,
not whether one is.

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
routes, and the second is the one that trips you up: name the password in the
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

## The second failure worth reading

Signing on with the wrong password reported **success**, and exited 0.

A step finishes when its checkpoint holds — "the screen should now say X".
Step 4 of every recipe here waits for `MAIN MENU`. MERIDIAN prints a
function-key bar along the bottom of every screen it renders:

```
F3=Sign Off   F5=Main Menu   F7=Member Inquiry   F12=Cancel
```

Including the screen it returns when it refuses a sign-on. The engine asked
Playwright's `get_by_text`, which matches without regard to case, so
`F5=Main Menu` satisfied a checkpoint on `MAIN MENU`. The run never reached the
main menu, and said it had.

The deeper fault is that the two halves of the same checkpoint disagreed. The
engine's wait was case-insensitive; `default_verify`, which re-checks the screen
before an operator hands control back, has always compared with a plain `in` —
case-sensitive. One checkpoint, two meanings. The fix makes the wait
case-sensitive too, which is a one-line change and lines the halves up.

It is the same shape as the balance above: not a crash, an answer that is wrong
and that nothing in the run contradicts. And it was invisible at home for the
same reason the caption bug was — the local practice app had no function-key
bar, so no screen there ever carried a phrase that differed from a checkpoint
only by case. That fixture now prints one, and three cases in the behavior suite
hold it: the phrase the screen really shows passes, the same phrase in the wrong
case fails, and a phrase containing a `.` still matches literally.

## The front door

`agent_hands/chat.py`. A sentence resolves to one job and the values it can find;
`POST /chat` returns that plan and runs nothing, and the page takes it to the
same invocation route every other caller uses. Keeping those two apart is the
whole point — a chat box that could reach the engine directly would be a way
around the gates rather than a way in.

No model runs in it. The claim this project rests on is that the production path
has no model in the decision loop, and a chat box quietly calling one would blur
exactly that line. `read_intent` is the seam: a model returning the same
(capability, slots) pair drops in without anything downstream noticing. What it
costs is real and worth saying — it understands the phrasings written down in
that file and nothing else.

It is deterministic, so it is testable, and writing the tests immediately found
two things reading it had not. "hold on, check the balance" resolved to *freeze
this account* — a filler word selecting a write, which is the worst mistake the
module could make. The obvious fix then swallowed "put a hold on member 102777",
which is a real instruction, so the explicit phrasings are scored before the
veto. And "for Member 103001" invented the surname *Member*, turning a lookup by
number into a name search that finds nobody. Both are pinned.

One rule it does not share with the Service Desk: it never fills a business
value from the recording. Asked for a balance without an account named, it asks.
The desk seeds its form from examples because the form is on screen to be read
before anybody clicks; in a sentence there is nothing to read, so choosing which
of somebody's accounts to look at would be a guess with money attached.

## What I left out, and what I would build next

- **Reads have no label-relative target in the engine.** The failure above is
  this, and it is the first thing I would build. The idea already exists for form
  fields as `LABELED_FIELD`; a value cell needs the same thing anchored to the
  row it is in. The artifacts carry an xpath as a workaround, which works and
  does not generalize.
- **The confirmation gate is a deliberate-action gate, not an authorization
  one.** Nothing knows who is asking. Real authorization needs identity, which
  this system does not have. The operator handoff has the same hole from the
  other side: whoever has the console answers, and the run records that somebody
  did, not who.
- **A parked run holds a browser.** `QueueConsole` blocks a worker thread with a
  live page on it, which is the right shape for one person watching and the
  wrong one for a queue with depth. Parking properly means letting the browser
  go and re-establishing it on resume, which the artifact makes possible and
  nothing here does.
- **The shipped start command contradicts the paragraph above.** README's
  `--confirmable` list names `meridian_place_hold_recorded`, which lets a caller
  authorize the one action this write-up says only a supervisor should. Drop it
  from the command; the claim is the better position.
- **Recorded artifacts still checkpoint only their last step.** The approval
  handoff no longer needs one, but a resume after a failed checkpoint or an
  unfound control does, and there it still refuses. The fix belongs in the
  recorder: confirm each screen as it is reached, not the destination only.

## Running it

```bash
# no credentials needed for any of this
PYTHONPATH=. .venv/bin/python -m agent_hands.api --port 8080 --headed \
    --confirmable meridian_transfer_recorded   # then open localhost:8080

PYTHONPATH=. .venv/bin/python tools/build_meridian.py   # re-author the seven
```

Suites: 168 unit, 34 end-to-end against a real browser, nothing mocked. The rule
the design rests on still holds — replay cannot reach a model:

```bash
grep -rn "anthropic\|ANTHROPIC" agent_hands/*.py | grep -v discover.py   # empty
```
