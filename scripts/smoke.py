"""Assert a real composed run, persisted result, ordered events and metrics."""

import time

import httpx


def main():
    with httpx.Client(
        base_url="http://127.0.0.1:8000", timeout=10, trust_env=False
    ) as client:
        client.get("/health").raise_for_status()
        response = client.post(
            "/runs",
            json={
                "steps": [
                    {"tool": "record", "payload": {"stage": "capture"}},
                    {"tool": "record", "payload": {"stage": "confirm"}},
                ]
            },
        )
        response.raise_for_status()
        run_id = response.json()["id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            current = client.get(f"/runs/{run_id}").json()
            if current["status"] == "succeeded":
                break
            assert current["status"] not in {"failed", "needs_review", "cancelled"}, (
                current
            )
            time.sleep(0.1)
        assert current["status"] == "succeeded", current
        assert [step["result"]["recorded"]["stage"] for step in current["steps"]] == [
            "capture",
            "confirm",
        ]
        kinds = [event["kind"] for event in current["events"]]
        assert kinds == [
            "run_created",
            "step_claimed",
            "tool_effect_created",
            "step_succeeded",
            "step_claimed",
            "tool_effect_created",
            "run_succeeded",
        ]
        cursor = current["events"][-1]["id"]
        assert client.get(f"/runs/{run_id}/events?after={cursor}").json() == {
            "events": []
        }
        client.get("/metrics").raise_for_status()
        response = client.get("http://127.0.0.1:9100/metrics")
        response.raise_for_status()
        assert 'durable_tool_calls_total{outcome="created"}' in response.text
        print({"run_id": run_id, "status": current["status"], "events": kinds})


if __name__ == "__main__":
    main()
