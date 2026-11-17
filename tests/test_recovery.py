from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import ToolEffect
from app.service import SimulatedWorkerCrash, Worker


@pytest.fixture()
def client():
    url = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://durable:durable@localhost:5433/durable_runs_test")
    app = create_app(url)
    app.state.database.reset_schema()
    with TestClient(app) as test_client:
        yield test_client


def test_run_completes_with_a_replayable_trace(client):
    created = client.post("/runs", json={"steps": [{"tool": "record", "payload": {"ticket": "A-17"}}]})
    assert created.status_code == 201
    run_id = created.json()["id"]

    outcome = Worker(client.app.state.database, "worker-a").run_once()
    assert outcome["replayed"] is False

    snapshot = client.get(f"/runs/{run_id}").json()
    assert snapshot["status"] == "succeeded"
    assert snapshot["steps"] == [{"id": outcome["step_id"], "position": 1, "status": "succeeded", "attempts": 1, "result": {"recorded": {"ticket": "A-17"}, "effect_key": f"{run_id}:step:1"}, "error": None}]
    assert [event["kind"] for event in snapshot["events"]] == ["run_created", "step_claimed", "tool_effect_created", "run_succeeded"]


def test_crash_after_effect_retries_without_a_duplicate_effect(client):
    run_id = client.post("/runs", json={"steps": [{"payload": {"ticket": "A-18"}}]}).json()["id"]
    first = Worker(client.app.state.database, "worker-a", lease_seconds=0)
    with pytest.raises(SimulatedWorkerCrash):
        first.run_once(crash_after_tool=True)

    with client.app.state.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ToolEffect)) == 1

    recovered = Worker(client.app.state.database, "worker-b", lease_seconds=0).run_once()
    assert recovered["replayed"] is True
    snapshot = client.get(f"/runs/{run_id}").json()
    assert snapshot["status"] == "succeeded"
    assert snapshot["steps"][0]["attempts"] == 2
    assert [event["kind"] for event in snapshot["events"]] == ["run_created", "step_claimed", "step_reclaimed", "tool_replayed", "run_succeeded"]
    with client.app.state.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ToolEffect)) == 1


def test_queued_run_cancels_without_an_effect(client):
    run_id = client.post("/runs", json={"steps": [{"payload": {"ticket": "A-19"}}]}).json()["id"]
    cancelled = client.post(f"/runs/{run_id}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert Worker(client.app.state.database, "worker-a").run_once() is None
    with client.app.state.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ToolEffect)) == 0


def test_unknown_tools_are_rejected_at_the_api_boundary(client):
    response = client.post("/runs", json={"steps": [{"tool": "shell", "payload": {"command": "anything"}}]})
    assert response.status_code == 400
    assert response.json()["detail"] == "Only one or more idempotent record steps are available in v1"


def test_steps_run_in_order_and_events_can_resume_from_a_cursor(client):
    run_id = client.post("/runs", json={"steps": [
        {"payload": {"ticket": "A-20", "position": 1}},
        {"payload": {"ticket": "A-20", "position": 2}},
    ]}).json()["id"]
    worker = Worker(client.app.state.database, "worker-a")

    first = worker.run_once()
    midway = client.get(f"/runs/{run_id}").json()
    assert midway["status"] == "queued"
    assert [step["status"] for step in midway["steps"]] == ["succeeded", "queued"]
    assert [event["kind"] for event in midway["events"]] == ["run_created", "step_claimed", "tool_effect_created", "step_succeeded"]

    second = worker.run_once()
    completed = client.get(f"/runs/{run_id}").json()
    assert [first["step_id"], second["step_id"]] == [step["id"] for step in completed["steps"]]
    assert completed["status"] == "succeeded"
    resumed = client.get(f"/runs/{run_id}/events?after={midway['events'][-1]['id']}").json()["events"]
    assert [event["kind"] for event in resumed] == ["step_claimed", "tool_effect_created", "run_succeeded"]


def test_two_workers_cannot_claim_one_step_twice(client):
    run_id = client.post("/runs", json={"steps": [{"payload": {"ticket": "A-21"}}]}).json()["id"]
    workers = [Worker(client.app.state.database, "worker-a"), Worker(client.app.state.database, "worker-b")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda worker: worker.run_once(), workers))
    assert len([outcome for outcome in outcomes if outcome]) == 1
    snapshot = client.get(f"/runs/{run_id}").json()
    assert snapshot["steps"][0]["attempts"] == 1
    with client.app.state.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ToolEffect)) == 1


def test_tool_failure_is_terminal_and_cancels_later_steps(client):
    run_id = client.post("/runs", json={"steps": [
        {"payload": {"ticket": "A-22", "position": 1}},
        {"payload": {"ticket": "A-22", "position": 2}},
    ]}).json()["id"]
    worker = Worker(client.app.state.database, "worker-a")
    worker.tool.execute = lambda *_: (_ for _ in ()).throw(RuntimeError("provider unavailable"))

    assert worker.run_once()["failed"] is True
    snapshot = client.get(f"/runs/{run_id}").json()
    assert snapshot["status"] == "failed"
    assert [step["status"] for step in snapshot["steps"]] == ["failed", "cancelled"]
    assert [event["kind"] for event in snapshot["events"]][-2:] == ["tool_failed", "run_failed"]


def test_expired_lease_stops_after_its_attempt_budget(client):
    run_id = client.post("/runs", json={"steps": [{"payload": {"ticket": "A-23"}}]}).json()["id"]
    first = Worker(client.app.state.database, "worker-a", lease_seconds=0, max_attempts=1)
    with pytest.raises(SimulatedWorkerCrash):
        first.run_once(crash_after_tool=True)

    assert Worker(client.app.state.database, "worker-b", max_attempts=1).run_once() is None
    snapshot = client.get(f"/runs/{run_id}").json()
    assert snapshot["status"] == "failed"
    assert snapshot["steps"][0]["error"] == "lease expired after 1 attempts"

