from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

RUNS_CREATED = Counter("durable_runs_created_total", "Runs accepted by the API")
STEPS_CLAIMED = Counter(
    "durable_steps_claimed_total", "Steps claimed by workers", ["outcome"]
)
TOOL_CALLS = Counter(
    "durable_tool_calls_total", "Calls to the remote tool provider", ["outcome"]
)
STEP_DURATION = Histogram(
    "durable_step_execution_seconds", "Time spent executing a step"
)


def render_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
