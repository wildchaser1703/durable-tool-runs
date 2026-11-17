from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.database import Database
from app.main import create_app
from app.migrations import upgrade_database
from app.service import SimulatedWorkerCrash, ToolExecutionError, Worker


def api_request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(request())


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def provider_url(tmp_path_factory):
    port = unused_port()
    provider_database = tmp_path_factory.mktemp("provider") / "effects.db"
    environment = os.environ | {"PROVIDER_DATABASE_PATH": str(provider_database)}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.provider:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/health", timeout=0.2).is_success:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise RuntimeError("tool provider did not become healthy")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.fixture()
def runner(provider_url):
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://durable:durable@localhost:5433/durable_runs_test",
    )
    database = Database(url)
    database.reset_schema()
    upgrade_database(url)
    app = create_app(url)
    yield app, database, provider_url


def create_run(app, steps: list[dict]) -> str:
    response = api_request(app, "POST", "/runs", json={"steps": steps})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def snapshot(app, run_id: str) -> dict:
    response = api_request(app, "GET", f"/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_run_completes_with_a_replayable_trace(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"tool": "record", "payload": {"ticket": "A-17"}}])
    outcome = Worker(database, "worker-a", provider_url=provider_url).run_once()
    assert outcome["replayed"] is False
    completed = snapshot(app, run_id)
    assert completed["status"] == "succeeded"
    assert completed["steps"][0]["result"] == {
        "recorded": {"ticket": "A-17"},
        "effect_key": f"{run_id}:step:1",
    }
    assert [event["kind"] for event in completed["events"]] == [
        "run_created",
        "step_claimed",
        "tool_effect_created",
        "run_succeeded",
    ]


def test_process_kill_after_remote_effect_reclaims_without_a_duplicate(
    runner, tmp_path
):
    """The worker is a real process, not an exception injected into a method."""
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-18"}}])
    marker = tmp_path / "remote-effect-returned"
    environment = os.environ | {
        "DATABASE_URL": database.engine.url.render_as_string(hide_password=False),
        "TOOL_PROVIDER_URL": provider_url,
        "WORKER_ID": "killed-worker",
        "LEASE_SECONDS": "1",
        "METRICS_PORT": "0",
        "AFTER_TOOL_PAUSE_SECONDS": "30",
        "AFTER_TOOL_MARKER": str(marker),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker", "--once"], env=environment
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "worker never received the provider response"
        effect = httpx.get(f"{provider_url}/effects/{run_id}:step:1", timeout=2)
        assert effect.status_code == 200
    finally:
        process.kill()
        process.wait(timeout=5)
    time.sleep(1.2)
    recovered = Worker(
        database, "recovery-worker", lease_seconds=1, provider_url=provider_url
    ).run_once()
    assert recovered["replayed"] is True
    completed = snapshot(app, run_id)
    assert completed["status"] == "succeeded"
    assert completed["steps"][0]["attempts"] == 2
    assert [event["kind"] for event in completed["events"]] == [
        "run_created",
        "step_claimed",
        "step_reclaimed",
        "tool_replayed",
        "run_succeeded",
    ]


def test_queued_run_cancels_without_an_effect(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-19"}}])
    assert (
        api_request(app, "POST", f"/runs/{run_id}/cancel").json()["status"]
        == "cancelled"
    )
    assert Worker(database, "worker-a", provider_url=provider_url).run_once() is None
    assert httpx.get(f"{provider_url}/effects/{run_id}:step:1").status_code == 404


def test_unknown_tools_are_rejected_at_the_api_boundary(runner):
    app, _, _ = runner
    response = api_request(
        app,
        "POST",
        "/runs",
        json={"steps": [{"tool": "shell", "payload": {"command": "anything"}}]},
    )
    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "Only one or more idempotent record steps are available in v1"
    )


def test_steps_run_in_order_and_events_can_resume_from_a_cursor(runner):
    app, database, provider_url = runner
    run_id = create_run(
        app,
        [
            {"payload": {"ticket": "A-20", "position": 1}},
            {"payload": {"ticket": "A-20", "position": 2}},
        ],
    )
    worker = Worker(database, "worker-a", provider_url=provider_url)
    first = worker.run_once()
    midway = snapshot(app, run_id)
    assert midway["status"] == "queued"
    assert [step["status"] for step in midway["steps"]] == ["succeeded", "queued"]
    second = worker.run_once()
    completed = snapshot(app, run_id)
    assert [first["step_id"], second["step_id"]] == [
        step["id"] for step in completed["steps"]
    ]
    resumed = api_request(
        app, "GET", f"/runs/{run_id}/events?after={midway['events'][-1]['id']}"
    ).json()["events"]
    assert [event["kind"] for event in resumed] == [
        "step_claimed",
        "tool_effect_created",
        "run_succeeded",
    ]


def test_two_workers_cannot_claim_one_step_twice(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-21"}}])
    workers = [
        Worker(database, "worker-a", provider_url=provider_url),
        Worker(database, "worker-b", provider_url=provider_url),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda worker: worker.run_once(), workers))
    assert len([outcome for outcome in outcomes if outcome]) == 1
    assert snapshot(app, run_id)["steps"][0]["attempts"] == 1


def test_terminal_provider_rejection_cancels_later_steps(runner):
    app, database, provider_url = runner
    run_id = create_run(
        app,
        [
            {"payload": {"ticket": "A-22", "position": 1}},
            {"payload": {"ticket": "A-22", "position": 2}},
        ],
    )
    worker = Worker(database, "worker-a", provider_url=provider_url)
    worker.tool.execute = lambda *_: (_ for _ in ()).throw(
        ToolExecutionError("provider rejected request")
    )
    assert worker.run_once()["failed"] is True
    completed = snapshot(app, run_id)
    assert completed["status"] == "failed"
    assert [step["status"] for step in completed["steps"]] == ["failed", "cancelled"]


def test_ambiguous_transport_failure_is_retried_with_the_same_key(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-23"}}])
    worker = Worker(
        database, "worker-a", provider_url=provider_url, retry_base_seconds=0
    )
    worker.tool.base_url = "http://127.0.0.1:1"
    assert worker.run_once()["retry_scheduled"] is True
    assert snapshot(app, run_id)["steps"][0]["status"] == "queued"
    assert (
        Worker(database, "worker-b", provider_url=provider_url).run_once()["replayed"]
        is False
    )
    assert snapshot(app, run_id)["status"] == "succeeded"


def test_expired_lease_stops_after_its_attempt_budget(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-24"}}])
    first = Worker(
        database, "worker-a", lease_seconds=0, max_attempts=1, provider_url=provider_url
    )
    with pytest.raises(SimulatedWorkerCrash):
        first.run_once(crash_after_tool=True)
    assert (
        Worker(
            database, "worker-b", max_attempts=1, provider_url=provider_url
        ).run_once()
        is None
    )
    completed = snapshot(app, run_id)
    assert completed["status"] == "needs_review"
    assert "remote outcome unknown" in completed["steps"][0]["error"]


def expire(database, claim):
    from datetime import timedelta

    from sqlalchemy import func, update

    from app.models import Step

    with database.sessions() as session:
        session.execute(
            update(Step)
            .where(Step.id == claim.step_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
        session.commit()


def test_old_claim_cannot_finish_fail_or_renew_even_with_same_worker_name(runner):
    app, database, url = runner
    run_id = create_run(app, [{"payload": {"case": "fence"}}])
    old = Worker(database, "same-name", provider_url=url)
    first = old._claim()
    expire(database, first)
    replacement = Worker(database, "same-name", provider_url=url)
    second = replacement._claim()
    assert first.token != second.token
    assert old._renew_lease(first) is False
    assert old._finish(first, {"wrong": True}, False)["lease_lost"]
    assert old._error(first, "late failure", True)["lease_lost"]
    result, replayed = replacement.tool.execute(second.key, second.payload)
    replacement._finish(second, result, replayed)
    assert old._finish(first, {"wrong": True}, False)["lease_lost"]
    assert snapshot(app, run_id)["steps"][0]["result"] == result


def test_expired_claim_cannot_resurrect_before_replacement(runner):
    app, database, url = runner
    create_run(app, [{"payload": {}}])
    worker = Worker(database, "old", provider_url=url)
    claim = worker._claim()
    expire(database, claim)
    assert worker._renew_lease(claim) is False
    assert worker._finish(claim, {}, False)["lease_lost"]


@pytest.mark.parametrize(
    "retryable,expected", [(True, "needs_review"), (False, "cancelled")]
)
def test_cancel_running_then_failure_never_requeues(runner, retryable, expected):
    app, database, url = runner
    run_id = create_run(app, [{"payload": {}}, {"payload": {}}])
    worker = Worker(database, "one", provider_url=url)
    claim = worker._claim()
    assert (
        api_request(app, "POST", f"/runs/{run_id}/cancel").json()["status"]
        == "cancelling"
    )
    worker._error(claim, "provider failure", retryable)
    current = snapshot(app, run_id)
    assert current["status"] == expected
    assert current["steps"][1]["status"] == "cancelled"
    assert worker.run_once() is None


def test_cancel_after_effect_records_effect_and_stops_downstream(runner):
    app, database, url = runner
    run_id = create_run(app, [{"payload": {}}, {"payload": {}}])
    worker = Worker(database, "one", provider_url=url)
    claim = worker._claim()
    result, replayed = worker.tool.execute(claim.key, claim.payload)
    api_request(app, "POST", f"/runs/{run_id}/cancel")
    worker._finish(claim, result, replayed)
    current = snapshot(app, run_id)
    assert current["status"] == "cancelled"
    assert [s["status"] for s in current["steps"]] == ["succeeded", "cancelled"]
    assert current["events"][-1]["detail"]["after_effect"] is True


def test_cancel_during_backoff_marks_unknown_effect_for_review(runner):
    app, database, url = runner
    run_id = create_run(app, [{"payload": {}}])
    worker = Worker(database, "one", provider_url=url)
    claim = worker._claim()
    worker._error(claim, "lost reply", True)
    assert (
        api_request(app, "POST", f"/runs/{run_id}/cancel").json()["status"]
        == "needs_review"
    )
    assert worker.run_once() is None


def test_cancel_expired_claim_does_not_claim_no_effect(runner):
    app, database, url = runner
    run_id = create_run(app, [{"payload": {}}])
    worker = Worker(database, "one", provider_url=url)
    claim = worker._claim()
    worker.tool.execute(claim.key, claim.payload)
    api_request(app, "POST", f"/runs/{run_id}/cancel")
    expire(database, claim)
    assert worker.run_once() is None
    assert snapshot(app, run_id)["status"] == "needs_review"


def test_backoff_is_persisted_and_capped(runner):
    from sqlalchemy import func, select

    from app.models import Step

    app, database, url = runner
    run_id = create_run(app, [{"payload": {}}])
    worker = Worker(
        database, "one", provider_url=url, retry_base_seconds=10, retry_cap_seconds=10
    )
    claim = worker._claim()
    worker._error(claim, "unavailable", True)
    assert worker._claim() is None
    assert Worker(database, "restarted", provider_url=url)._claim() is None
    with database.sessions() as session:
        step = session.get(Step, claim.step_id)
        delay = (
            step.available_at - session.scalar(select(func.clock_timestamp()))
        ).total_seconds()
        assert 4 < delay <= 10
    assert 5 <= snapshot(app, run_id)["events"][-1]["detail"]["delay_seconds"] <= 10


def test_heartbeat_keeps_slow_tool_owned(runner):
    import threading

    app, database, url = runner
    run_id = create_run(app, [{"payload": {}}])
    worker = Worker(database, "slow", lease_seconds=0.6, provider_url=url)
    began, release = threading.Event(), threading.Event()
    execute = worker.tool.execute

    def slow(*args):
        began.set()
        assert release.wait(5)
        return execute(*args)

    worker.tool.execute = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.run_once)
        try:
            assert began.wait(3)
            time.sleep(1)
            assert Worker(database, "other", provider_url=url)._claim() is None
        finally:
            release.set()
        assert running.result(timeout=5)["replayed"] is False
    assert snapshot(app, run_id)["status"] == "succeeded"


def test_cancel_and_finish_are_serialized_without_duplicate_terminal_events(runner):
    import threading

    app, database, url = runner
    run_id = create_run(app, [{"payload": {}}])
    worker = Worker(database, "one", provider_url=url)
    claim = worker._claim()
    result, replayed = worker.tool.execute(claim.key, claim.payload)
    barrier = threading.Barrier(2)

    def cancel():
        barrier.wait()
        return app.state.service.cancel(run_id)

    def finish():
        barrier.wait()
        return worker._finish(claim, result, replayed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(cancel), pool.submit(finish)]
        for job in jobs:
            job.result(timeout=5)
    current = snapshot(app, run_id)
    assert current["status"] in {"succeeded", "cancelled"}
    assert current["steps"][0]["result"] == result
    assert (
        sum(e["kind"] in {"run_succeeded", "run_cancelled"} for e in current["events"])
        == 1
    )


def test_provider_rejects_conflicting_payload_without_replacing_effect(provider_url):
    from uuid import uuid4

    key = str(uuid4())
    with httpx.Client(base_url=provider_url, trust_env=False) as client:
        first = client.post(
            "/effects", json={"idempotency_key": key, "payload": {"a": 1, "b": 2}}
        )
        same = client.post(
            "/effects", json={"idempotency_key": key, "payload": {"b": 2, "a": 1}}
        )
        conflict = client.post(
            "/effects", json={"idempotency_key": key, "payload": {"a": 2}}
        )
        assert first.status_code == same.status_code == 200
        assert same.json()["replayed"]
        assert conflict.status_code == 409
        assert client.get(f"/effects/{key}").json()["result"] == first.json()["result"]


@pytest.mark.parametrize(
    "damage", [None, "missing_table", "wrong_type", "missing_unique"]
)
def test_migrations_preserve_valid_legacy_data_and_reject_schema_drift(runner, damage):
    import importlib.util
    from pathlib import Path
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    _, database, _ = runner

    def revision(filename):
        spec = importlib.util.spec_from_file_location(
            "migration", Path("alembic/versions") / filename
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    first, second = revision("0001_runner_schema.py"), revision("0002_claim_fencing.py")
    schema = "test_migration_" + uuid4().hex
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        with Operations.context(MigrationContext.configure(connection)):
            first.upgrade()
            connection.exec_driver_sql(
                "INSERT INTO runs (id,status,cancellation_requested) VALUES ('preserved','queued',false)"
            )
            if damage == "missing_table":
                connection.exec_driver_sql("DROP TABLE run_events")
            elif damage == "wrong_type":
                connection.exec_driver_sql(
                    "ALTER TABLE runs ALTER COLUMN status TYPE TEXT"
                )
            elif damage == "missing_unique":
                connection.exec_driver_sql(
                    "ALTER TABLE steps DROP CONSTRAINT uq_step_position_per_run"
                )
            if damage:
                with pytest.raises(RuntimeError, match="Baseline schema mismatch"):
                    first.upgrade()
                with pytest.raises(RuntimeError, match="Baseline schema mismatch"):
                    second.upgrade()
            else:
                first.upgrade()
                second.upgrade()
                assert (
                    connection.exec_driver_sql("SELECT id FROM runs").scalar()
                    == "preserved"
                )
                second.downgrade()
                second.upgrade()
        connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
