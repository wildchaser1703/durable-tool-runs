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
unchanged unless POST /runs/{id}/reconcile confirms the original persisted effect.
That endpoint requires the configured API token. It compares the exact effect key
and payload, records reconciliation, and resumes unattempted downstream steps
only if the run was not cancelled. A cancelled run remains cancelled. Missing or
conflicting provider effects return 409 without mutating history or making a new
tool call. Repeated reconciliation is harmless. Never create a replacement key
merely to bypass an uncertain outcome.

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

Application images now run as UID/GID 10001 with read-only root filesystems and
no Linux capabilities. For an existing provider volume originally owned by root,
stop the provider and change that volume's ownership to 10001:10001 before first
starting the new image. New named volumes inherit the image's ownership. Retain
the provider backup while doing this; do not replace its data directory.

Workers handle SIGTERM/SIGINT by finishing their current attempt and refusing new
claims. Compose allows 45 seconds for this. A forced kill still uses lease recovery.
Database connections, statements, locks and pool acquisition have finite timeouts.

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

Bind ports to localhost. Set REQUIRE_AUTH=1 and API_TOKEN for operator access
through an HTTPS proxy. This is one trust domain, not tenant isolation; the
provider and worker metrics must remain private. The provider SQLite volume
must be retained alongside the runner database. docker compose down without -v
retains both named stores; do not use destructive volume cleanup on
data you need.
