from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from uuid import uuid4

from prometheus_client import start_http_server
from sqlalchemy.exc import OperationalError

from app.database import Database
from app.service import Worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a durable-tool-runs worker")
    parser.add_argument(
        "--once", action="store_true", help="claim and execute at most one step"
    )
    args = parser.parse_args()
    database = Database(
        os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://durable:durable@localhost:5432/durable_runs",
        )
    )
    worker = Worker(
        database,
        worker_id=os.environ.get("WORKER_ID", f"worker-{uuid4()}"),
        lease_seconds=int(os.environ.get("LEASE_SECONDS", "30")),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
        retry_base_seconds=float(os.environ.get("RETRY_BASE_SECONDS", "1")),
        retry_cap_seconds=float(os.environ.get("RETRY_CAP_SECONDS", "30")),
        after_tool_pause_seconds=float(os.environ.get("AFTER_TOOL_PAUSE_SECONDS", "0")),
        after_tool_marker=os.environ.get("AFTER_TOOL_MARKER"),
    )
    # Metrics are process-local. A worker gets its own scrape target instead
    # of exposing misleading zero-valued worker counters from the API process.
    start_http_server(int(os.environ.get("METRICS_PORT", "9100")))
    if args.once:
        worker.run_once()
        return
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            outcome = worker.run_once()
        except OperationalError:
            # A database restart must not permanently remove a worker from the
            # pool. The lease protocol handles any work it had previously held.
            logging.getLogger(__name__).warning("database unavailable; retrying")
            stop.wait(1)
            continue
        if not outcome:
            stop.wait(0.25)


if __name__ == "__main__":
    main()
