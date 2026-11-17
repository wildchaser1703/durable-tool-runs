from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.database import Database
from app.service import RunService


class CreateRun(BaseModel):
    tool: str = Field(default="record")
    payload: dict


def create_app(database_url: str | None = None) -> FastAPI:
    database = Database(database_url or os.environ.get("DATABASE_URL", "postgresql+psycopg://durable:durable@localhost:5432/durable_runs"))
    database.create_schema()
    service = RunService(database)
    app = FastAPI(title="Durable Tool Runs", version="0.1.0")
    app.state.database = database
    app.state.service = service

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/runs", status_code=201)
    def create_run(request: CreateRun) -> dict:
        try:
            run = service.create_run(request.tool, request.payload)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return service.snapshot(run.id)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            return service.snapshot(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found") from error

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict:
        try:
            service.cancel(run_id)
            return service.snapshot(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found") from error

    return app


app = create_app()

