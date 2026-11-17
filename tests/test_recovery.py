from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import ToolEffect
from app.service import SimulatedWorkerCrash, Worker


@pytest.fixture()
def client():
    url = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://durable:durable@localhost:5432/durable_runs")
    app = create_app(url)
    app.state.database.reset_schema()
    with TestClient(app) as test_client:
        yield test_client


def test_run_completes_with_a_replayable_trace(client):
    created = client.post("/runs", json={"tool": "record", "payload": {"ticket": "A-17"}})
    assert created.status_code == 201
    run_id = created.json()["id"]

    outcome = Worker(client.app.state.database, "worker-a").run_once()
    assert outcome["replayed"] is False

    snapshot = client.get(f"/runs/{run_id}").json()
    assert snapshot["status"] == "succeeded"
    assert snapshot["steps"] == [{"id": outcome["step_id"], "status": "succeeded", "attempts": 1, "result": {"recorded": {"ticket": "A-17"}, "effect_key": f"{run_id}:step:1"}, "error": None}]
    assert [event["kind"] for event in snapshot["events"]] == ["run_created", "step_claimed", "tool_effect_created", "run_succeeded"]


def test_crash_after_effect_retries_without_a_duplicate_effect(client):
    run_id = client.post("/runs", json={"payload": {"ticket": "A-18"}}).json()["id"]
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
    run_id = client.post("/runs", json={"payload": {"ticket": "A-19"}}).json()["id"]
    cancelled = client.post(f"/runs/{run_id}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert Worker(client.app.state.database, "worker-a").run_once() is None
    with client.app.state.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ToolEffect)) == 0


def test_unknown_tools_are_rejected_at_the_api_boundary(client):
    response = client.post("/runs", json={"tool": "shell", "payload": {"command": "anything"}})
    assert response.status_code == 400
    assert response.json()["detail"] == "Only the idempotent record tool is available in v1"

