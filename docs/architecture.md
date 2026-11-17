# Architecture and failure boundaries

HTTP API → PostgreSQL (runs, steps, events) → workers → HTTP provider → SQLite effects.

## Claims and lock order

Every claim receives a UUID token independent of the human-readable worker ID.
Claiming locks a runnable Run with FOR UPDATE SKIP LOCKED, then its next Step.
Completion, failure and cancellation acquire locks in the same Run → Step order.
This reduces parallelism within a run deliberately: v1 steps are sequential.

Before a terminal or retry update, the worker verifies the claim token, running
state, and lease expiry while holding both locks. Lease renewal is a conditional
SQL UPDATE on the same token, state and unexpired lease. All lease decisions use
PostgreSQL clock_timestamp(), avoiding differences between worker clocks. An
expired claim cannot renew itself, even before a replacement arrives.

A heartbeat covers the HTTP call and result persistence. If the database becomes
unavailable, renewal can resume only while the lease is still live. The worker
poll loop tolerates database restart. The replacement worker may call a provider
while a stale process is still running: the provider's idempotency key protects
effects, while the claim token protects runner state. Neither alone is sufficient.

## Cancellation

| State at cancellation | Result |
|---|---|
| Queued, never attempted | Cancelled, no call starts |
| Running | Cancelling; wait for known outcome or lease expiry |
| Running, provider confirms effect | Step succeeded, run cancelled, later steps cancelled |
| Running, provider explicitly rejects | Cancelled with recorded rejection |
| Lost response, expired running claim, or pending ambiguous retry | Needs review; no automatic new call |
| Terminal | No change |

Cancellation is not rollback. Needs-review runs retain the stable key and trace so
an operator can reconcile against the provider's persisted effect. Creating a replacement run with a new key may
duplicate a prior effect and is not an automatic reconciliation mechanism.

## Retries and provider contract

Transport errors, 5xx replies, malformed success responses and unexpected adapter
failures are ambiguous. Replay uses the same key and persisted available_at time.
Delay is uniformly sampled between half and all of
min(cap, base × 2^(attempt - 1)); defaults are base 1 second, cap 30 seconds,
three attempts. Exceeding that budget ends ambiguous work in needs_review.

4xx provider rejection is terminal except for transient 408 and 429 responses.
The record provider atomically inserts a
unique idempotency key; a collision compares canonical JSON, returning the existing
result only when the payload matches. Different payloads receive 409, including
JSON booleans versus numbers. Effects are stored outside runner transactions.

## Events and snapshots

A database sequence orders event IDs, but a sequence alone does not establish
commit order. Every writer holds the Run lock while allocating and committing
events, so readers cannot skip a late-committing lower ID within that run.
Snapshots take a shared Run lock while reading run, steps and events. Cursors
are per-run; no global cross-run ordering guarantee is made.

## Schema evolution

0001 can create a new schema or validate the exact original prototype layout.
0002 validates the baseline again (including databases previously stamped by
the permissive migration), then adds claim_token and available_at. Validation is
frozen against the baseline rather than derived from current ORM definitions.
Extra legacy tables such as tool_effects are retained. Stop old workers before
upgrading; rolling mixed-version workers are unsupported.

0003 adds nullable submission keys and request hashes without modifying existing
runs. A unique submission-key constraint makes concurrent client retries converge
on one run. Conflicting normalized payloads are rejected. These keys deduplicate
API submissions; step keys separately deduplicate provider effects.

## Observability and limits

API and each worker have separate Prometheus registries. Metrics describe
process-local counters and tool-call duration; database events retain the audit
history across restarts. Prometheus scraping aggregates process metrics.

The reference service is local-only by default, optionally protected by a single
operator token, and intentionally sequential within each run.
It does not establish production throughput, arbitrary exactly-once APIs, or
model quality. Its AI example scores allowlisted categories, validates proposals,
and measures classification separately from durable execution.
