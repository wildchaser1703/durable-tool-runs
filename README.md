# Durable Tool Runs

A small execution service for one question that agent demos usually avoid: **what happens if a worker dies after a tool performs an effect, but before the worker records success?**

The service persists runs, steps, leases and event history in PostgreSQL. Workers reclaim expired leases. Its bundled record provider runs as a separate HTTP service with its own durable store; it accepts a stable idempotency key, so replaying an ambiguous step returns the original effect instead of creating another one.

Read [the architecture decisions](docs/architecture.md) for the failure boundaries, recovery model, and explicit non-goals.

## Guarantees and boundary

- Runs, steps and events survive an API or worker restart.
- Workers claim steps with PostgreSQL row locks and `SKIP LOCKED`.
- The separate provider provides effectively-once effects when a stable idempotency key is reused.
- Expired leases are reclaimed up to a bounded attempt budget; a run becomes terminally failed instead of retrying forever.
- A heartbeat renews a live worker's lease while a tool call is in flight.
- Provider transport or 5xx outcomes are treated as ambiguous and retried with the same key; 4xx rejections are terminal.
- The API exposes accepted-run metrics at `/metrics`; each worker exposes
  claim, provider-call, and step-duration metrics on its own `/metrics` target.
- Arbitrary external APIs cannot be made exactly-once by this service. A tool without idempotency support must be treated as ambiguous and require review.

## Run locally

```sh
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

This starts PostgreSQL, the migration job, idempotent provider, API and one worker. The API is on `:8000`; the compose worker metrics target is on `:9100`. The runner uses Alembic migrations; it never creates tables at API or worker boot. For test-driven development, start PostgreSQL and the provider, then run the processes from your virtual environment:

```sh
docker compose up -d --wait postgres provider
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
DATABASE_URL=postgresql+psycopg://durable:durable@localhost:5432/durable_runs make migrate
make test
uvicorn app.main:app --reload
```

In a second shell, start a worker:

```sh
. .venv/bin/activate
python -m app.worker
```

Create a two-step run:

```sh
curl -X POST http://127.0.0.1:8000/runs \
  -H 'content-type: application/json' \
  -d '{"steps":[{"tool":"record","payload":{"ticket":"A-17","stage":"capture"}},{"tool":"record","payload":{"ticket":"A-17","stage":"confirm"}}]}'
```

## Recovery experiment

`test_process_kill_after_remote_effect_reclaims_without_a_duplicate` proves this sequence with a real subprocess:

1. Worker A claims a step and the separate HTTP provider commits its effect.
2. The test observes Worker A receive the provider response, then sends it `SIGKILL` before it can persist the result.
3. Its lease expires.
4. Worker B reclaims the step and calls the tool using the same idempotency key.
5. The tool replays the original result; no second effect is created.

The test verifies the provider's persisted effect, two attempts, a reclaimed lease, a provider replay, and a replayable event trail. This is deliberately stronger than raising a test exception within the worker process.

## Architecture

```text
HTTP API → PostgreSQL (runs, steps, leases, events)
                     ↓
                  workers ──HTTP──→ idempotent provider → its own durable store
```

This is deliberately not a general agent framework. There is no model API, task planner, vector store or external orchestration dependency. Those systems can submit durable steps later; this project establishes the execution boundary first. `GET /runs/{id}/events?after={event_id}` lets a client resume its ordered event stream after reconnecting.
