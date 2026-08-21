# agent-hands: design report

**What this is about.** Some business applications have no API — the only way to
make them do anything is to click through the screen. agent-hands lets an AI do
that clicking once, writes down exactly what it did, and then repeats it with
ordinary code and no AI. This document is the reasoning behind how it is built,
and an honest list of what it does not do.

> Every term below is defined in `README.md`: capability, artifact, replay,
> checkpoint, escalate.

## Architecture

An AI drives the application once, at record time, and produces a **capability
artifact** — a file listing the steps it took and how to find each control
again. From then on, ordinary code replays that file, with no AI involved.

```
record (once, supervised, costs a model call)
    observe -> model picks a control -> policy gate -> act -> derive targets
                                                              |
                                                        draft artifact
                                                              |
                                                    human review + approval
                                                              |
replay (many, unattended, no model)  ------------------------->
    for each step: gate -> resolve -> act -> classify -> checkpoint
```

That split is the whole design. The expensive, unpredictable part happens once
and a person checks it. The part that runs a thousand times is a fixed list of
steps someone already approved.

Recorded live against the fixture with Claude Opus 5, this took 3 turns and two
actions. The artifact it produced replays on members the model never saw:

```
member_id=12345 (recorded with)  success
member_id=22887 (never seen)     success
member_id=30021 (never seen)     success
```

The model picked *control 3* from a numbered list of what was on screen. The way
that control is described in the artifact, "the field captioned Member ID", was
worked out by the recorder afterwards, not by the model. Both halves are needed.
The model knows which control it wants. The recorder knows how to find that
control again next month.

Two things follow from this, and they are what make it work:

**The model never sees HTML.** It sees the same simplified view a screen reader
would: what each thing is, what it is called, and the text on each panel. That is
smaller, but the real reason is safety. If someone types instructions into a
customer note field hoping the model will obey them, that text arrives as the
label on a numbered row. It never arrives looking like part of our own
instructions. This does not solve the problem, because the model still has to
read the page. It does make the line between our instructions and the page's
own content easy to see.

**The model chooses which control, not how to find it.** It refers to a control
by its number in the list it was just shown. It cannot write its own way of
locating that control. If it could, that would be the one part of the saved
artifact nothing had checked or ranked. The recorder works out the ways to find
it, best first. The model supplies the intent; the recorder supplies the part
that has to survive.

## Artifact schema

A capability is a name, parameters, outputs, business rules, and an ordered list
of steps. The two decisions that matter:

**A target is a ranked list of ways to find something, not one selector.** When
recording, we work out every way that control could have been found and sort
them, most durable first. We also save the ways that did not work, and why.
Replay tries them in order and uses the first that hits.

Verbatim from the committed artifact, for the search field:

```
1. type [reversible] {member_id}
      -> labelled_field       'Member ID'   (0.85)  caption cell left of the field
         nth_of_role          'textbox[0]'  (0.40)  index 0 of 1; breaks if the page reorders
         dom_path             'body > table:nth-of-type(2) > ...' (0.20)
       x role_name            ''            control has no accessible name
       x role_name_in_region  ''            control has no accessible name
       x point                '346,96'      recorded for review only, never replayed
```

The rejected ones are saved on purpose. The first thing a reviewer asks is "why
is this finding the field by counting?" The answer is that this field has no
name, no label and no id worth trusting, so counting is all that is left. That
is a fact about the application, not a shortcut we took.

Notice what is missing. The field has an id that looks perfectly usable,
`ctl00_r3_c1`, and it appears nowhere in the artifact. It is the obvious thing to
grab, and it would work today. It is also regenerated every time the page
renders, so it would break tomorrow. `grep ctl00 capabilities/*.json` returns
nothing, and a test enforces that.

Every way of finding the control is tried against the live page before it gets
saved. It has to match exactly one thing, and that thing has to be the control we
mean, checked by identity rather than by counting matches. Anything that matched
nothing, matched several things, or matched the wrong thing gets moved to the
rejected list along with the number that disqualified it.

Saving a fallback we never tested would be worse than saving no fallback at all.
Replay would reach for it, act on the wrong control, and report success.

**A result is one of three answers, not a success-or-crash.**

```python
class Outcome(str, enum.Enum):
    SUCCESS           # the flow completed
    BUSINESS_OUTCOME  # the app answered, and the answer was "no"
    FAILURE           # the recording no longer matches the application
```

"No such member" is the right answer to a lookup that worked perfectly. Filing it
under FAILURE is the most common mistake in this kind of system, and it makes the
capability useless for the case it exists to serve: telling a caller the member
does not exist. `ReplayResult.ok` covers the first two.

The rules that say which screens mean "no such member" live in the artifact, not
in the engine. What a screen means is a fact about that particular application.
An engine with one bank's error messages baked into it is wrong for the next
bank.

## Determinism & error handling

`replay.py` imports nothing that can reach a model. Same artifact plus same
input gives the same actions.

Inside a step, three things happen in a fixed order, and each order matters:

1. **Check for trouble we can recover from, before checking the app's answer.**
   Otherwise a session timeout gets mistaken for the app answering us.
2. **Check the app's answer, before checking whether the step worked.** Otherwise
   a real "no such member" gets reported as a click that failed. This ordering
   has a cost, and it is written up in Cuts.
3. **Every step checks itself.** A click that never landed fails right there,
   rather than three steps later as some unrelated error.

The engine never sleeps for a fixed number of seconds. It always waits for a
condition to become true, so the thing it is waiting for and the thing it is
checking are the same thing. A slow page is treated as something to wait out: a
short budget first, then one longer try, and only then a verdict.

Every kind of retry is counted and named. Three attempts per step, one session
re-entry, three whole-flow re-entries. There is no generic "just try again".
Each one is written to the log, because a system that quietly recovers is a
system that can limp for months before anyone notices.

The full behavior. `tests/test_replay.py` drives the engine against the live
fixture and asserts the outcome of each, 34/34 passing, alongside 134 unit tests
covering perception, policy, escalation and the CLI:

| case | outcome | note |
|---|---|---|
| valid member | success | |
| unknown member | business_outcome | `member_not_found` |
| non-numeric input | business_outcome | `invalid_identifier` |
| interstitial, resumes the request | success | dismissed, step continued |
| interstitial, discards the request | success | dismissed, whole flow re-entered |
| 3s stall | success | widened timeout, recovery recorded |
| session expired | failure | re-entered once, then escalated |
| permission denied | failure | no retry; entitlements are a person's job |
| application error | failure | retried once, then failed |
| primary strategy missed | success | resolved on a fallback, degradation recorded |
| degraded flow that starts over | success | one degradation for the step, not one per attempt |
| operator resumed without fixing anything | failure | attempts ran out; the step never completed |
| operator corrected it | success | resume re-verified, the retried step arrived |
| every strategy missed | failure | `unresolved control`, evidence captured |
| host not allowlisted | failure | refused before the browser opened |
| unapproved draft | failure | refused |
| read-only lane vs a write step | failure | refused |
| mistyped parameter | raises | caller's bug, not a replay outcome |


## Heterogeneity & multi-tenant

Same vendor product, two institutions: different brand, different field caption
("Member ID" vs "Account Number"), different route.

A recording made at bank A, replayed against bank B, **worked**. How it worked is
the whole argument for keeping a ranked list instead of one selector:

```
step 1 type
   won    : nth_of_role 'textbox[0]' (conf 0.4)
   failed : labelled_field 'Member ID' -- matched nothing
```

The best way of finding the field missed, because bank B captions it "Account
Number". The backup caught it. `result.json` just says `success`. **Only the log
shows that it limped.**

A run that only passed because it fell back to counting is one release away from
not passing at all. So falling back is recorded rather than shrugged off: the
step's log line carries which rank won, and dropping below the best one writes a
warning naming both.

```
target degraded to nth_of_role ('link[0]'); primary was role_name
```

The result carries it too. A step that resolved on a fallback appears under
`degraded`, naming the strategy that was recorded, the one that actually found
the control, and how far down the list it sat:

```json
"degraded": [{"step": 1, "primary": "role_name", "won_by": "nth_of_role", "rank": 2}]
```

Without that field a clean run and a limping one are the same object with
different log files. `tests/test_replay.py` pins the strategy that won, so a
change that starts resolving the control some other way turns the case red
instead of passing quietly.

What is **not** built is anything that adds those up across runs. No threshold,
no dashboard, no ticket when a capability has been limping for a week. Every run
says so; nothing counts them:

| case | expected |
|---|---|
| primary strategy missed, fallbacks intact | success, degraded |
| every strategy missed | failure, `unresolved control` |

The artifact carries `app_id` and `app_variant`, so that one recording could
carry small per-bank corrections instead of being recorded again for every bank.
The code that would merge those corrections is not written.

## Escalation & handoff

Escalation is not the error path. It is the normal path for the awkward cases
that will always exist, and how well it works decides whether anyone trusts the
system with the ordinary ones.

**The session is not thrown away.** The expensive part of one of these tasks is
where you already are: logged in, the right record open, three screens deep. If
handing over to a person means they start from the login page, they will stop
using the automation and just do the whole job by hand. So the browser stays
exactly where it is, and that same page is handed over.

**Exactly one party owns the page at a time, and the code enforces it.** Control
moves `AUTOMATION -> OPERATOR -> RESUMING -> AUTOMATION`, and any other move
raises an error.

`RESUMING` is its own state for a reason. Between the moment a person hands
control back and the moment we have confirmed where the page is, **nobody** is
allowed to act, because neither side knows what is on screen. A wrapper around
the page refuses any call from whoever is not the current owner. An old reference
raises an error instead of quietly typing into a screen a person is halfway
through fixing.

**A resume gets the attempt it asked for.** Escalation for a control that cannot
be found only ever happens on a step's last attempt, because the earlier ones are
spent looking again. So handing back "fixed" used to arrive with no attempt left,
the step fell out of its loop unfinished, and the run reported success with the
click never sent. A resume now extends the step's budget, twice at most, and a
step whose attempts run out is a failure rather than a step quietly skipped.

**Taking control back means checking, not assuming.** While the person had it
they may have gone somewhere else entirely, or finished the job themselves. So on
resume we re-check the last thing we know was true, and refuse if it no longer
is:

```
ResumeRefused: page moved during handoff:
  expected text_present='Member Detail', saw 'Member Search'
```

Having nothing confirmed to check against is also a refusal. It is not a reason
to guess.

What the person receives is not a blank terminal. It names the capability and the
step, says what was expected and what was actually on screen, and includes the
URL, a screenshot, and the folder holding everything else from that run.

## Safety

Every gate is code that runs before the action is sent, not an instruction in a
prompt asking the model to behave. A refusal that arrives after the click has
already landed is not a control, it is a log line.

**Where it may go.** The host, and the part of the site, checked against the URL
the browser is actually on rather than the one written in the artifact. The
artifact is just a file, and a file can be out of date, or written by whoever
filed the ticket. A capability is not a general-purpose browser pointed at
everything the login session can reach.

**What it may do.** Which kinds of action are allowed. `policy.read_only()` runs
the very same artifact with anything that writes turned off, so there is no
second copy to keep in step.

**How dangerous each step is.** Decided once, while recording, and reviewed by
the person who approves the artifact. It is not judged in the moment by anything
at runtime. A step marked `IRREVERSIBLE` is refused by default: with nobody
configured to approve it, it stops rather than going ahead unwatched.

**Approval.** A freshly recorded flow is a draft and will not replay until a
person approves it. That approval is a plain flag in the file. Nothing ties it to
the file's contents, so editing an approved artifact does not un-approve it. That
is written up in Cuts.

**It never handles credentials.** A session that has timed out always goes to a
person. The automation has no password to type.

**Redaction on the way out.** Account numbers, SSNs, emails and phone numbers are
stripped from evidence and logs when they appear in a form the patterns recognize:
next to a caption, or under a key named `password`/`token`/`secret`. Values returned
to the caller are left alone, because the caller asked for the balance. What
nobody needs is our own copy sitting on disk.

This does not work for a bare value with nothing around it. `redact("12345")`
returns it unchanged, and a step that types something writes down what it typed.
So the log records `member_id: [REDACTED]` and then, two lines later, records
`text: "12345"`. All of the cost of redacting and none of the benefit, and it is
in the committed evidence. The fix is to redact what a step typed. It is not
done.

The unattended operator console never answers "resume" and never approves. An
automatic yes would leave the escalation path untested in exactly the way that
matters.


## Cuts

Deliberately not built, in rough order of how much they would matter:

- **There is no good way to find a value by its label.** Values can now be read
  back, but the ways of finding things were designed for buttons and boxes. The
  best one is "find the thing whose text is X" -- and for a balance, that text
  *is* the answer we recorded. It can only ever match the member we recorded
  against. So every read for a different member falls back to counting boxes,
  and works on position alone. It works, and the log says so, but the landmark
  that would actually last is one this does not have: *the box to the right of
  the box that says "Savings Balance"*. The system already does exactly that
  trick for typing into form fields. Extending it to values is the next thing
  worth building here.

  This is not just about durability. Counting boxes is wrong **quietly**, where
  everything else is wrong loudly. Insert one "Member Since" row above the
  balances and every box below it shifts down, so the recorded read comes back
  with:

      truth       savings 76230.18    checking 9902.77
      returned    savings 2019-04-11  checking 76230.18   exit 0, "flow completed"

  A date, handed back as a savings balance, with a success code.

  A landmark that has *vanished* stops the run and screenshots the page. A
  landmark that merely *moved* does not, because finding a seventh box looks
  exactly like finding the seventh box. Reads carry this risk and clicks mostly
  do not: a click that lands on the wrong thing usually fails its own check,
  while a read has nothing to contradict it. Anchoring a read to its label is
  what closes this.

  The suite now at least *detects* it. `tests/test_replay.py` replays a read for
  three members whose balances differ and requires three different answers, so a
  capability recorded one cell to the left -- which hands every member the
  caption "Savings Balance" as their balance, with every check in the system
  passing -- turns the case red. Replaying against the member it was recorded
  against cannot do this: the recorded balance is the recorded target, so that
  one member agrees with a capability pointed almost anywhere.

- **No identity, no audit trail.** The rules control *what* may run, never *who*
  asked for it. Limiting what each caller may do, and keeping a record of who
  approved what, belong to whatever system calls this one. Named here rather
  than half-built.
- **The operator console is a terminal prompt, not a real one.** Everything
  behind it is real: who owns the page, the handover packet, the re-check on
  resume. In a real deployment the console would be a work queue, a review
  screen, a logged-in operator and a shared browser. `OperatorConsole` is the one
  piece that gets replaced. Nothing behind it changes.

- **One run at a time.** One browser session, one run. Nothing shares sessions,
  and nothing stops two runs touching the same customer record at once.
- **Discovery explores one path, and never checks its own work.** The live run
  records the successful route and stops. It does not probe the not-found or
  validation branches, so the business rules that separate an answer from a fault
  are authored by the reviewer afterwards. `evidence/README.md` shows the cost:
  the recorded artifact called an unknown member a failure until two rules were
  added by hand. Nor does it replay the draft once before handing it over, so
  "the goal was met" is the model's own word for it. Trying one bad input during
  recording, and replaying the draft against the recorded input, are the first
  two things I would add.


### On the fixture

The fake bank app is a test target, not part of what is being delivered. Its
failures are switched on with a query parameter rather than happening on their
own. That makes them repeatable, which is what let every branch be tested. It
also means the rules for recognizing trouble are matched against text I wrote
myself.

On a real application those rules would be written per bank, and they would be
the fiddliest part of setting up a new one. That is exactly why they sit in a
plain table in `replay.py` rather than being spread through the code. They are
data for someone to edit, not logic for someone to rewrite.
