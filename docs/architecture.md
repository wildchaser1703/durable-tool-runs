# Architecture decisions

## PostgreSQL is both the state store and the work queue

`runs`, `steps`, and `run_events` are persisted in PostgreSQL. Workers claim a step with `SELECT … FOR UPDATE SKIP LOCKED`, commit a lease, and execute outside that lease transaction. Schema changes are applied through Alembic before API and worker containers start.

This keeps the recovery record and the work-selection record together. A separate queue would improve throughput at a larger scale, but would create another delivery boundary to reason about. The v1 tradeoff is deliberate: one inspectable database is easier to recover and test.

## Leases recover worker loss

A `running` step has an owner and an expiry. A new worker may reclaim it after expiry. A worker never assumes that an in-memory task survived a restart.

Expiry is not a success signal. Each step has a bounded attempt budget. After that budget is exhausted, the run becomes `failed`; downstream queued steps are cancelled and the event trail records the reason.

A heartbeat renews the lease while an active worker waits for a tool response. This prevents a healthy slow call from being reclaimed simply because its initial lease elapsed. A permanently stuck worker still stops heartbeating and becomes reclaimable.

## Idempotency is the external-effect boundary

The record provider is a separate HTTP process with its own durable SQLite store. That exposes the important crash window:

1. A worker claims a step.
2. The tool commits an effect.
3. The worker dies before it stores the tool result.
4. Another worker reclaims the expired lease.

The replacement worker calls the provider with the same stable key, `run_id:step:position`. The provider returns the earlier result instead of creating a second effect. The integration suite starts a real worker subprocess, waits until it has received that response, kills it, waits for its lease to expire, and lets a second worker reclaim the step.

This provides effectively-once effects only for tools with an idempotency contract. No database transaction can make an arbitrary remote API exactly-once. Transport failures and provider 5xx responses are ambiguous, so the runner retries them using the original key; provider 4xx responses are terminal. A real non-idempotent provider needs a compensating action or a `needs_review` state; it must not be silently retried.

## Operational signals

Metrics are intentionally process-local, as Prometheus expects. The API exposes accepted runs at `/metrics`; every worker exposes its own `/metrics` target with claim/reclaim outcomes, provider-call outcomes, and a step-execution duration histogram. Structured events remain the per-run diagnostic trace; metrics expose fleet-level health.

## Events are replayable, not merely logs

Each event receives a monotonic database sequence. A reconnecting client uses `GET /runs/{id}/events?after={event_id}` to fetch only events it has not observed. UUIDs and timestamps do not define a reliable causal order when events are committed in the same transaction.

## Explicit non-goals for v1

- No model provider, prompt planner, vector database, or agent framework.
- No authentication or multi-tenant isolation.
- No general DAG scheduler; steps run in persisted sequence.
- No claim of production throughput or availability.

The point of this repository is the durable boundary beneath those systems: a tool effect, a persisted result, and a recovery story that can be tested.
