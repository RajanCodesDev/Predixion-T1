"""Mock STT -> LLM -> TTS dependency (challenge Appendix B).

Contract::

    POST /pipeline/process
    request  {"event_id": "...", "audio_ref": "synthetic"}
    success  HTTP 200 {"status": "ok", "transcript": "synthetic transcript",
                       "response_audio_ref": "synthetic", "latency_ms": <sampled>}
    failure  HTTP 503 {"status": "error", "reason": "downstream_timeout"}

Failure injection is independent per request: ``random() < 0.20``. On the
failure path no latency is simulated; the sampled latency applies to successful
responses only.
"""
import asyncio
import os
import random

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Challenge-suggested lognormal parameters (milliseconds). The median is
# exp(mu) ~= 300 ms. The challenge suggests sigma=0.35 and asks us to tune the
# spread so p90 ~= 700 ms and p99 ~= 1200 ms; measurements (scripts/latency_sample.py)
# showed sigma=0.62 fits those tail targets far better while keeping p50 ~= 300 ms.
LATENCY_MU = 5.7
LATENCY_SIGMA = 0.62
FAILURE_RATE = 0.20


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


TEST_SCENARIOS = (
    "always_succeed",
    "fail_once_then_succeed",
    "always_fail",
    "slow",
)


class Request(BaseModel):
    event_id: str
    audio_ref: str = "synthetic"
    # Test-only fields, honored only when test controls are enabled.
    scenario: str | None = None
    delay_ms: int | None = None


def create_app(test_controls: bool | None = None) -> FastAPI:
    """Build the mock STT/LLM/TTS dependency.

    Default behaviour (``test_controls`` false) matches the challenge spec: an
    independent 20% HTTP 503 failure probability per request and lognormal
    latency with median ~300 ms. When ``test_controls`` is true, a request may
    select a deterministic scenario instead.
    """
    if test_controls is None:
        test_controls = _as_bool(os.getenv("ENABLE_TEST_CONTROLS", "false"))

    app = FastAPI(title="Mock STT-LLM-TTS Dependency")

    # Per-event attempt counter for the "fail_once_then_succeed" scenario.
    # Keyed by event_id so concurrent unrelated events never interfere.
    attempts: dict[str, int] = {}
    lock = asyncio.Lock()

    def _failure_payload(reason: str) -> JSONResponse:
        return JSONResponse(status_code=503, content={"status": "error", "reason": reason})

    @app.get("/health")
    async def health():
        return {"status": "ok", "test_controls": test_controls}

    @app.post("/pipeline/process")
    async def process(request: Request):
        scenario = request.scenario if test_controls else None

        if scenario is None:
            # Challenge spec: independent 20% failure probability per request.
            if random.random() < FAILURE_RATE:
                return _failure_payload("downstream_timeout")
            # Challenge-specified lognormal latency in milliseconds.
            latency_ms = max(1, int(random.lognormvariate(LATENCY_MU, LATENCY_SIGMA)))
        elif scenario == "always_succeed":
            latency_ms = 0
        elif scenario == "always_fail":
            return _failure_payload("downstream_timeout")
        elif scenario == "fail_once_then_succeed":
            async with lock:
                count = attempts.get(request.event_id, 0) + 1
                attempts[request.event_id] = count
            if count == 1:
                return _failure_payload("downstream_timeout")
            latency_ms = 0
        elif scenario == "slow":
            # Sleep longer than a short client timeout so the caller observes a
            # timeout. delay_ms is capped to keep accidental long sleeps small.
            latency_ms = min(request.delay_ms or 1000, 5000)
        else:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "reason": "unknown_scenario"},
            )

        # Simulate the successful-path latency, then report the sampled value
        # (not the wall-clock elapsed time) so the response is deterministic.
        if latency_ms > 0:
            await asyncio.sleep(latency_ms / 1000)
        return {
            "status": "ok",
            "transcript": "synthetic transcript",
            "response_audio_ref": "synthetic",
            "latency_ms": latency_ms,
        }

    return app


app = create_app()
