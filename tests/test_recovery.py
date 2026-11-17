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
from app.service import SimulatedWorkerCrash, Worker


def api_request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
        [sys.executable, "-m", "uvicorn", "app.provider:app", "--host", "127.0.0.1", "--port", str(port)],
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
    url = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://durable:durable@localhost:5433/durable_runs_test")
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
    assert completed["steps"][0]["result"] == {"recorded": {"ticket": "A-17"}, "effect_key": f"{run_id}:step:1"}
    assert [event["kind"] for event in completed["events"]] == ["run_created", "step_claimed", "tool_effect_created", "run_succeeded"]


def test_process_kill_after_remote_effect_reclaims_without_a_duplicate(runner, tmp_path):
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
    process = subprocess.Popen([sys.executable, "-m", "app.worker", "--once"], env=environment)
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
    recovered = Worker(database, "recovery-worker", lease_seconds=1, provider_url=provider_url).run_once()
    assert recovered["replayed"] is True
    completed = snapshot(app, run_id)
    assert completed["status"] == "succeeded"
    assert completed["steps"][0]["attempts"] == 2
    assert [event["kind"] for event in completed["events"]] == ["run_created", "step_claimed", "step_reclaimed", "tool_replayed", "run_succeeded"]


def test_queued_run_cancels_without_an_effect(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-19"}}])
    assert api_request(app, "POST", f"/runs/{run_id}/cancel").json()["status"] == "cancelled"
    assert Worker(database, "worker-a", provider_url=provider_url).run_once() is None
    assert httpx.get(f"{provider_url}/effects/{run_id}:step:1").status_code == 404


def test_unknown_tools_are_rejected_at_the_api_boundary(runner):
    app, _, _ = runner
    response = api_request(app, "POST", "/runs", json={"steps": [{"tool": "shell", "payload": {"command": "anything"}}]})
    assert response.status_code == 400
    assert response.json()["detail"] == "Only one or more idempotent record steps are available in v1"


def test_steps_run_in_order_and_events_can_resume_from_a_cursor(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-20", "position": 1}}, {"payload": {"ticket": "A-20", "position": 2}}])
    worker = Worker(database, "worker-a", provider_url=provider_url)
    first = worker.run_once()
    midway = snapshot(app, run_id)
    assert midway["status"] == "queued"
    assert [step["status"] for step in midway["steps"]] == ["succeeded", "queued"]
    second = worker.run_once()
    completed = snapshot(app, run_id)
    assert [first["step_id"], second["step_id"]] == [step["id"] for step in completed["steps"]]
    resumed = api_request(app, "GET", f"/runs/{run_id}/events?after={midway['events'][-1]['id']}").json()["events"]
    assert [event["kind"] for event in resumed] == ["step_claimed", "tool_effect_created", "run_succeeded"]


def test_two_workers_cannot_claim_one_step_twice(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-21"}}])
    workers = [Worker(database, "worker-a", provider_url=provider_url), Worker(database, "worker-b", provider_url=provider_url)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda worker: worker.run_once(), workers))
    assert len([outcome for outcome in outcomes if outcome]) == 1
    assert snapshot(app, run_id)["steps"][0]["attempts"] == 1


def test_terminal_provider_rejection_cancels_later_steps(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-22", "position": 1}}, {"payload": {"ticket": "A-22", "position": 2}}])
    worker = Worker(database, "worker-a", provider_url=provider_url)
    worker.tool.execute = lambda *_: (_ for _ in ()).throw(RuntimeError("provider rejected request"))
    assert worker.run_once()["failed"] is True
    completed = snapshot(app, run_id)
    assert completed["status"] == "failed"
    assert [step["status"] for step in completed["steps"]] == ["failed", "cancelled"]


def test_ambiguous_transport_failure_is_retried_with_the_same_key(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-23"}}])
    worker = Worker(database, "worker-a", provider_url=provider_url)
    worker.tool.base_url = "http://127.0.0.1:1"
    assert worker.run_once()["retry_scheduled"] is True
    assert snapshot(app, run_id)["steps"][0]["status"] == "queued"
    assert Worker(database, "worker-b", provider_url=provider_url).run_once()["replayed"] is False
    assert snapshot(app, run_id)["status"] == "succeeded"


def test_expired_lease_stops_after_its_attempt_budget(runner):
    app, database, provider_url = runner
    run_id = create_run(app, [{"payload": {"ticket": "A-24"}}])
    first = Worker(database, "worker-a", lease_seconds=0, max_attempts=1, provider_url=provider_url)
    with pytest.raises(SimulatedWorkerCrash):
        first.run_once(crash_after_tool=True)
    assert Worker(database, "worker-b", max_attempts=1, provider_url=provider_url).run_once() is None
    completed = snapshot(app, run_id)
    assert completed["status"] == "failed"
    assert completed["steps"][0]["error"] == "lease expired after 1 attempts"
