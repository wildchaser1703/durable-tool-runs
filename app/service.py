from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased

from app.database import Database
from app.metrics import RUNS_CREATED, STEP_DURATION, STEPS_CLAIMED, TOOL_CALLS
from app.models import Run, RunEvent, Step


class SimulatedWorkerCrash(RuntimeError):
    """Used only by the integration suite to model a process dying after an effect."""


class ToolExecutionError(RuntimeError):
    """A terminal v1 tool failure; callers receive an inspectable run result."""


class RetryableToolError(RuntimeError):
    """The remote outcome may be unknown; replay later with the same key."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def event(session, run_id: str, kind: str, detail: dict | None = None, step_id: str | None = None) -> None:
    session.add(RunEvent(run_id=run_id, step_id=step_id, kind=kind, detail=detail or {}))


class RecordTool:
    """HTTP client for an independently persisted idempotent provider."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def execute(self, idempotency_key: str, payload: dict) -> tuple[dict, bool]:
        try:
            # The provider is an internal service boundary. Do not accidentally
            # route its hostname through a developer or platform HTTP proxy.
            with httpx.Client(trust_env=False, timeout=10) as client:
                response = client.post(
                    f"{self.base_url}/effects",
                    json={"idempotency_key": idempotency_key, "payload": payload},
                )
        except httpx.HTTPError as error:
            TOOL_CALLS.labels(outcome="transport_error").inc()
            raise RetryableToolError(f"provider transport outcome is unknown: {error}") from error
        if response.status_code >= 500:
            TOOL_CALLS.labels(outcome="provider_error").inc()
            raise RetryableToolError(f"provider returned {response.status_code}")
        if response.status_code >= 400:
            TOOL_CALLS.labels(outcome="rejected").inc()
            raise ToolExecutionError(f"provider rejected request with {response.status_code}: {response.text}")
        body = response.json()
        if not isinstance(body.get("result"), dict) or not isinstance(body.get("replayed"), bool):
            raise ToolExecutionError("provider returned an invalid response")
        TOOL_CALLS.labels(outcome="replayed" if body["replayed"] else "created").inc()
        return body["result"], body["replayed"]


class RunService:
    def __init__(self, database: Database):
        self.database = database

    def create_run(self, requested_steps: list[dict]) -> Run:
        if not requested_steps or any(step["tool"] != "record" for step in requested_steps):
            raise ValueError("Only one or more idempotent record steps are available in v1")
        with self.database.sessions() as session:
            run = Run(status="queued")
            session.add(run)
            session.flush()
            for position, requested in enumerate(requested_steps, start=1):
                session.add(Step(
                    run_id=run.id,
                    position=position,
                    tool_name=requested["tool"],
                    payload=requested["payload"],
                    idempotency_key=f"{run.id}:step:{position}",
                ))
            event(session, run.id, "run_created", {"steps": len(requested_steps)})
            session.commit()
            RUNS_CREATED.inc()
            return run

    def cancel(self, run_id: str) -> Run:
        with self.database.sessions() as session:
            run = session.get(Run, run_id)
            if not run:
                raise KeyError(run_id)
            if run.status in {"succeeded", "failed", "cancelled"}:
                return run
            run.cancellation_requested = True
            steps = session.scalars(select(Step).where(Step.run_id == run.id)).all()
            queued = [step for step in steps if step.status == "queued"]
            running = any(step.status == "running" for step in steps)
            for step in queued:
                step.status = "cancelled"
            if running:
                run.status = "cancelling"
                event(session, run.id, "cancellation_requested", {"queued_steps_cancelled": len(queued)})
            else:
                run.status = "cancelled"
                event(session, run.id, "run_cancelled", {"before_execution": True, "queued_steps_cancelled": len(queued)})
            session.commit()
            return run

    def snapshot(self, run_id: str) -> dict:
        with self.database.sessions() as session:
            run = session.get(Run, run_id)
            if not run:
                raise KeyError(run_id)
            steps = session.scalars(select(Step).where(Step.run_id == run.id).order_by(Step.position)).all()
            events = session.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.id)).all()
            return {
                "id": run.id,
                "status": run.status,
                "cancellation_requested": run.cancellation_requested,
                "steps": [{"id": s.id, "position": s.position, "status": s.status, "attempts": s.attempts, "result": s.result, "error": s.error} for s in steps],
                "events": [{"id": e.id, "kind": e.kind, "detail": e.detail, "step_id": e.step_id} for e in events],
            }

    def events_since(self, run_id: str, after: int = 0) -> list[dict]:
        with self.database.sessions() as session:
            if not session.get(Run, run_id):
                raise KeyError(run_id)
            events = session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.id > after).order_by(RunEvent.id)
            ).all()
            return [{"id": item.id, "kind": item.kind, "detail": item.detail, "step_id": item.step_id} for item in events]


class Worker:
    def __init__(
        self,
        database: Database,
        worker_id: str,
        lease_seconds: int = 30,
        max_attempts: int = 3,
        provider_url: str | None = None,
        after_tool_pause_seconds: float = 0,
        after_tool_marker: str | None = None,
    ):
        self.database = database
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.tool = RecordTool(provider_url or os.environ.get("TOOL_PROVIDER_URL", "http://localhost:8081"))
        self.after_tool_pause_seconds = after_tool_pause_seconds
        self.after_tool_marker = after_tool_marker

    def _claim(self) -> tuple[str, str] | None:
        with self.database.sessions() as session:
            current = now()
            predecessor = aliased(Step)
            has_unfinished_predecessor = exists(
                select(predecessor.id).where(
                    predecessor.run_id == Step.run_id,
                    predecessor.position < Step.position,
                    predecessor.status != "succeeded",
                )
            )
            candidate = session.scalar(
                select(Step)
                .where(
                    or_(Step.status == "queued", and_(Step.status == "running", Step.lease_expires_at < current)),
                    ~has_unfinished_predecessor,
                )
                .order_by(Step.created_at, Step.position)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not candidate:
                return None
            run = session.get(Run, candidate.run_id)
            if run.cancellation_requested:
                candidate.status = "cancelled"
                run.status = "cancelled"
                event(session, run.id, "run_cancelled", {"before_execution": True}, candidate.id)
                session.commit()
                return None
            reclaimed = candidate.status == "running"
            if reclaimed and candidate.attempts >= self.max_attempts:
                candidate.status = "failed"
                candidate.error = f"lease expired after {candidate.attempts} attempts"
                run.status = "failed"
                for pending in session.scalars(select(Step).where(Step.run_id == run.id, Step.status == "queued")):
                    pending.status = "cancelled"
                event(session, run.id, "run_failed", {"reason": candidate.error}, candidate.id)
                session.commit()
                STEPS_CLAIMED.labels(outcome="attempts_exhausted").inc()
                return None
            candidate.status = "running"
            candidate.attempts += 1
            candidate.lease_owner = self.worker_id
            candidate.lease_expires_at = current + timedelta(seconds=self.lease_seconds)
            run.status = "running"
            event(session, run.id, "step_reclaimed" if reclaimed else "step_claimed", {"worker": self.worker_id, "attempt": candidate.attempts}, candidate.id)
            session.commit()
            STEPS_CLAIMED.labels(outcome="reclaimed" if reclaimed else "claimed").inc()
            return candidate.id, candidate.idempotency_key

    def _renew_lease(self, step_id: str) -> bool:
        with self.database.sessions() as session:
            step = session.get(Step, step_id)
            if not step or step.status != "running" or step.lease_owner != self.worker_id:
                return False
            step.lease_expires_at = now() + timedelta(seconds=self.lease_seconds)
            session.commit()
            return True

    def _heartbeat(self, step_id: str, stop: threading.Event) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while not stop.wait(interval):
            if not self._renew_lease(step_id):
                return

    def run_once(self, crash_after_tool: bool = False) -> dict | None:
        claim = self._claim()
        if not claim:
            return None
        step_id, idempotency_key = claim
        with self.database.sessions() as session:
            step = session.get(Step, step_id)
            payload = step.payload
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(step_id, stop_heartbeat), daemon=True)
        heartbeat.start()
        try:
            with STEP_DURATION.time():
                result, replayed = self.tool.execute(idempotency_key, payload)
        except SimulatedWorkerCrash:
            raise
        except RetryableToolError as error:
            return self._retry_or_fail(step_id, str(error))
        except Exception as error:
            return self._fail(step_id, str(error))
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)
        if crash_after_tool:
            raise SimulatedWorkerCrash("worker ended after the external effect committed")
        if self.after_tool_marker:
            Path(self.after_tool_marker).touch()
        if self.after_tool_pause_seconds:
            time.sleep(self.after_tool_pause_seconds)
        with self.database.sessions() as session:
            step = session.get(Step, step_id)
            run = session.get(Run, step.run_id)
            if step.lease_owner != self.worker_id:
                raise RuntimeError("worker no longer owns this lease")
            step.status = "succeeded"
            step.result = result
            step.lease_owner = None
            step.lease_expires_at = None
            event(session, run.id, "tool_replayed" if replayed else "tool_effect_created", {"idempotency_key": idempotency_key}, step.id)
            if run.cancellation_requested:
                run.status = "cancelled"
                event(session, run.id, "run_cancelled", {"after_effect": True}, step.id)
            elif session.scalar(select(Step.id).where(Step.run_id == run.id, Step.status != "succeeded").limit(1)):
                run.status = "queued"
                event(session, run.id, "step_succeeded", {"position": step.position}, step.id)
            else:
                run.status = "succeeded"
                event(session, run.id, "run_succeeded", step_id=step.id)
            session.commit()
            return {"run_id": run.id, "step_id": step.id, "replayed": replayed}

    def _retry_or_fail(self, step_id: str, error: str) -> dict:
        with self.database.sessions() as session:
            step = session.get(Step, step_id)
            run = session.get(Run, step.run_id)
            if step.lease_owner != self.worker_id:
                raise RuntimeError("worker no longer owns this lease")
            if step.attempts >= self.max_attempts:
                step.status = "failed"
                step.error = f"attempt budget exhausted: {error}"
                run.status = "failed"
                for pending in session.scalars(select(Step).where(Step.run_id == run.id, Step.status == "queued")):
                    pending.status = "cancelled"
                event(session, run.id, "run_failed", {"reason": step.error}, step.id)
                session.commit()
                return {"run_id": run.id, "step_id": step.id, "failed": True}
            step.status = "queued"
            step.error = error
            step.lease_owner = None
            step.lease_expires_at = None
            run.status = "queued"
            event(session, run.id, "step_retry_scheduled", {"attempt": step.attempts, "reason": error}, step.id)
            session.commit()
            return {"run_id": run.id, "step_id": step.id, "retry_scheduled": True}

    def _fail(self, step_id: str, error: str) -> dict:
        with self.database.sessions() as session:
            step = session.get(Step, step_id)
            run = session.get(Run, step.run_id)
            if step.lease_owner != self.worker_id:
                raise RuntimeError("worker no longer owns this lease")
            step.status = "failed"
            step.error = error
            step.lease_owner = None
            step.lease_expires_at = None
            run.status = "failed"
            for pending in session.scalars(select(Step).where(Step.run_id == run.id, Step.status == "queued")):
                pending.status = "cancelled"
            event(session, run.id, "tool_failed", {"error": error}, step.id)
            event(session, run.id, "run_failed", {"failed_step": step.position}, step.id)
            session.commit()
            return {"run_id": run.id, "step_id": step.id, "failed": True}

