# The action succeeded. The worker disappeared.

This is the v0.2 case study. See [v0.3 hardening and model evaluation](hardening.md)
for subsequent authentication, submission deduplication, reconciliation and tests.

A tool can commit an action and lose its response before a worker saves success.
Retrying without a stable key can duplicate the action; refusing every retry can
leave recoverable work stuck. This project makes that failure boundary observable
and tests a bounded recovery contract.

## Design and tradeoffs

The API writes a run, sequential steps and an initial event in one PostgreSQL
transaction. Workers claim eligible steps with row locks, a fresh claim token and
a renewable lease. The record tool runs over HTTP in another process and stores
effects in SQLite. Its commit is deliberately outside the runner transaction.

| Choice | Benefit | Cost or boundary |
|---|---|---|
| PostgreSQL queue with SKIP LOCKED | Claims and state share one transactional store | Polling and database contention; no throughput benchmark claimed |
| One lock order: run, then step | Cancellation and completion serialize; per-run event cursors are safe | Steps within one run are sequential |
| UUID claim token plus database-clock lease | A stale process cannot overwrite a replacement, even with the same worker name | Lease expiry cannot stop a remote HTTP action already in flight |
| Stable provider idempotency key | Retry can recover the original effect | Requires provider support and retained provider storage |
| Persisted retry time and jitter | Restart does not bypass backoff | No priority queue or dynamic rate-limit scheduling |
| Explicit needs_review terminal state | Unknown outcomes remain visible | Operator reconciliation is required; no automatic compensation |

An idempotent provider protects the effect. Claim fencing protects the runner's
state. These are different responsibilities. Neither offers universal exactly-once
execution for arbitrary external APIs.

## Failure evidence

The PostgreSQL-backed suite launches an independent HTTP provider, waits for its
effect to commit, kills the worker process, waits for lease expiry and starts a
replacement. The replacement replays the same key, reads the existing effect and
finishes on attempt two. The trace includes step_claimed, step_reclaimed,
tool_replayed and run_succeeded.

Additional cases cover competing workers, stale completion and renewal with reused
worker names, cancellation racing completion, cancellation after an effect,
ambiguous outcomes during backoff, exhausted attempts, slow-tool heartbeats,
conflicting provider payloads, sequential steps and event cursor resumption.
Migration tests preserve valid legacy rows and reject missing tables, wrong column
types and missing unique constraints. Proposal tests reject unknown tools, changed
ticket IDs, invalid labels and unexpected fields.

Run `make test`, then `python scripts/smoke.py` against Compose. The latter submits
two real steps, checks their effects and ordered events, resumes a cursor and
checks both metric endpoints. GitHub Actions repeats the suite and composed smoke
test. These tests are not a substitute for production load or availability testing.

## Local model experiment: reliability is not accuracy

A pinned SmolLM2-135M-Instruct model scores billing, access and other labels on CPU.
Application code wraps that label in a schema-checked record proposal, preserves
the source ticket ID and executes the durable workflow. It cannot choose arbitrary
tools, URLs or executable code. This is a constrained classifier integration.

| Prompt | Correct labels | Validated and completed workflows |
|---|---:|---:|
| Zero-shot baseline | 3 / 9 | 9 / 9 |
| Three independent demonstration examples | 4 / 9 | 9 / 9 |

Both raw reports are retained in [evidence](../evidence/). The second prompt was
developed after inspecting the first result on these same nine synthetic cases.
This is a development set, not a held-out benchmark. The classifier strongly
favours billing and is unsuitable for unattended ticket routing. Successful
execution only means the selected label was faithfully recorded.

Reproduce the baseline with `--prompt zero-shot` and the second run with
`--prompt few-shot` in `python -m examples.triage --api-url http://127.0.0.1:8000`.
Use distinct `--output` paths to retain both reports. No API key, GPU or paid
service is involved. A useful follow-up would compare a stronger local model on a
separate held-out set; it is not necessary to claim the execution service works.

## Release boundary

This is a local, inspectable backend reference project. Authentication, tenant
isolation, arbitrary tool adapters, DAG scheduling, load/soak evidence and an
automated reconciliation interface remain outside this release. Stop old workers
before migration. Do not expose the unauthenticated API publicly. See
[operations](operations.md) for recovery and upgrade procedures.
