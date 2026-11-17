from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
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


class IdempotencyConflict(ValueError):
    """A submission key cannot identify two different requests."""


def event(
    session,
    run_id: str,
    kind: str,
    detail: dict | None = None,
    step_id: str | None = None,
) -> None:
    session.add(
        RunEvent(run_id=run_id, step_id=step_id, kind=kind, detail=detail or {})
    )


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
            raise RetryableToolError(
                f"provider transport outcome is unknown: {error}"
            ) from error
        if response.status_code >= 500 or response.status_code in {408, 429}:
            TOOL_CALLS.labels(outcome="provider_error").inc()
            raise RetryableToolError(f"provider returned {response.status_code}")
        if response.status_code >= 400:
            TOOL_CALLS.labels(outcome="rejected").inc()
            raise ToolExecutionError(
                f"provider rejected request with {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as error:
            raise RetryableToolError(
                "provider returned invalid JSON after possible effect"
            ) from error
        if not isinstance(body, dict):
            raise RetryableToolError(
                "provider returned an invalid response after possible effect"
            )
        if not isinstance(body.get("result"), dict) or not isinstance(
            body.get("replayed"), bool
        ):
            raise RetryableToolError(
                "provider returned an invalid response after possible effect"
            )
        TOOL_CALLS.labels(outcome="replayed" if body["replayed"] else "created").inc()
        return body["result"], body["replayed"]


class RunService:
    def __init__(self, database: Database):
        self.database = database

    def create_run(
        self, requested_steps: list[dict], request_key: str | None = None
    ) -> Run:
        if not requested_steps or any(
            step["tool"] != "record" for step in requested_steps
        ):
            raise ValueError(
                "Only one or more idempotent record steps are available in v1"
            )
        with self.database.sessions() as session:
            if request_key:
                fingerprint = hashlib.sha256(
                    json.dumps(
                        requested_steps,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                run_id = session.scalar(
                    insert(Run)
                    .values(
                        id=str(uuid4()),
                        status="queued",
                        cancellation_requested=False,
                        request_key=request_key,
                        request_hash=fingerprint,
                    )
                    .on_conflict_do_nothing(index_elements=[Run.request_key])
                    .returning(Run.id)
                )
                if run_id is None:
                    existing = session.scalar(
                        select(Run).where(Run.request_key == request_key)
                    )
                    if existing.request_hash != fingerprint:
                        raise IdempotencyConflict(
                            "Idempotency-Key was used with a different request"
                        )
                    return existing
                run = session.get(Run, run_id)
            else:
                run = Run(status="queued")
                session.add(run)
                session.flush()
            for position, requested in enumerate(requested_steps, start=1):
                session.add(
                    Step(
                        run_id=run.id,
                        position=position,
                        tool_name=requested["tool"],
                        payload=requested["payload"],
                        idempotency_key=f"{run.id}:step:{position}",
                    )
                )
            event(session, run.id, "run_created", {"steps": len(requested_steps)})
            session.commit()
            RUNS_CREATED.inc()
            return run

    def cancel(self, run_id: str) -> Run:
        with self.database.sessions() as session:
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if not run:
                raise KeyError(run_id)
            if (
                run.status in {"succeeded", "failed", "cancelled", "needs_review"}
                or run.cancellation_requested
            ):
                return run
            run.cancellation_requested = True
            steps = session.scalars(
                select(Step)
                .where(Step.run_id == run.id)
                .order_by(Step.position)
                .with_for_update()
            ).all()
            queued = [step for step in steps if step.status == "queued"]
            running = any(step.status == "running" for step in steps)
            for step in queued:
                step.status = "needs_review" if step.attempts else "cancelled"
            if running:
                run.status = "cancelling"
                event(
                    session,
                    run.id,
                    "cancellation_requested",
                    {"queued_steps_cancelled": len(queued)},
                )
            else:
                run.status = (
                    "needs_review"
                    if any(step.status == "needs_review" for step in steps)
                    else "cancelled"
                )
                event(
                    session,
                    run.id,
                    "run_" + run.status,
                    {
                        "before_execution": not any(step.attempts for step in steps),
                        "queued_steps_cancelled": len(queued),
                    },
                )
            session.commit()
            return run

    def snapshot(self, run_id: str) -> dict:
        with self.database.sessions() as session:
            # Keep the run, steps and event list from different commits from
            # being mixed into one response while a worker completes.
            run = session.scalar(
                select(Run).where(Run.id == run_id).with_for_update(read=True)
            )
            if not run:
                raise KeyError(run_id)
            steps = session.scalars(
                select(Step).where(Step.run_id == run.id).order_by(Step.position)
            ).all()
            events = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run.id)
                .order_by(RunEvent.id)
                .limit(1000)
            ).all()
            return {
                "id": run.id,
                "status": run.status,
                "cancellation_requested": run.cancellation_requested,
                "steps": [
                    {
                        "id": s.id,
                        "position": s.position,
                        "status": s.status,
                        "attempts": s.attempts,
                        "result": s.result,
                        "error": s.error,
                    }
                    for s in steps
                ],
                "events": [
                    {
                        "id": e.id,
                        "kind": e.kind,
                        "detail": e.detail,
                        "step_id": e.step_id,
                    }
                    for e in events
                ],
            }

    def events_since(self, run_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        with self.database.sessions() as session:
            if not session.get(Run, run_id):
                raise KeyError(run_id)
            events = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.id > after)
                .order_by(RunEvent.id)
                .limit(limit)
            ).all()
            return [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "detail": item.detail,
                    "step_id": item.step_id,
                }
                for item in events
            ]

    def reconcile(self, run_id: str) -> None:
        """Confirm a provider's persisted effect; never retry an unknown action."""
        with self.database.sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status != "needs_review":
                return
            step = session.scalar(
                select(Step).where(Step.run_id == run_id, Step.status == "needs_review")
            )
            step_id, key, payload = step.id, step.idempotency_key, step.payload
        provider = os.environ.get("TOOL_PROVIDER_URL", "http://localhost:8081").rstrip(
            "/"
        )
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.get(f"{provider}/effects/{key}")
            if response.status_code == 404:
                raise IdempotencyConflict(
                    "Provider has no confirmed effect; run remains needs_review"
                )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
                raise ValueError("Provider returned an invalid stored result")
            result = body["result"]
        expected = {"recorded": payload, "effect_key": key}
        if json.dumps(result, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise IdempotencyConflict(
                "Provider effect does not match the original request"
            )
        with self.database.sessions() as session:
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            step = session.scalar(
                select(Step).where(Step.id == step_id).with_for_update()
            )
            if run.status != "needs_review" or step.status != "needs_review":
                return
            step.status, step.result, step.error = "succeeded", result, None
            event(session, run_id, "tool_reconciled", {"idempotency_key": key}, step_id)
            if run.cancellation_requested:
                run.status = "cancelled"
            else:
                remaining = session.scalars(
                    select(Step).where(
                        Step.run_id == run_id,
                        Step.position > step.position,
                        Step.status == "cancelled",
                        Step.attempts == 0,
                    )
                ).all()
                for pending in remaining:
                    pending.status = "queued"
                run.status = "queued" if remaining else "succeeded"
            event(
                session,
                run_id,
                "run_resumed" if run.status == "queued" else "run_" + run.status,
                step_id=step_id,
            )
            session.commit()


@dataclass(frozen=True)
class Claim:
    run_id: str
    step_id: str
    key: str
    token: str
    payload: dict


class Worker:
    def __init__(
        self,
        database: Database,
        worker_id: str,
        lease_seconds: float = 30,
        max_attempts: int = 3,
        provider_url: str | None = None,
        after_tool_pause_seconds: float = 0,
        after_tool_marker: str | None = None,
        retry_base_seconds: float = 1,
        retry_cap_seconds: float = 30,
    ):
        if (
            lease_seconds < 0
            or max_attempts < 1
            or retry_base_seconds < 0
            or retry_cap_seconds < retry_base_seconds
        ):
            raise ValueError("invalid worker timing or attempt settings")
        self.database, self.worker_id = database, worker_id
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts
        self.retry_base_seconds, self.retry_cap_seconds = (
            retry_base_seconds,
            retry_cap_seconds,
        )
        self.tool = RecordTool(
            provider_url or os.environ.get("TOOL_PROVIDER_URL", "http://localhost:8081")
        )
        self.after_tool_pause_seconds, self.after_tool_marker = (
            after_tool_pause_seconds,
            after_tool_marker,
        )

    def _claim(self) -> Claim | None:
        with self.database.sessions() as session:
            predecessor = aliased(Step)
            unfinished = exists(
                select(predecessor.id).where(
                    predecessor.run_id == Step.run_id,
                    predecessor.position < Step.position,
                    predecessor.status != "succeeded",
                )
            )
            eligible = or_(
                and_(
                    Step.status == "queued", Step.available_at <= func.clock_timestamp()
                ),
                and_(
                    Step.status == "running",
                    Step.lease_expires_at <= func.clock_timestamp(),
                ),
            )
            # Every run-state mutation locks Run then Step. Holding the run lock
            # also serializes per-run event commits for resumable event cursors.
            run = session.scalar(
                select(Run)
                .where(
                    Run.status.in_(["queued", "running", "cancelling"]),
                    Run.id.in_(select(Step.run_id).where(eligible, ~unfinished)),
                )
                .order_by(Run.created_at, Run.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not run:
                return None
            step = session.scalar(
                select(Step)
                .where(Step.run_id == run.id, eligible, ~unfinished)
                .order_by(Step.position)
                .with_for_update()
                .limit(1)
            )
            if not step:
                return None
            reclaimed = step.status == "running"
            if run.cancellation_requested or step.attempts >= self.max_attempts:
                # A dead worker / lost response may already have committed an effect.
                uncertain = step.attempts > 0
                step.status = run.status = "needs_review" if uncertain else "cancelled"
                step.error = (
                    "attempt budget exhausted; remote outcome unknown"
                    if not run.cancellation_requested
                    else "cancelled with remote outcome unknown"
                    if uncertain
                    else None
                )
                self._clear(step)
                self._cancel_pending(session, run.id)
                event(
                    session,
                    run.id,
                    "run_" + run.status,
                    {"reason": step.error},
                    step.id,
                )
                session.commit()
                return None
            step.status, run.status = "running", "running"
            step.attempts += 1
            step.lease_owner, step.claim_token = self.worker_id, str(uuid4())
            current = session.scalar(select(func.clock_timestamp()))
            step.lease_expires_at = current + timedelta(seconds=self.lease_seconds)
            event(
                session,
                run.id,
                "step_reclaimed" if reclaimed else "step_claimed",
                {
                    "worker": self.worker_id,
                    "attempt": step.attempts,
                    "claim_token": step.claim_token,
                },
                step.id,
            )
            claim = Claim(
                run.id, step.id, step.idempotency_key, step.claim_token, step.payload
            )
            session.commit()
            STEPS_CLAIMED.labels(outcome="reclaimed" if reclaimed else "claimed").inc()
            return claim

    def _renew_lease(self, claim: Claim) -> bool:
        with self.database.sessions() as session:
            changed = session.execute(
                update(Step)
                .where(
                    Step.id == claim.step_id,
                    Step.claim_token == claim.token,
                    Step.status == "running",
                    Step.lease_expires_at > func.clock_timestamp(),
                )
                .values(
                    lease_expires_at=func.clock_timestamp()
                    + timedelta(seconds=self.lease_seconds)
                )
            )
            session.commit()
            return changed.rowcount == 1

    def _heartbeat(self, claim: Claim, stop: threading.Event) -> None:
        while not stop.wait(max(0.05, self.lease_seconds / 3)):
            try:
                if not self._renew_lease(claim):
                    return
            except OperationalError:
                logging.getLogger(__name__).warning(
                    "heartbeat database unavailable", extra={"step_id": claim.step_id}
                )
                # Never resurrect an expired lease when the connection recovers.

    def _locked(self, session, claim: Claim):
        run = session.scalar(
            select(Run).where(Run.id == claim.run_id).with_for_update()
        )
        step = session.scalar(
            select(Step).where(Step.id == claim.step_id).with_for_update()
        )
        if (
            not run
            or not step
            or step.status != "running"
            or step.claim_token != claim.token
            or step.lease_expires_at <= session.scalar(select(func.clock_timestamp()))
        ):
            return None
        return run, step

    @staticmethod
    def _clear(step):
        step.lease_owner = step.claim_token = step.lease_expires_at = None

    @staticmethod
    def _cancel_pending(session, run_id):
        for pending in session.scalars(
            select(Step).where(Step.run_id == run_id, Step.status == "queued")
        ):
            pending.status = "cancelled"

    def _finish(self, claim: Claim, result: dict, replayed: bool) -> dict:
        with self.database.sessions() as session:
            locked = self._locked(session, claim)
            if not locked:
                return {"step_id": claim.step_id, "lease_lost": True}
            run, step = locked
            step.status, step.result, step.error = "succeeded", result, None
            self._clear(step)
            session.flush()
            event(
                session,
                run.id,
                "tool_replayed" if replayed else "tool_effect_created",
                {"idempotency_key": claim.key},
                step.id,
            )
            if run.cancellation_requested:
                self._cancel_pending(session, run.id)
                run.status = "cancelled"
                event(session, run.id, "run_cancelled", {"after_effect": True}, step.id)
            elif session.scalar(
                select(Step.id)
                .where(Step.run_id == run.id, Step.status != "succeeded")
                .limit(1)
            ):
                run.status = "queued"
                event(
                    session,
                    run.id,
                    "step_succeeded",
                    {"position": step.position},
                    step.id,
                )
            else:
                run.status = "succeeded"
                event(session, run.id, "run_succeeded", step_id=step.id)
            session.commit()
            return {"run_id": run.id, "step_id": step.id, "replayed": replayed}

    def _error(self, claim: Claim, error: str, retryable: bool) -> dict:
        with self.database.sessions() as session:
            locked = self._locked(session, claim)
            if not locked:
                return {"step_id": claim.step_id, "lease_lost": True}
            run, step = locked
            step.error = error
            self._clear(step)
            if (
                run.cancellation_requested
                or step.attempts >= self.max_attempts
                or not retryable
            ):
                status = (
                    "needs_review"
                    if retryable
                    else "cancelled"
                    if run.cancellation_requested
                    else "failed"
                )
                run.status = step.status = status
                self._cancel_pending(session, run.id)
                event(
                    session,
                    run.id,
                    "run_" + status,
                    {"reason": error, "remote_outcome_unknown": retryable},
                    step.id,
                )
                session.commit()
                return {"run_id": run.id, "step_id": step.id, status: True}
            delay = min(
                self.retry_cap_seconds,
                self.retry_base_seconds * 2 ** min(step.attempts - 1, 20),
            )
            delay *= random.uniform(0.5, 1.0)
            step.available_at = session.scalar(
                select(func.clock_timestamp())
            ) + timedelta(seconds=delay)
            step.status = run.status = "queued"
            event(
                session,
                run.id,
                "step_retry_scheduled",
                {"attempt": step.attempts, "reason": error, "delay_seconds": delay},
                step.id,
            )
            session.commit()
            return {"run_id": run.id, "step_id": step.id, "retry_scheduled": True}

    def run_once(self, crash_after_tool: bool = False) -> dict | None:
        claim = self._claim()
        if not claim:
            return None
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(claim, stop), daemon=True
        )
        heartbeat.start()
        try:
            try:
                with STEP_DURATION.time():
                    result, replayed = self.tool.execute(claim.key, claim.payload)
            except ToolExecutionError as error:
                return self._error(claim, str(error), retryable=False)
            except SimulatedWorkerCrash:
                raise
            except Exception as error:
                return self._error(claim, str(error), retryable=True)
            if crash_after_tool:
                raise SimulatedWorkerCrash(
                    "worker ended after the external effect committed"
                )
            if self.after_tool_marker:
                Path(self.after_tool_marker).touch()
            if self.after_tool_pause_seconds:
                time.sleep(self.after_tool_pause_seconds)
            return self._finish(claim, result, replayed)
        finally:
            stop.set()
            heartbeat.join(timeout=1)
