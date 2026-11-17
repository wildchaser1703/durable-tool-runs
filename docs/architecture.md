# Architecture decisions

## PostgreSQL is both the state store and the work queue

`runs`, `steps`, `run_events`, and `tool_effects` are persisted in PostgreSQL. Workers claim a step with `SELECT … FOR UPDATE SKIP LOCKED`, commit a lease, and execute outside that lease transaction.

This keeps the recovery record and the work-selection record together. A separate queue would improve throughput at a larger scale, but would create another delivery boundary to reason about. The v1 tradeoff is deliberate: one inspectable database is easier to recover and test.

## Leases recover worker loss

A `running` step has an owner and an expiry. A new worker may reclaim it after expiry. A worker never assumes that an in-memory task survived a restart.

Expiry is not a success signal. Each step has a bounded attempt budget. After that budget is exhausted, the run becomes `failed`; downstream queued steps are cancelled and the event trail records the reason.

Long-running production tools would require a heartbeat that renews the lease. The bundled record tool is intentionally short-lived, so this version does not pretend that a fixed lease can safely cover arbitrary work.

## Idempotency is the external-effect boundary

The record tool commits its effect in a separate transaction. That exposes the important crash window:

1. A worker claims a step.
2. The tool commits an effect.
3. The worker dies before it stores the tool result.
4. Another worker reclaims the expired lease.

The replacement worker calls the tool with the same stable key, `run_id:step:position`. The tool returns the earlier result instead of creating a second effect.

This provides effectively-once effects only for tools with an idempotency contract. No database transaction can make an arbitrary remote API exactly-once. A real non-idempotent provider needs a compensating action or a `needs_review` state; it must not be silently retried.

## Events are replayable, not merely logs

Each event receives a monotonic database sequence. A reconnecting client uses `GET /runs/{id}/events?after={event_id}` to fetch only events it has not observed. UUIDs and timestamps do not define a reliable causal order when events are committed in the same transaction.

## Explicit non-goals for v1

- No model provider, prompt planner, vector database, or agent framework.
- No authentication or multi-tenant isolation.
- No general DAG scheduler; steps run in persisted sequence.
- No claim of production throughput or availability.

The point of this repository is the durable boundary beneath those systems: a tool effect, a persisted result, and a recovery story that can be tested.
