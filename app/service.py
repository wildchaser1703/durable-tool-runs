from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.models import Run, RunEvent, Step, ToolEffect


class SimulatedWorkerCrash(RuntimeError):
    """Used only by the integration suite to model a process dying after an effect."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def event(session, run_id: str, kind: str, detail: dict | None = None, step_id: str | None = None) -> None:
    session.add(RunEvent(run_id=run_id, step_id=step_id, kind=kind, detail=detail or {}))


class RecordTool:
    """A controlled external effect with idempotency semantics.

    It commits independently from the worker. A crash after this commit but before
    the worker stores its result is the ambiguity a durable runner must recover from.
    """

    def __init__(self, database: Database):
        self.database = database

    def execute(self, idempotency_key: str, payload: dict) -> tuple[dict, bool]:
        result = {"recorded": payload, "effect_key": idempotency_key}
        with self.database.sessions() as session:
            existing = session.get(ToolEffect, idempotency_key)
            if existing:
                return existing.result, True
            try:
                session.add(ToolEffect(idempotency_key=idempotency_key, result=result))
                session.commit()
                return result, False
            except IntegrityError:
                session.rollback()
                existing = session.get(ToolEffect, idempotency_key)
                if not existing:
                    raise
                return existing.result, True


class RunService:
    def __init__(self, database: Database):
        self.database = database

    def create_run(self, tool_name: str, payload: dict) -> Run:
        if tool_name != "record":
            raise ValueError("Only the idempotent record tool is available in v1")
        with self.database.sessions() as session:
            run = Run(status="queued")
            session.add(run)
            session.flush()
            step = Step(run_id=run.id, tool_name=tool_name, payload=payload, idempotency_key=f"{run.id}:step:1")
            session.add(step)
            event(session, run.id, "run_created", {"tool": tool_name}, step.id)
            session.commit()
            return run

    def cancel(self, run_id: str) -> Run:
        with self.database.sessions() as session:
            run = session.get(Run, run_id)
            if not run:
                raise KeyError(run_id)
            if run.status in {"succeeded", "failed", "cancelled"}:
                return run
            run.cancellation_requested = True
            step = session.scalar(select(Step).where(Step.run_id == run.id))
            if step and step.status == "queued":
                step.status = "cancelled"
                run.status = "cancelled"
                event(session, run.id, "run_cancelled", {"before_execution": True}, step.id)
            else:
                event(session, run.id, "cancellation_requested")
            session.commit()
            return run

    def snapshot(self, run_id: str) -> dict:
        with self.database.sessions() as session:
            run = session.get(Run, run_id)
            if not run:
                raise KeyError(run_id)
            steps = session.scalars(select(Step).where(Step.run_id == run.id).order_by(Step.created_at)).all()
            events = session.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.created_at, RunEvent.id)).all()
            return {
                "id": run.id,
                "status": run.status,
                "cancellation_requested": run.cancellation_requested,
                "steps": [{"id": s.id, "status": s.status, "attempts": s.attempts, "result": s.result, "error": s.error} for s in steps],
                "events": [{"kind": e.kind, "detail": e.detail, "step_id": e.step_id} for e in events],
            }


class Worker:
    def __init__(self, database: Database, worker_id: str, lease_seconds: int = 30):
        self.database = database
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.tool = RecordTool(database)

    def _claim(self) -> tuple[str, str] | None:
        with self.database.sessions() as session:
            current = now()
            candidate = session.scalar(
                select(Step)
                .where(or_(Step.status == "queued", and_(Step.status == "running", Step.lease_expires_at < current)))
                .order_by(Step.created_at)
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
            candidate.status = "running"
            candidate.attempts += 1
            candidate.lease_owner = self.worker_id
            candidate.lease_expires_at = current + timedelta(seconds=self.lease_seconds)
            run.status = "running"
            event(session, run.id, "step_reclaimed" if reclaimed else "step_claimed", {"worker": self.worker_id, "attempt": candidate.attempts}, candidate.id)
            session.commit()
            return candidate.id, candidate.idempotency_key

    def run_once(self, crash_after_tool: bool = False) -> dict | None:
        claim = self._claim()
        if not claim:
            return None
        step_id, idempotency_key = claim
        with self.database.sessions() as session:
            step = session.get(Step, step_id)
            payload = step.payload
        result, replayed = self.tool.execute(idempotency_key, payload)
        if crash_after_tool:
            raise SimulatedWorkerCrash("worker ended after the external effect committed")
        with self.database.sessions() as session:
            step = session.get(Step, step_id)
            run = session.get(Run, step.run_id)
            if step.lease_owner != self.worker_id:
                raise RuntimeError("worker no longer owns this lease")
            step.status = "succeeded"
            step.result = result
            step.lease_owner = None
            step.lease_expires_at = None
            run.status = "succeeded"
            event(session, run.id, "tool_replayed" if replayed else "tool_effect_created", {"idempotency_key": idempotency_key}, step.id)
            event(session, run.id, "run_succeeded", step_id=step.id)
            session.commit()
            return {"run_id": run.id, "step_id": step.id, "replayed": replayed}

