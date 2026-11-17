from __future__ import annotations

import os
import time

from app.database import Database
from app.service import Worker


def main() -> None:
    database = Database(os.environ.get("DATABASE_URL", "postgresql+psycopg://durable:durable@localhost:5432/durable_runs"))
    database.create_schema()
    worker = Worker(database, worker_id=os.environ.get("WORKER_ID", "worker-1"))
    while True:
        if not worker.run_once():
            time.sleep(0.25)


if __name__ == "__main__":
    main()

