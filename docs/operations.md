# Operations

## Start and inspect

Run docker compose up -d --build --wait. Inspect docker compose ps and
docker compose logs worker provider migrate if readiness fails. /health checks
database access; /metrics on port 8000 belongs to the API, and port 9100 to the worker.
Read /runs/{id} to distinguish queued, running, cancelling, succeeded, cancelled,
failed and needs_review. An unavailable provider leaves a bounded retry trail.

## Needs review

Use the step position to obtain the stable key: run_id:step:position.
Query the local provider:

```sh
curl 'http://127.0.0.1:8081/effects/RUN_ID:step:1'
```

A matching persisted record establishes the effect occurred. A current 404 alone
does not prove a disconnected remote attempt can never commit later; stop/reconcile
that attempt before deciding to re-execute. This release leaves needs_review
terminal and requires operator reconciliation. It does not automatically issue a
new key or overwrite audit history.

## Upgrade

The original Compose file used an anonymous PostgreSQL volume. Before first
starting this version over that checkout, stop API and worker, save a pg_dump
backup, and inspect the existing PostgreSQL container's mounts. Either restore
the backup into the new named postgres_data volume or copy the original data
directory while PostgreSQL is fully stopped, using the same PostgreSQL major
version and preserving ownership. Do not copy a live data directory. Keep the
old volume and backup until you have verified row counts and representative runs.
Changing the Compose mount without migrating data would start an empty database.

Back up PostgreSQL and the provider's SQLite store using their standard backup
tools before changing a valuable deployment. Stop API and worker first, build
new images, then start Compose. Its migration service must succeed before the
new workers start. Schema mismatches require investigation; do not delete tables
or stamp a migration merely to bypass validation. Legacy tool_effects rows are
retained as historical data and are not silently moved into the new provider.

A rollback to old application code is unsafe while new workers are running.
Schema downgrade exists for development tests; the normal recovery path is a
forward fix with the new worker processes stopped.

## Reproduce evidence

- make test: isolated PostgreSQL plus real provider/process-kill and race tests.
- python scripts/smoke.py: live two-step API → worker → provider → persisted result.
- python -m examples.triage --api-url http://127.0.0.1:8000: free CPU model workflow.

Worker options: LEASE_SECONDS (30), MAX_ATTEMPTS (3), RETRY_BASE_SECONDS (1),
RETRY_CAP_SECONDS (30), TOOL_PROVIDER_URL and a distinct WORKER_ID if desired.
Fault-injection marker/pause options are for controlled experiments only.

## Local-only boundaries

Bind ports to localhost. There is no authentication or tenant isolation, so this
configuration is not an Internet-facing deployment. The provider SQLite volume
must be retained alongside the runner database. docker compose down without -v
retains both named stores; do not use destructive volume cleanup on
data you need.
