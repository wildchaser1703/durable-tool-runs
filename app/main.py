from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.database import Database
from app.http_limits import RequestLimits
from app.service import IdempotencyConflict, RunService


class RequestedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(default="record")
    payload: dict

    @field_validator("payload")
    @classmethod
    def finite_json(cls, value):
        json.dumps(value, allow_nan=False)
        return value


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    token = os.environ.get("API_TOKEN", "")
    if (os.environ.get("REQUIRE_AUTH") == "1" and not token) or (
        token and len(token) < 32
    ):
        raise ValueError(
            "Configure API_TOKEN with at least 32 characters when authentication is enabled"
        )
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if token and (
            credentials is None
            or not secrets.compare_digest(
                credentials.credentials.encode(), token.encode()
            )
        ):
            raise HTTPException(
                401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
            )

    protected = [Depends(authorize)]
    app = FastAPI(
        title="Durable Tool Runs", version="0.3.0", docs_url=None, redoc_url=None
    )
    app.add_middleware(RequestLimits)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Avoid echoing arbitrary payloads (including non-finite floats) in errors.
        return JSONResponse(
            {
                "detail": [
                    {"loc": e["loc"], "msg": e["msg"], "type": e["type"]}
                    for e in error.errors()
                ]
            },
            status_code=422,
        )

    @app.exception_handler(OperationalError)
    @app.exception_handler(PoolTimeout)
    async def database_unavailable(request, error):
        return JSONResponse(
            {
                "detail": "Database temporarily unavailable; retry with the same Idempotency-Key"
            },
            status_code=503,
            headers={"Retry-After": "1"},
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

    @app.get("/metrics", include_in_schema=False, dependencies=protected)
    def metrics():
        from app.metrics import render_metrics

        return render_metrics()

    @app.post("/runs", status_code=201, dependencies=protected)
    def create_run(
        request: CreateRun,
        idempotency_key: str | None = Header(
            default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
        ),
    ) -> dict:
        try:
            run = service.create_run(
                [step.model_dump() for step in request.steps], idempotency_key
            )
        except IdempotencyConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return service.snapshot(run.id)

    @app.get("/runs/{run_id}", dependencies=protected)
    def get_run(run_id: str) -> dict:
        try:
            return service.snapshot(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found") from error

    @app.get("/runs/{run_id}/events", dependencies=protected)
    def get_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        try:
            return {"events": service.events_since(run_id, after, limit)}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found") from error

    @app.post("/runs/{run_id}/cancel", dependencies=protected)
    def cancel(run_id: str) -> dict:
        try:
            service.cancel(run_id)
            return service.snapshot(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found") from error

    @app.post("/runs/{run_id}/reconcile", dependencies=protected)
    def reconcile(run_id: str) -> dict:
        try:
            service.reconcile(run_id)
            return service.snapshot(run_id)
        except KeyError as error:
            raise HTTPException(404, "Run or provider result not found") from error
        except IdempotencyConflict as error:
            raise HTTPException(409, str(error)) from error
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise HTTPException(
                503, "Provider result unavailable; run unchanged"
            ) from error

    return app


app = create_app()
