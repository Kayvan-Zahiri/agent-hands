# agent-hands: design report

## Architecture

A model drives the application once, at record time, and produces a **capability
artifact**. A deterministic engine replays that artifact, with no model in the
loop.

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

The split is the design. Everything expensive and non-deterministic happens once
and gets reviewed; everything repeated is a fixed list of steps someone signed.

Two consequences worth stating, because they are what make it work:

**The model never sees HTML.** It receives the accessibility view: roles, names,
and the text of each frame. That is a smaller context, but the real reason is
prompt injection. A hostile string in a customer note field arrives as *the
content of a cell*, inside a numbered list of controls, rather than as text that
looks like part of the instructions. This does not eliminate the problem, since
the model must read the page to work, but it makes the boundary legible.

**The model chooses which control, not how to address it.** Actions name a
control by its index in the observation it was just shown. It cannot write a
selector, because a model-authored selector is the one part of the artifact no
ranking logic ever touched. The perception layer derives the ranked strategies.
Intent comes from the model; durability comes from the recording.

## Artifact schema

A capability is a name, parameters, outputs, business rules, and an ordered list
of steps. The two decisions that matter:

**A target is a ranked list, not a selector.** Recording captures every way the
control could have been identified, ordered by expected survival, together with
the ones that were *rejected and why*. Replay walks the list.

```
1. type [reversible] {member_id}
      -> labelled_field   'Member ID'    (0.85)  caption cell left of the field
         nth_of_role      'textbox[0]'   (0.40)  breaks if the page reorders
         dom_path         'body > table:nth-of-type(2) > ...' (0.20)
       x role_name        ''             control has no accessible name
       x dom_path         '#ctl00_r3_c1' id looks server-generated
       x point            '346,96'       recorded for review only, never replayed
```

The rejections are in the artifact on purpose. "Why is this matching by
position?" is the first question a reviewer asks, and the answer, that the
control has no accessible name, is a fact about the application rather than a
shortcut the recorder took. The server-generated id is recorded as *declined*,
not used: it would pass today and fail silently next release.

**A result is a three-way tagged union, not an exception.**

```python
class Outcome(str, enum.Enum):
    SUCCESS           # the flow completed
    BUSINESS_OUTCOME  # the app answered, and the answer was "no"
    FAILURE           # the recording no longer matches the application
```

"No such member" is the correct result of a lookup that ran perfectly.
Collapsing it into FAILURE is the single most common mistake in this kind of
system and makes the capability useless for the case it was built for.
`ReplayResult.ok` covers the first two.

Business rules live in the artifact rather than the engine, because which
screens mean what is a fact about the application. A shared engine that
hard-codes one tenant's error strings is wrong for the next one.

## Determinism & error handling

`replay.py` imports nothing that can reach a model. Same artifact plus same
input gives the same actions.

Three orderings inside a step, each load-bearing:

1. **Recoverable conditions before business rules.** Otherwise a session timeout
   gets read as an answer.
2. **Business rules before the checkpoint.** Otherwise a legitimate "not found"
   is reported as a broken click.
3. **Every step verifies itself.** A click that did not arrive fails at that
   step, not three steps later as an unrelated extraction error.

There is no `sleep` in the engine. Every wait is a Playwright condition wait, so
the assertion is also the synchronisation. A slow page becomes a recovery
(short budget, then one widened retry) rather than a verdict.

Recovery is bounded and named, never generic retry: three attempts per step, one
session re-entry, three flow re-entries. Each is written to the evidence log,
because silent recovery is how a system degrades for months unnoticed.

The full behaviour, all verified against the fixture:

| case | outcome | note |
|---|---|---|
| valid member | success | |
| unknown member | business_outcome | `member_not_found` |
| non-numeric input | business_outcome | `invalid_identifier` |
| interstitial | success | dismissed, one recovery recorded |
| 3s stall | success | widened timeout, recovery recorded |
| session expired | failure | re-entered once, then escalated |
| permission denied | failure | no retry; entitlements are a person's job |
| application error | failure | retried once, then failed |
| host not allowlisted | failure | refused before the browser opened |
| unapproved draft | failure | refused |
| read-only lane vs a write step | failure | refused |
| mistyped parameter | raises | caller's bug, not a replay outcome |

One bug this surfaced, worth recording because it is a correctness issue rather
than a tidiness one. After a recovery, the engine originally re-ran the step's
action. For an interstitial that meant navigating straight back into the
interstitial. On an `IRREVERSIBLE` step it would have meant **submitting twice**.
The fix is to ask whether the recovery already achieved the step's goal before
re-acting:

```python
if attempt > 1 and step.checkpoint is not None and self._already_there(step):
    record.extra["satisfied_by_recovery"] = True
    return None
```

## Heterogeneity & multi-tenant

Same vendor product, two institutions: different brand, different field caption
("Member ID" vs "Account Number"), different route.

Replaying the tenant-A artifact against tenant B **succeeded**. The interesting
part is how, and it is the argument for ranked targets:

```
step 1 type
   won    : nth_of_role 'textbox[0]' (conf 0.4)
   failed : labelled_field 'Member ID' -- matched nothing
```

The recorded primary strategy missed, because their caption is different, and
the fallback caught it. The `result.json` says `success`, and **only the
evidence file shows the degradation**.

That cuts both ways and I want to be honest about it. A run that passes on a
0.4-confidence fallback is one release away from not passing. The right
operational answer is to alert on strategy degradation, not just on failure, and
that alerting is not built here. The artifact carries `app_id` and `app_variant`
so a variant can override individual targets rather than forcing a re-record per
tenant, but the override merge is not implemented either.

## Escalation & handoff

Escalation is not the error path. It is the normal path for the long tail, and
how well it works decides whether anyone trusts the system with the rest.

**The session is not thrown away.** The expensive part of a back-office task is
the state: logged in, right record open, three screens deep. An escalation that
ends the session makes the person redo all of it, so they stop escalating and
start doing the whole task by hand. The browser stays where it is and the same
page is handed over.

**Ownership is explicit and enforced.** `AUTOMATION -> OPERATOR -> RESUMING ->
AUTOMATION`, with illegal transitions raising. `RESUMING` is a distinct state
rather than a flavour of automation: between the handback and a verified
checkpoint, nobody may act, because neither party knows where the page is. An
`OwnedPage` wrapper refuses calls from whichever party is not the current owner,
so a stale reference raises instead of quietly typing into a screen a person is
halfway through fixing.

**Resuming re-verifies rather than assumes.** While the person had control they
may have navigated elsewhere or already finished the task. On resume the last
confirmed checkpoint is re-checked and disagreement refuses:

```
ResumeRefused: page moved during handoff:
  expected text_present='Member Detail', saw 'Member Search'
```

No confirmed checkpoint is itself a refusal, not a reason to guess.

The handoff packet carries the capability, step, what was expected, what was on
screen, the URL, a screenshot and the evidence directory, so the first thing the
operator sees is a diagnosis rather than a blank terminal.

## Safety

Gates are structural and checked before dispatch, never negotiated in a prompt.
A refusal that arrives after the click has landed is not a control.

**Surface.** Host *and* path prefix, checked against the live URL rather than the
artifact's `entry_url`, because the artifact is data and data can be stale or
supplied by whoever filed the ticket. A capability is not a general-purpose
browser pointed at everything the session cookie can reach.

**Action.** Which action kinds a lane permits. `policy.read_only()` runs the same
artifact with writes disabled, rather than maintaining a second artifact.

**Risk.** Classified once at record time and reviewed by the person who approves
the artifact, not judged at runtime. `IRREVERSIBLE` is default-deny: with no
approver configured it fails closed rather than proceeding unattended.

**Approval.** A freshly recorded flow is a draft and will not replay. Approval is
recorded against the artifact version.

**Credentials are never handled.** A session timeout always goes to a person.

**Redaction on the way out.** Account numbers, SSNs, emails and phone numbers are
stripped from evidence and logs, and keys named `password`/`token`/`secret` are
replaced wholesale. Values returned to the caller are not redacted: the caller
asked for the balance. What nobody is entitled to is our copy on disk.

The unattended operator console never answers "resume" and never approves. An
automatic yes would leave the escalation path untested in exactly the way that
matters.

One bug worth naming, because it was silent and it was a *safety* bug. `save`
serialises the whole capability but `load` rebuilt it field by field, so a field
the reader did not know about was dropped. `approve` loads and saves, so
approving an artifact **deleted its business rules**, and the loss showed up
later as an unrelated-looking checkpoint failure. Any field added to
`Capability` has to be added to `capability_from_json` in the same change; the
comment there now says so.

## Cuts

Deliberately not built, in rough order of how much they would matter:

- **Identity and audit.** The policy layer gates *what* runs, not *who* asked.
  Per-caller scoping and an attributable approvals trail belong to whatever calls
  this. Named rather than half-built.
- **The operator console is a terminal stub.** Everything above it, the
  ownership machine, the packet, the re-verification, is real. In a deployment
  the console is a queue, a review UI, an authenticated operator and a shared
  browser session. `OperatorConsole` is the seam; nothing above it changes.
- **Degradation alerting.** The evidence records that a run passed on a fallback.
  Nothing watches for it, which is the gap the multi-tenant result exposes.
- **Variant target overrides.** The schema carries `app_variant`; the merge that
  would let one artifact carry per-tenant target overrides is not written.
- **Native desktop.** The accessibility abstraction was chosen partly because
  UIA and AX expose the same role-and-name vocabulary, so the targeting layer
  should carry over. No backend is implemented, so that is a claim, not a result.
- **Business rules are hand-authored.** The discovery agent does not explore the
  not-found or validation branches to learn them. It records one successful path.
- **Discovery records one path.** No branch exploration, no self-verification of
  the artifact by replaying it before handing it over. Replaying the draft once
  against the recorded input would be a cheap, high-value addition.
- **Concurrency.** One session, one run. No pooling, no locking across runs
  against the same record.

### On the fixture

It is a fixture, not a deliverable, and its failure modes are switched by query
parameter rather than arising naturally. That makes them deterministic, which is
what let each branch be tested, but it does mean the recovery rules are matched
against text I also wrote. On a real application those rules would be authored
per tenant and would be the fiddliest part of onboarding. The rules are a
`CONDITIONS` table in `replay.py` for exactly that reason: they are data someone
edits, not logic someone rewrites.
