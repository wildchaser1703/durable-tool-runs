import asyncio
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from test_recovery import api_request, create_run, snapshot
from test_recovery import provider_url as provider_url
from test_recovery import runner as runner

from app.http_limits import RequestLimits
from app.main import create_app
from app.service import RecordTool, RetryableToolError, Worker


def test_concurrent_duplicate_submissions_create_one_run(runner):
    app, database, url = runner
    request = {"steps": [{"payload": {"a": 1, "b": 2}}]}

    def submit(_):
        return api_request(
            app,
            "POST",
            "/runs",
            json=request,
            headers={"Idempotency-Key": "client-123"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert {r.status_code for r in responses} == {201}
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1
    run_id = ids.pop()
    Worker(database, "one", provider_url=url).run_once()
    # A retry after completion is the same run, even with reordered JSON keys.
    request["steps"][0]["payload"] = {"b": 2, "a": 1}
    assert submit(0).json()["id"] == run_id
    request["steps"][0]["payload"] = {"a": 999}
    assert submit(0).status_code == 409
    current = snapshot(app, run_id)
    assert current["steps"][0]["result"]["recorded"] == {"a": 1, "b": 2}
    assert sum(e["kind"] == "run_created" for e in current["events"]) == 1


def test_optional_auth_covers_data_mutation_and_metrics(runner, monkeypatch):
    _, database, _ = runner
    token = "test-token-" + "x" * 32
    monkeypatch.setenv("API_TOKEN", token)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    app = create_app(database.engine.url.render_as_string(hide_password=False))
    body = {"steps": [{"payload": {}}]}
    assert api_request(app, "POST", "/runs", json=body).status_code == 401
    good = {"Authorization": "Bearer " + token}
    response = api_request(app, "POST", "/runs", json=body, headers=good)
    assert response.status_code == 201
    run_id = response.json()["id"]
    for method, path in [
        ("GET", f"/runs/{run_id}"),
        ("GET", f"/runs/{run_id}/events"),
        ("POST", f"/runs/{run_id}/cancel"),
        ("POST", f"/runs/{run_id}/reconcile"),
        ("GET", "/metrics"),
    ]:
        assert api_request(app, method, path).status_code == 401
        assert (
            api_request(
                app, method, path, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert api_request(app, method, path, headers=good).status_code == 200
    assert api_request(app, "GET", "/health").status_code == 200
    assert api_request(app, "GET", "/docs").status_code == 200


def test_required_auth_fails_closed_at_startup(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.delenv("API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="API_TOKEN"):
        create_app()
    monkeypatch.setenv("API_TOKEN", "short")
    with pytest.raises(ValueError, match="API_TOKEN"):
        create_app()


def test_body_limit_applies_without_content_length(runner):
    app, _, _ = runner

    async def request():
        async def body():
            for _ in range(9):
                yield b"x" * 8192

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/runs", content=body())

    assert asyncio.run(request()).status_code == 413
    assert (
        api_request(
            app, "POST", "/runs", json={"steps": [{"payload": {}, "ignored": "typo"}]}
        ).status_code
        == 422
    )


def test_slow_body_times_out_before_application():
    messages = []

    async def forbidden_app(*_):
        raise AssertionError("unbounded request reached application")

    async def receive():
        await asyncio.sleep(1)

    async def send(message):
        messages.append(message)

    asyncio.run(
        RequestLimits(forbidden_app, timeout_seconds=0.01)(
            {"type": "http"}, receive, send
        )
    )
    assert messages[0]["status"] == 408


def test_nonfinite_json_and_invalid_submission_keys_are_rejected(runner):
    app, _, _ = runner
    response = api_request(
        app,
        "POST",
        "/runs",
        content='{"steps":[{"payload":{"n":NaN}}]}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    for key in ["spaces are not allowed", "x" * 129]:
        assert (
            api_request(
                app,
                "POST",
                "/runs",
                json={"steps": [{"payload": {}}]},
                headers={"Idempotency-Key": key},
            ).status_code
            == 422
        )


def test_event_pagination_has_no_gaps_or_duplicates(runner):
    app, database, url = runner
    run_id = create_run(app, [{"payload": {}}])
    Worker(database, "one", provider_url=url).run_once()
    seen, cursor = [], 0
    while True:
        response = api_request(
            app, "GET", f"/runs/{run_id}/events?after={cursor}&limit=2"
        )
        events = response.json()["events"]
        if not events:
            break
        assert len(events) <= 2
        seen.extend(events)
        cursor = events[-1]["id"]
    assert seen == snapshot(app, run_id)["events"]
    assert (
        api_request(app, "GET", f"/runs/{run_id}/events?limit=1001").status_code == 422
    )


def test_database_outage_returns_retryable_response_without_internal_details(
    runner, monkeypatch
):
    app, _, _ = runner

    def unavailable(*_):
        raise OperationalError("private SQL", {}, RuntimeError("private connection"))

    monkeypatch.setattr(app.state.service, "snapshot", unavailable)
    response = api_request(app, "GET", "/runs/anything")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "private" not in response.text


@pytest.mark.parametrize("status", [408, 429, 503])
def test_temporary_provider_response_is_retryable(monkeypatch, status):
    client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=transport))
    with pytest.raises(RetryableToolError):
        RecordTool("http://provider").execute("same-key", {})


def test_sigterm_finishes_current_step_without_claiming_next(runner, tmp_path):
    app, database, url = runner
    run_id = create_run(app, [{"payload": {"n": 1}}, {"payload": {"n": 2}}])
    marker = tmp_path / "effect-returned"
    environment = os.environ | {
        "DATABASE_URL": database.engine.url.render_as_string(hide_password=False),
        "TOOL_PROVIDER_URL": url,
        "METRICS_PORT": "0",
        "AFTER_TOOL_PAUSE_SECONDS": "1",
        "AFTER_TOOL_MARKER": str(marker),
    }
    process = subprocess.Popen([sys.executable, "-m", "app.worker"], env=environment)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        current = snapshot(app, run_id)
        assert current["status"] == "queued"
        assert [s["status"] for s in current["steps"]] == ["succeeded", "queued"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("cancelled", [False, True])
def test_reconcile_confirms_effect_and_resumes_only_uncancelled_work(
    runner, monkeypatch, cancelled
):
    app, database, url = runner
    monkeypatch.setenv("TOOL_PROVIDER_URL", url)
    run_id = create_run(app, [{"payload": {"n": 1}}, {"payload": {"n": 2}}])
    worker = Worker(database, "one", provider_url=url, max_attempts=1)
    claim = worker._claim()
    result, _ = worker.tool.execute(claim.key, claim.payload)
    if cancelled:
        app.state.service.cancel(run_id)
    worker._error(claim, "lost response", retryable=True)
    assert snapshot(app, run_id)["status"] == "needs_review"
    response = api_request(app, "POST", f"/runs/{run_id}/reconcile")
    assert response.status_code == 200
    current = response.json()
    assert current["status"] == ("cancelled" if cancelled else "queued")
    assert current["steps"][0]["result"] == result
    assert current["steps"][1]["status"] == ("cancelled" if cancelled else "queued")
    again = api_request(app, "POST", f"/runs/{run_id}/reconcile").json()
    assert again == current


def test_reconcile_does_not_reexecute_missing_or_conflicting_effect(
    runner, monkeypatch
):
    app, database, url = runner
    monkeypatch.setenv("TOOL_PROVIDER_URL", url)
    run_id = create_run(app, [{"payload": {"n": 1}}])
    worker = Worker(database, "one", provider_url=url, max_attempts=1)
    claim = worker._claim()
    worker._error(claim, "lost response", retryable=True)
    before = snapshot(app, run_id)
    assert api_request(app, "POST", f"/runs/{run_id}/reconcile").status_code == 409
    assert snapshot(app, run_id) == before
    worker.tool.execute(claim.key, {"n": 2})
    assert api_request(app, "POST", f"/runs/{run_id}/reconcile").status_code == 409
    assert snapshot(app, run_id) == before
