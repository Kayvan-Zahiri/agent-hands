# Evidence

One directory per run. `run.jsonl` is append-only and flushed per line, so a
process killed mid-step still leaves the steps before it. `result.json` is
written last and is the only file that means "this run finished".

## The recording

| run | what it shows |
|---|---|
| `...002140Z_record-member-lookup-live_d061f6` | a real Claude Opus 5 run driving the fixture, 3 turns |

Every action the model took, with its own stated reason and the strategy the
perception layer derived for it:

```
type   control=3 via labelled_field   why='Enter the member ID into the search field'
click  control=4 via role_name        why='Submit the member search'
```

The model chose control *3*, not a selector. It was shown a numbered list of
controls from the accessibility tree and picked one; `labelled_field 'Member ID'`
was derived by the recorder, not written by the model. That split is deliberate,
and the evidence is where you can check it held.

The goal reads `Look up member [REDACTED:ACCOUNT] ...` because the redactor
treats a five-digit identifier as an account number. That is over-redaction of a
value that is not very secret, and it is the intended direction of the error: a
run directory that is slightly harder to read is a much better failure than one
that cannot be attached to a ticket.

## The replays

| run | outcome | what it shows |
|---|---|---|
| `...233224Z_member-lookup_5dfaee` | success | the happy path |
| `...234201Z_member-lookup_21da9b` | business_outcome `member_not_found` | the app answered "no". not a failure |
| `...234229Z_member-lookup_86898f` | business_outcome `invalid_identifier` | input rejected by the app's own validation |
| `...234652Z_member-lookup_403428` | success, **degraded** | the same artifact against a second tenant. the recorded primary strategy missed and a 0.4-confidence fallback caught it. `result.json` says success; only `run.jsonl` shows the slide |
| `...234414Z_member-lookup-stale_542a17` | failure | a recording that no longer matches the app. all four strategies missed; screenshot, accessibility snapshot and intervention packet captured |

The failure run is the one worth opening. `step-02-failure.png` is what a person
would have seen; `step-02-failure-a11y.json` is what the targeting layer saw.
Most confusing failures are a disagreement between those two.

## What the recording proves

The artifact recorded from `member 12345` replays on members the model never
saw, because the literal was parameterised at record time:

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
