"""Bounded concurrency check against a running local stack; not a capacity claim."""

import argparse
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--output", default="evidence/concurrency.json")
    args = parser.parse_args()
    if not 1 <= args.runs <= 200 or not 1 <= args.concurrency <= 16:
        parser.error("Use 1–200 runs and 1–16 clients")
    prefix = str(uuid4())
    headers = (
        {"Authorization": "Bearer " + os.environ["API_TOKEN"]}
        if os.environ.get("API_TOKEN")
        else {}
    )

    def execute(index):
        start = time.monotonic()
        key = f"{prefix}:{index}"
        body = {"steps": [{"payload": {"case": key, "step": step}} for step in [1, 2]]}
        with httpx.Client(
            base_url="http://127.0.0.1:8000",
            trust_env=False,
            timeout=10,
            headers=headers,
        ) as client:
            submitted = (
                client.post("/runs", json=body, headers={"Idempotency-Key": key})
                .raise_for_status()
                .json()
            )
            repeated = (
                client.post("/runs", json=body, headers={"Idempotency-Key": key})
                .raise_for_status()
                .json()
            )
            assert submitted["id"] == repeated["id"]
            deadline = start + 90
            while time.monotonic() < deadline:
                current = (
                    client.get("/runs/" + submitted["id"]).raise_for_status().json()
                )
                if current["status"] == "succeeded":
                    break
                assert current["status"] in {"queued", "running"}, current
                time.sleep(0.1)
            assert current["status"] == "succeeded", current
            assert [step["result"]["recorded"] for step in current["steps"]] == [
                step["payload"] for step in body["steps"]
            ]
            assert all(step["attempts"] == 1 for step in current["steps"])
            assert sum(e["kind"] == "run_created" for e in current["events"]) == 1
            return {
                "run_id": submitted["id"],
                "seconds": time.monotonic() - start,
                "steps": 2,
            }

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(execute, range(args.runs)))
    assert len({r["run_id"] for r in results}) == args.runs
    durations = sorted(r["seconds"] for r in results)
    report = {
        "scope": "One bounded functional trial; shared CPU host, one worker, no production capacity claim",
        "runs": args.runs,
        "clients": args.concurrency,
        "submissions": 2 * args.runs,
        "effects": 2 * args.runs,
        "elapsed_seconds": time.monotonic() - start,
        "completion_p50_seconds": durations[math.ceil(len(durations) * 0.5) - 1],
        "completion_p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1],
        "results": results,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
