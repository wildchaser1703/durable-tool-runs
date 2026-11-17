"""A separately deployed idempotent tool provider used by the runner demo.

Its SQLite database is intentionally independent from the runner's PostgreSQL
database. The HTTP boundary means a worker can lose its process after the
provider commits, before the runner records the result.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


def database_path() -> str:
    return os.environ.get("PROVIDER_DATABASE_PATH", "/tmp/durable-tool-provider.db")


def connection() -> sqlite3.Connection:
    path = database_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize() -> None:
    with closing(connection()) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS effects (
                idempotency_key TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.commit()


class RecordRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    payload: dict


def create_provider_app() -> FastAPI:
    app = FastAPI(title="Idempotent Record Provider", version="0.1.0")

    @app.on_event("startup")
    def startup() -> None:
        initialize()

    @app.get("/health")
    def health() -> dict:
        initialize()
        return {"status": "ok"}

    @app.post("/effects")
    def record(request: RecordRequest) -> dict:
        result = {"recorded": request.payload, "effect_key": request.idempotency_key}
        with closing(connection()) as conn:
            try:
                conn.execute(
                    "INSERT INTO effects (idempotency_key, result_json) VALUES (?, ?)",
                    (request.idempotency_key, json.dumps(result, sort_keys=True)),
                )
                conn.commit()
                return {"result": result, "replayed": False}
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT result_json FROM effects WHERE idempotency_key = ?", (request.idempotency_key,)
                ).fetchone()
                if not row:
                    raise
                return {"result": json.loads(row[0]), "replayed": True}

    @app.get("/effects/{idempotency_key}")
    def effect(idempotency_key: str) -> dict:
        with closing(connection()) as conn:
            row = conn.execute(
                "SELECT result_json, created_at FROM effects WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Effect not found")
        return {"result": json.loads(row[0]), "created_at": row[1]}

    return app


app = create_provider_app()
