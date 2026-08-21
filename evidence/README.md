# Evidence

**What you are looking at.** Every time this system drives the banking
application, it saves everything about that attempt into one folder here: every
step it took, how long each took, what the screen said, and screenshots when
something went wrong. That folder is how you check what it really did rather
than taking its word for it.

The folders named `record-*` are the one-off runs where an AI worked a task out.
The rest are replays: ordinary code following the recipe the AI wrote, which is
the path that runs in production.

**The two files that matter.** `result.json` is the verdict — did it work, did
the application say no, or did it break. `run.jsonl` is the blow-by-blow.

One directory per run. `run.jsonl` is append-only and flushed per line, so a
process killed mid-step still leaves the steps before it. `result.json` is
written last and is the only file that means "this run finished".

## Against MERIDIAN CORE, the hosted target

Seven bundles, chosen because between them they cover every way a run can end.

| run | what it shows |
|---|---|
| `...212018Z_record-meridian-transfer-recorded_7c690e` | a model working out how to move money, 11 turns, 46s |
| `...213457Z_meridian-funds-transfer_9cfe58` | the same job replayed with no model: `success`, confirmation `CN480137` |
| `...213516Z_meridian-funds-transfer_1f3b52` | the same job again with nobody authorized to approve it: stops at step 11, **before the write**, and leaves an intervention packet with screenshots |
| `...035523Z_meridian-transfer-recorded_6efd79` | the same stop, answered: a person resumed it in the console and the transfer posted, confirmation `CN480202`. `run.jsonl` carries both escalation lines, the second with the answer |
| `...133705Z_meridian-transfer-recorded_546c74` | the same stop, walked away from: the operator closed the browser window instead of answering, and the run ended in about a second with `declined: the browser window was closed`. Nothing written |
| `...213458Z_meridian-place-hold_8acbbe` | a teller attempting a supervisor-only action: `business_outcome / supervisor_required`, not a crash |
| `...211103Z_meridian-balance-recorded_2955f5` | a read that still worked and should not be trusted — see below |

### The one worth opening

`...211103Z_meridian-balance-recorded_2955f5` returns `success` and a plausible
number, and it is wrong. The capability is named `savings_balance`. It was
recorded against member 103001's **Money Market** account and replayed for member
100234, where it returned `$198.04` — that member's **Share Draft (Checking)**
balance.

Nothing in the run contradicts it. The checkpoint held, the declared type is
`number` and `$198.04` is a number. The only signal is one line in `result.json`:

```json
"degraded": [{"step": 9, "primary": "role_name", "won_by": "nth_of_role", "rank": 2}]
```

The recorded target for that read was `cell "$22.37"` — the answer from the
recording. It can only ever match the member it was recorded against, so every
other member falls through to counting cells in a table whose length varies. This
is the first item in REPORT.md's Cuts, and this directory is it happening.

## The fixture recording

| run | what it shows |
|---|---|
| `...002140Z_record-member-lookup-live_d061f6` | a real Claude Opus 5 run driving the local fixture, 3 turns |

Every action the model took, with its own stated reason and the strategy the
perception layer derived for it:

```
type   control=3 via labelled_field   why='Enter the member ID into the search field'
click  control=4 via role_name        why='Submit the member search'
```

The model chose control *3*, not a selector. It was shown a numbered list of
controls from the accessibility tree and picked one; the way of finding it again
was derived by the recorder, not written by the model. That split is deliberate,
and the evidence is where you can check it held.

The transcript above spells that strategy with two l's because that is what the
run wrote at the time. Evidence is a record of what happened, so it is never
edited; the code spells it `labeled_field` now and still reads the old spelling
back.

The goal reads `Look up member [REDACTED:ACCOUNT] ...` because the redactor
treats a five-digit identifier as an account number. That is over-redaction of a
value that is not very secret, and it is the intended direction of the error: a
run directory that is slightly harder to read is a much better failure than one
that cannot be attached to a ticket.

## The replays

| run | outcome | what it shows |
|---|---|---|
| `...233224Z_member-lookup_5dfaee` | success | the happy path |
| `...234201Z_member-lookup_21da9b` | business_outcome `member_not_found` | the app answered "no." not a failure |
| `...234229Z_member-lookup_86898f` | business_outcome `invalid_identifier` | input rejected by the app's own validation |
| `...234652Z_member-lookup_403428` | success, **degraded** | the same artifact against a second tenant. the recorded primary strategy missed and a 0.4-durability fallback caught it. `result.json` says success; only `run.jsonl` shows the slide |
| `...234414Z_member-lookup-stale_542a17` | failure | a recording that no longer matches the app. all four strategies missed; screenshot, accessibility snapshot and intervention packet captured |

The failure run is the one worth opening. `step-02-failure.png` is what a person
would have seen; `step-02-failure-a11y.json` is what the targeting layer saw.
Most confusing failures are a disagreement between those two.

## What the recording proves

The artifact recorded from `member 12345` replays on members the model never
saw, because the literal was parameterized at record time:

```
member_id=12345 (recorded with)  success
member_id=22887 (never seen)     success
member_id=30021 (never seen)     success
member_id=99999 (never seen)     failure  <- checkpoint failed
```

That last line is the honest one. The discovery agent records one successful
path and never learns which screens are *answers*, so an unknown member was
misclassified as a broken automation, which is precisely the mistake `Outcome`
exists to prevent. Adding two business rules is a reviewer's job, and doing it
cleared the approval and bumped the artifact to v2:

```
replay while unapproved      failure  refused before starting
after re-approval:
  member_id=99999            business_outcome  member_not_found
  member_id=abcde            business_outcome  invalid_identifier
  member_id=22887            success
```

An approval that survives an edit approves something nobody read.
