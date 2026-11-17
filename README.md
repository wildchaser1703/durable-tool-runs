# Durable Tool Runs

A small execution service for one question that agent demos usually avoid: **what happens if a worker dies after a tool performs an effect, but before the worker records success?**

The service persists runs, steps, leases and event history in PostgreSQL. Workers reclaim expired leases. The included `record` tool accepts a stable idempotency key, so replaying an ambiguous step returns the original effect instead of creating another one.

Read [the architecture decisions](docs/architecture.md) for the failure boundaries, recovery model, and explicit non-goals.

## Guarantees and boundary

- Runs, steps and events survive an API or worker restart.
- Workers claim steps with PostgreSQL row locks and `SKIP LOCKED`.
- The included tool provides effectively-once effects when a stable idempotency key is reused.
- Expired leases are reclaimed up to a bounded attempt budget; a run becomes terminally failed instead of retrying forever.
- Arbitrary external APIs cannot be made exactly-once by this service. A tool without idempotency support must be treated as ambiguous and require review.

## Run locally

```sh
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

This starts PostgreSQL, the API and one worker. For test-driven development, start only PostgreSQL and run the processes from your virtual environment:

```sh
docker compose up -d --wait postgres
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
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

`test_crash_after_effect_retries_without_a_duplicate_effect` models this sequence:

1. Worker A claims a step and the tool commits its effect.
2. Worker A dies before persisting the result.
3. Its lease expires.
4. Worker B reclaims the step and calls the tool using the same idempotency key.
5. The tool replays the original result; no second effect is created.

The test verifies one effect, two attempts, a reclaimed lease, and a replayable event trail.

## Architecture

```text
HTTP API → PostgreSQL (runs, steps, leases, events)
                     ↓
                  workers
                     ↓
       idempotent record tool → committed effect
```

This is deliberately not a general agent framework. There is no model API, task planner, vector store or external orchestration dependency. Those systems can submit durable steps later; this project establishes the execution boundary first. `GET /runs/{id}/events?after={event_id}` lets a client resume its ordered event stream after reconnecting.
