from __future__ import annotations

import os
import argparse
import time

from prometheus_client import start_http_server

from app.database import Database
from app.service import Worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a durable-tool-runs worker")
    parser.add_argument("--once", action="store_true", help="claim and execute at most one step")
    args = parser.parse_args()
    database = Database(os.environ.get("DATABASE_URL", "postgresql+psycopg://durable:durable@localhost:5432/durable_runs"))
    worker = Worker(
        database,
        worker_id=os.environ.get("WORKER_ID", "worker-1"),
        lease_seconds=int(os.environ.get("LEASE_SECONDS", "30")),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
        after_tool_pause_seconds=float(os.environ.get("AFTER_TOOL_PAUSE_SECONDS", "0")),
        after_tool_marker=os.environ.get("AFTER_TOOL_MARKER"),
    )
    # Metrics are process-local. A worker gets its own scrape target instead
    # of exposing misleading zero-valued worker counters from the API process.
    start_http_server(int(os.environ.get("METRICS_PORT", "9100")))
    if args.once:
        worker.run_once()
        return
    while True:
        if not worker.run_once():
            time.sleep(0.25)


if __name__ == "__main__":
    main()

