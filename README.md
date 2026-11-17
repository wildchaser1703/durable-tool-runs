# Durable Tool Runs

An execution service for the awkward gap between **a tool performed an action**
and **the worker recorded success**. PostgreSQL stores sequential steps, renewable
leases and an event trace. A separate HTTP provider commits idempotent effects in
its own SQLite store. Recovery crosses a real process and database boundary.

## Run

```sh
docker compose up -d --build --wait
curl http://127.0.0.1:8000/health
```

Open http://127.0.0.1:8000/docs for the interactive API. Documentation assets are
served locally. API, database, provider and metrics ports bind to localhost.
No paid service, API key or GPU is required.
Named volumes retain runner history and provider effects across ordinary Compose
teardown and startup. Follow the upgrade guide before switching an older checkout
that used an anonymous PostgreSQL volume.

```sh
curl http://127.0.0.1:8000/runs -H 'content-type: application/json' \
  -d '{"steps":[{"tool":"record","payload":{"ticket":"A-17","stage":"capture"}},{"tool":"record","payload":{"ticket":"A-17","stage":"confirm"}}]}'
```

Read the returned run with `GET /runs/{id}`; resume events with
`GET /runs/{id}/events?after={event_id}`; cancel with `POST /runs/{id}/cancel`.
The API serves metrics on port 8000 and the Compose worker on port 9100.

## What is guaranteed within this boundary

- A new UUID identifies each claim, even when a worker name is reused. Expired or
  replaced claims cannot renew, finish, retry or fail another attempt's work.
- All state transitions lock the run before the step. Claims use SKIP LOCKED.
  Cancellation and completion serialize, and events commit in order per run.
- Retries preserve the tool's key, use persisted exponential backoff with jitter,
  and stop after the attempt budget.
- The provider returns the original result for a matching key and payload.
  Conflicting payloads get HTTP 409. Arbitrary providers need their own equivalent
  contract; this is not universal exactly-once execution.
- Cancellation stops queued work. An in-flight effect is not undone. If its
  outcome is unknown, the run ends in `needs_review`, not a false no-effect result.

## Validation and development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
make test
python scripts/smoke.py
```

`make test` starts an isolated PostgreSQL database on port 5433. The tests launch a
separate HTTP provider and kill a real worker after a remote effect commits.
Do not point TEST_DATABASE_URL at a database you want to retain: tests reset it.

For host-process development, start `docker compose up -d --wait postgres provider`,
run `make migrate`, then `make api` and `make worker` in separate terminals.
Set POSTGRES_PORT when 5432 is occupied; use the matching DATABASE_URL for host
processes. Containers always connect to the internal database port.

## Evaluated local AI example

```sh
pip install -e '.[ai]'
python -m examples.triage --api-url http://127.0.0.1:8000 --output evidence/triage.json
```

The pinned, public SmolLM2-135M-Instruct model scores three ticket categories on CPU.
Application code constructs an allowlisted record proposal, validates its schema
and source ticket ID, submits it, and verifies the persisted effect. Model output
cannot select arbitrary tools or network destinations. This is constrained
classification feeding a durable workflow, not an autonomous agent.

[evidence/triage.json](evidence/triage.json) records every prediction, label score,
and completed run. The initial prompt's 3/9 result is retained in
[evidence/triage-baseline.json](evidence/triage-baseline.json). Nine synthetic
examples show integration behavior, not production model quality.
The few-shot prompt scored 4/9 with 9/9 workflows completed. Both prompts used
the same development cases; the classifier is not ready for unattended routing.
Use `--prompt zero-shot` to reproduce the baseline.

## Upgrading the earlier prototype

Stop old API and worker processes before upgrading; old workers lack claim fencing.
If upgrading the original Compose layout, first follow the anonymous-volume
preservation procedure in [operations](docs/operations.md).

```sh
docker compose stop api worker
docker compose build
docker compose up -d --wait
```

The migration validates all baseline columns, types, nullability, keys, relations,
and required indexes before adopting legacy tables or applying revision 0002.
A mismatch fails with an explanation and transaction rollback. It never drops
history to repair drift. See [operations](docs/operations.md).

## Scope

One PostgreSQL service, sequential record steps, one idempotent provider, and
bounded recovery. No general DAG planner, authentication, tenant isolation,
automatic compensation, or production availability/throughput claim.

[Architecture](docs/architecture.md) · [Case study and evidence](docs/case-study.md)
· [Verification](docs/verification.md) · [Third-party components](docs/third-party.md)
