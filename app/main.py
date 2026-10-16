import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.config import (
    MOCK_URL,
    REQUEST_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_DELAY_SECONDS,
    CB_FAILURE_THRESHOLD,
    CB_RECOVERY_SECONDS,
    ENABLE_TEST_CONTROLS,
)
from app.metrics import CloudWatchPublisher, RequestMetricsMiddleware, metrics_from_env
from app.models import CallEvent, PipelineRequest, TEST_SCENARIOS
from app.pipeline import MockPipeline
from app.resilience import ResilientPipeline, CircuitOpenError
from app.dlq import DeadLetterQueue, DynamoDBDeadLetterQueue, dlq_from_env

STATIC_DIR = Path(__file__).parent / "static"
UI_HTML = STATIC_DIR / "index.html"


def describe_exception(exc: Exception) -> str:
    """Produce a short, useful failure reason.

    Must stay useful for exceptions with an empty ``str()`` (httpx timeouts
    raise ``ReadTimeout("")``) and must not leak multi-line internal detail: for
    ``httpx.HTTPStatusError`` we report only the status, not the internal URL.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return f"HTTPStatusError: {response.status_code} {response.reason_phrase}".strip()
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    # Keep only the first line so stack-trace-ish help text is never surfaced.
    return f"{type(exc).__name__}: {text.splitlines()[0].strip()}"


def create_app(
    *,
    dlq: DeadLetterQueue | DynamoDBDeadLetterQueue | None = None,
    resilient: ResilientPipeline | None = None,
    mock_client: httpx.AsyncClient | None = None,
    enable_test_controls: bool | None = None,
    mock_url: str | None = None,
    max_attempts: int | None = None,
    metrics: CloudWatchPublisher | None = None,
) -> FastAPI:
    """Build the API application.

    Production uses no arguments: the HTTP client and resilient pipeline are
    created during lifespan startup. Tests inject ``dlq``/``resilient`` (and
    optionally ``mock_client``) so behaviour is fully deterministic and no
    real network calls are made.

    ``metrics`` overrides the CloudWatch publisher (tests inject one with a fake
    client). When omitted it is built from configuration and is ``None`` unless
    ``CLOUDWATCH_METRICS_ENABLED`` is true; with no publisher the request
    instrumentation is not installed at all, so the default path is unchanged.
    Enabling metrics without ``ECS_CLUSTER_NAME``/``ECS_SERVICE_NAME`` raises
    :class:`~app.metrics.MissingMetricDimensionsError` here, before the app is
    built, so a misconfigured task fails to start instead of running with
    autoscaling that can never trigger.
    """
    settings_url = mock_url or MOCK_URL
    attempts = max_attempts or MAX_ATTEMPTS
    test_controls = (
        ENABLE_TEST_CONTROLS if enable_test_controls is None else enable_test_controls
    )
    metrics_publisher = metrics if metrics is not None else metrics_from_env()

    # When only a mock client is injected, wire the real adapter around it now.
    if resilient is None and mock_client is not None:
        resilient = ResilientPipeline(MockPipeline(mock_client), max_attempts=attempts)

    created_client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal created_client
        if app.state.resilient is None:
            created_client = httpx.AsyncClient(
                base_url=settings_url,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            app.state.mock_client = created_client
            app.state.resilient = ResilientPipeline(
                MockPipeline(created_client),
                max_attempts=attempts,
            )
        if app.state.metrics is not None:
            await app.state.metrics.start()
        yield
        if app.state.metrics is not None:
            await app.state.metrics.stop()
        if created_client is not None:
            await created_client.aclose()

    app = FastAPI(
        title="Collections Inference Service",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Application state (replaces module globals, keeps handlers testable).
    # ``dlq_from_env`` selects SQLite (default) or the shared DynamoDB backend,
    # and fails fast if DynamoDB is configured without a table name.
    app.state.dlq = dlq or dlq_from_env()
    app.state.resilient = resilient
    app.state.mock_client = mock_client
    app.state.enable_test_controls = test_controls
    app.state.max_attempts = attempts
    app.state.metrics = metrics_publisher

    async def dlq_call(method: str, *args):
        """Run a (possibly network-backed) DLQ method off the event loop.

        The DLQ interface is synchronous so it can be used and tested directly.
        DynamoDB calls would otherwise block the event loop, so every handler
        goes through this helper.
        """
        return await asyncio.to_thread(getattr(app.state.dlq, method), *args)

    # Request instrumentation is only installed when metrics are enabled, so a
    # disabled deployment pays no middleware cost and behaves exactly as before.
    if metrics_publisher is not None:
        app.add_middleware(RequestMetricsMiddleware, metrics=metrics_publisher.collector)

    def _resolve_scenario(request: Request) -> str | None:
        """Read the test-only scenario header when controls are enabled.

        Returns ``None`` (normal behaviour) whenever controls are disabled, so
        a stray header can never influence a production request.
        """
        if not app.state.enable_test_controls:
            return None
        scenario = request.headers.get("x-test-scenario")
        if scenario is None:
            return None
        if scenario not in TEST_SCENARIOS:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown_scenario", "allowed": list(TEST_SCENARIOS)},
            )
        return scenario

    def _require_test_controls() -> None:
        if not app.state.enable_test_controls:
            raise HTTPException(status_code=403, detail="test_controls_disabled")

    @app.get("/health")
    async def health():
        breaker_pipeline = app.state.resilient
        return {
            "status": "ok",
            "circuit_state": (
                breaker_pipeline.breaker.state if breaker_pipeline else "starting"
            ),
        }

    @app.get("/dependencies")
    async def dependencies():
        """Aggregate health of this API and the mock dependency (used by /ui)."""
        breaker_pipeline = app.state.resilient
        payload = {
            "api": {
                "status": "ok",
                "circuit_state": (
                    breaker_pipeline.breaker.state if breaker_pipeline else "starting"
                ),
            },
            "mock": {"reachable": None, "status": None},
        }
        client = app.state.mock_client
        if client is not None:
            try:
                response = await client.get("/health")
                payload["mock"] = {
                    "reachable": True,
                    "status_code": response.status_code,
                    "status": response.json().get("status"),
                }
            except Exception as exc:  # pragma: no cover - exercised via tests
                payload["mock"] = {"reachable": False, "error": describe_exception(exc)}
        return payload

    @app.post("/calls")
    async def process_call(event: CallEvent, request: Request):
        scenario = _resolve_scenario(request)
        event_id = str(event.event_id)
        pipeline_request = PipelineRequest(
            event_id=event_id,
            audio_ref="synthetic",
            scenario=scenario,
        )

        try:
            result, retries = await app.state.resilient.process(pipeline_request)
            return {
                "event_id": event_id,
                "outcome": "connected",
                "retry_count": retries,
                "pipeline": result,
            }

        except CircuitOpenError:
            await dlq_call(
                "put",
                event_id,
                event.model_dump(mode="json"),
                "circuit_open",
                0,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "event_id": event_id,
                    "status": "dead_lettered",
                    "reason": "circuit_open",
                },
            )

        except Exception as exc:
            reason = describe_exception(exc)
            # The resilience layer records the *actual* number of retries on the
            # raised exception, so this is accurate even when a non-retryable
            # error short-circuits before exhausting attempts.
            await dlq_call(
                "put",
                event_id,
                event.model_dump(mode="json"),
                reason,
                max(0, int(getattr(exc, "retries", 0))),
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "event_id": event_id,
                    "status": "dead_lettered",
                    "reason": reason,
                },
            )

    @app.get("/dlq")
    async def list_dlq():
        return {"items": await dlq_call("list")}

    @app.get("/dlq/{event_id}")
    async def get_dlq_event(event_id: str):
        item = await dlq_call("get", event_id)
        if not item:
            raise HTTPException(status_code=404, detail="event_not_found")
        return item

    @app.post("/dlq/{event_id}/replay")
    async def replay_event(event_id: str, request: Request):
        item = await dlq_call("get", event_id)
        if not item:
            raise HTTPException(status_code=404, detail="event_not_found")
        if item["status"] == "processing":
            raise HTTPException(status_code=409, detail="event_already_processing")

        scenario = _resolve_scenario(request)

        # Atomically claim the event so concurrent replays cannot both proceed.
        if not await dlq_call("claim", event_id):
            raise HTTPException(status_code=409, detail="event_already_processing")

        try:
            result, retries = await app.state.resilient.process(
                PipelineRequest(
                    event_id=event_id,
                    audio_ref="synthetic",
                    scenario=scenario,
                )
            )
            await dlq_call("set_status", event_id, "completed")
            return {
                "event_id": event_id,
                "status": "completed",
                "retry_count": retries,
                "pipeline": result,
            }

        except Exception as exc:
            reason = (
                "circuit_open"
                if isinstance(exc, CircuitOpenError)
                else describe_exception(exc)
            )
            await dlq_call("set_status", event_id, "pending", reason)
            raise HTTPException(
                status_code=503,
                detail={
                    "event_id": event_id,
                    "status": "pending",
                    "reason": reason,
                },
            )

    # ---- Test-only controls (gated by ENABLE_TEST_CONTROLS) ----------------

    @app.get("/test/scenarios")
    async def test_scenarios():
        """Report whether test controls are enabled and which scenarios exist.

        Safe to call regardless of mode; the UI uses it to enable/disable the
        failure-simulation controls.
        """
        return {
            "enabled": app.state.enable_test_controls,
            "scenarios": list(TEST_SCENARIOS),
            "max_attempts": app.state.max_attempts,
            "max_delay_seconds": MAX_DELAY_SECONDS,
            "circuit_failure_threshold": app.state.resilient.breaker.failure_threshold
            if app.state.resilient
            else CB_FAILURE_THRESHOLD,
            "circuit_recovery_seconds": app.state.resilient.breaker.recovery_seconds
            if app.state.resilient
            else CB_RECOVERY_SECONDS,
        }

    @app.post("/test/circuit/reset")
    async def reset_circuit():
        _require_test_controls()
        app.state.resilient.breaker.reset()
        return {"circuit_state": app.state.resilient.breaker.state}

    # ---- Visual E2E testing dashboard --------------------------------------

    @app.get("/ui", include_in_schema=False)
    async def ui():
        return FileResponse(UI_HTML)

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/health")

    return app


app = create_app()
