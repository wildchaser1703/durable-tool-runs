from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.database import Database
from app.service import RunService


class RequestedStep(BaseModel):
    tool: str = Field(default="record")
    payload: dict


class CreateRun(BaseModel):
    steps: list[RequestedStep] = Field(min_length=1, max_length=20)


def create_app(database_url: str | None = None) -> FastAPI:
    database = Database(
        database_url
        or os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://durable:durable@localhost:5432/durable_runs",
        )
    )
    service = RunService(database)
    app = FastAPI(
        title="Durable Tool Runs", version="0.2.0", docs_url=None, redoc_url=None
    )
    app.mount(
        "/docs-assets",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="docs-assets",
    )

    @app.get("/docs", include_in_schema=False)
    def documentation():
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="Durable Tool Runs API",
            swagger_js_url="/docs-assets/swagger-ui-bundle.js",
            swagger_css_url="/docs-assets/swagger-ui.css",
            swagger_favicon_url="/docs-assets/favicon-32x32.png",
        )

    app.state.database = database
    app.state.service = service

    @app.get("/health")
    def health() -> dict:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM runs LIMIT 1"))
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        from app.metrics import render_metrics

        return render_metrics()

    @app.post("/runs", status_code=201)
    def create_run(request: CreateRun) -> dict:
        try:
            run = service.create_run([step.model_dump() for step in request.steps])
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return service.snapshot(run.id)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            return service.snapshot(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found") from error

    @app.get("/runs/{run_id}/events")
    def get_events(run_id: str, after: int = Query(default=0, ge=0)) -> dict:
        try:
            return {"events": service.events_since(run_id, after)}
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
