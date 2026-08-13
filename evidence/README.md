# Evidence

One directory per run. `run.jsonl` is append-only and flushed per line, so a
process killed mid-step still leaves the steps before it. `result.json` is
written last and is the only file that means "this run finished".

| run | outcome | what it shows |
|---|---|---|
| `...233224Z_member-lookup_5dfaee` | success | the happy path, member 12345 |
| `...234201Z_member-lookup_21da9b` | business_outcome `member_not_found` | the app answered "no". not a failure |
| `...234229Z_member-lookup_86898f` | business_outcome `invalid_identifier` | input rejected by the app's own validation |
| `...234652Z_member-lookup_403428` | success, **degraded** | the same artifact against a second tenant. the recorded primary strategy missed and a 0.4-confidence fallback caught it. `result.json` says success; only `run.jsonl` shows the slide |
| `...234414Z_member-lookup-stale_542a17` | failure | a recording that no longer matches the app. all four strategies missed; screenshot + accessibility snapshot + intervention packet captured |

The failure run is the one worth opening. `step-02-failure.png` is what a person
would have seen; `step-02-failure-a11y.json` is what the targeting layer saw.
Most confusing failures are a disagreement between those two.

Missing: a discovery run. That needs an API key, which the machine this was
built on does not have. Everything else here was produced by the deterministic
engine, which needs no credentials.
