# v0.3: bounded operation and measured model quality

The service now deduplicates client submissions as well as provider effects.
It adds optional single-operator authentication, bounded requests and database
waits, graceful worker shutdown, and explicit reconciliation of confirmed effects.
It remains one PostgreSQL queue and a separate idempotent provider. No broker,
agent framework, Kubernetes layer, DAG planner or tenant abstraction was added.

## Operational behavior

- `Idempotency-Key` is optional for compatibility and recommended for every
  retriable submission. A unique PostgreSQL constraint arbitrates concurrent
  requests. The normalized request hash detects conflicting reuse. Keys are not
  silently expired, because reusing an expired key could duplicate an action.
- `REQUIRE_AUTH=1` fails startup without an adequate `API_TOKEN`. Bearer tokens
  protect mutations, run reads and API metrics. This is one operator trust domain;
  tokens do not create separate tenants. HTTPS and ingress rate limits belong to
  the deployment's reverse proxy. Provider/database/worker metrics stay private.
- Request bodies are capped before JSON parsing, including chunked requests;
  body receipt has a ten-second deadline. The API accepts at most twenty steps,
  rejects unexpected fields and non-finite payload numbers, and bounds event
  pages to 1,000 records. The example Uvicorn process caps HTTP concurrency at 64.
- PostgreSQL connection/pool/lock waits are limited to five seconds and statements
  to fifteen seconds. API database failures return 503 with a retry hint, without
  exposing connection or SQL details. Retry the same submission key.
- SIGTERM stops new claims while the current attempt finishes. Compose grants
  45 seconds; forced termination still uses the existing lease recovery protocol.
- Reconciliation reads the provider outside a database transaction, then locks
  run and step before changing state. It verifies the exact stored key/payload,
  never accepts a caller-supplied result, and never retries an uncertain action.
  Missing/conflicting effects remain `needs_review`. Confirmed effects can resume
  downstream work; cancellation remains respected.
- Application containers run as UID 10001, with read-only root filesystems,
  dropped capabilities and no privilege escalation. The provider's named data
  volume is the writable durable store. Existing volumes need an ownership
  migration; see [operations](operations.md).

## Verification

All 45 tests passed against PostgreSQL and a separate HTTP provider. New coverage
includes eight concurrent submissions using one key, conflicting reuse, auth
boundaries, chunked and slow bodies, event pagination, transient provider statuses,
graceful process shutdown, and reconciliation of confirmed/missing/conflicting
effects. Earlier process-kill, stale-claim, cancellation and migration tests remain.

The Docker image built and started successfully with non-root processes. A separate
authenticated container returned 401 for uncredentialed reads/mutations and 200/201
for authorized ones. Its test token was ephemeral and was not retained.
The final browser API check completed two ordered steps at desktop and mobile
viewports, with no page errors or external asset requests.

The bounded concurrency trial used eight clients, fifty two-step runs and one
worker. One hundred submissions (each key submitted twice) produced fifty run IDs
and one hundred intended effects, with one attempt per step. Observed completion
p50/p95 were 0.524/0.802 seconds in that single shared-host trial. This is functional
evidence, not a throughput SLA or sustained-load result.

A PostgreSQL dump restored into an isolated scratch database with matching full-row
hashes for 99 runs, 160 steps and 576 events. The provider's consistent SQLite
backup passed integrity_check and held 153 effects. Backups are retained outside
the repository. The restored scratch database was removed; live data was untouched.

Raw observations: [concurrency](../evidence/concurrency.json),
[authentication/container](../evidence/auth-container.json),
[backup/restore](../evidence/backup-restore.json).
CI repeats lint, automated tests, container startup, the smoke workflow and a
smaller concurrent-submission check. Model weights are not downloaded on every CI run.

## Model selection and held-out evaluation

All models are public, pinned and run on CPU without a key or hosted API. The
classification task is deliberately bounded: three labels feed a schema-validated
record action. The application, not the model, selects the tool and source ID.

| Development configuration | Correct / nine |
|---|---:|
| SmolLM2 135M, zero-shot | 3 |
| SmolLM2 135M, few-shot | 4 |
| Qwen2.5 0.5B, label scoring | 6 |
| Qwen2.5 0.5B, letter-choice scoring | 5 |
| Qwen2.5 1.5B, letter-choice scoring | 9 |

The final model/prompt was selected using those nine development examples. The
separate thirty-case set was written before that comparison, then evaluated once
after selection. It includes negation, varied wording and three instruction-override
attempts. No prompt or model was tuned against its results.

**Held-out result: 24/30 correct, macro-F1 0.803; 30/30 workflows completed.**
Billing recall was 10/10, access 6/10, and other 8/10. The six errors all selected
billing incorrectly. This small synthetic test is not representative production
traffic and does not establish prompt-injection resistance. Scores are raw model
log probabilities, not calibrated confidence. Do not treat this as unattended
ticket routing; real operational decisions still need human review.

Two of the three instruction-override cases were misclassified. They still could
not change the tool or source ticket ID: those are fixed and validated by the
application. Constraining an action's shape does not make its meaning correct.

The current default is Qwen2.5-1.5B-Instruct at revision
989aa7980e4cf806f80c7fef2b1adb7bc71aa306. Letter answers are single tokens, allowing
one forward pass per ticket rather than three label-completion passes. The model
and tokenizer load without remote code, using safetensors. The recorded environment
was Python 3.12.14, torch 2.5.1+cpu and transformers 4.57.6, with two torch threads.

```sh
pip install -e '.[ai]'
python -m examples.triage --model qwen-1.5b --prompt choices --dataset holdout \
  --api-url http://127.0.0.1:8000 --output evidence/triage-current.json
```

[Raw held-out predictions](../evidence/triage-current.json) include the dataset hash,
model revision, scores, timings, confusion matrix and completed run IDs. Earlier
reports remain in evidence; improvements are not measured by deleting failures.

## Remaining deployment-specific work

This is a hardened single-operator reference service, not a certified production
platform. A real deployment still needs its own TLS/identity policy, backup schedule
and recovery objectives, disk/queue retention policy, rate limits, alert thresholds,
and a workload-specific soak test. Multi-tenant isolation, arbitrary external tools,
automatic compensation and model-quality guarantees are outside this release.
