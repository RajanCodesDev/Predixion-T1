"""Shared pytest fixtures.

The project's config module reads environment variables at import time, so an
isolated database path and MOCK_URL are set *before* any application module is
imported. This guarantees the test suite never touches ``/data/dlq.sqlite3`` or
any real network endpoint.
"""
import contextlib
import os
import tempfile
import uuid

os.environ.setdefault(
    "DATABASE_PATH",
    os.path.join(tempfile.mkdtemp(prefix="collections-tests-"), "dlq.sqlite3"),
)
os.environ.setdefault("MOCK_URL", "http://mock.invalid:8001")
os.environ.setdefault("ENABLE_TEST_CONTROLS", "false")
# Keep the suite hermetic: CloudWatch publication must be opt-in per test.
os.environ.setdefault("CLOUDWATCH_METRICS_ENABLED", "false")

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.dlq import DeadLetterQueue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import PipelineRequest  # noqa: E402
from app.pipeline import MockPipeline  # noqa: E402
from app.resilience import CircuitBreaker, ResilientPipeline  # noqa: E402
from mock.main import create_app as create_mock_app  # noqa: E402
from tests.helpers import (  # noqa: E402
    AlwaysFail,
    FakeTime,
    ScriptedAdapter,
    event_uuid,
    is_uuid,
)


# --------------------------------------------------------------------------- #
# Deterministic adapters (re-exported from tests.helpers)
# --------------------------------------------------------------------------- #
__all__ = ["ScriptedAdapter", "AlwaysFail", "FakeTime", "event_uuid"]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "dlq.sqlite3")


@pytest.fixture
def dlq(temp_db_path):
    return DeadLetterQueue(temp_db_path)


@pytest.fixture
def make_adapter():
    def _make(outcomes=()):
        return ScriptedAdapter(outcomes)

    return _make


@pytest.fixture
def failing_adapter():
    return AlwaysFailAdapter()


@pytest.fixture
def busy_adapter():
    """Adapter that raises a non-empty exception (for reason assertions)."""
    return AlwaysFailAdapter(RuntimeError("downstream 503"))


@pytest.fixture
def clock(monkeypatch):
    """Replace the clock used by the circuit breaker with a controllable one."""
    import app.resilience as resilience

    fake = FakeTime()
    monkeypatch.setattr(resilience, "time", fake)
    return fake


@pytest.fixture
def fake_asyncio(monkeypatch):
    """Replace asyncio.sleep inside the resilience module with a recorder."""

    class FakeAsyncio:
        def __init__(self):
            self.sleeps = []

        async def sleep(self, delay):
            self.sleeps.append(delay)

    fake = FakeAsyncio()
    import app.resilience as resilience

    monkeypatch.setattr(resilience, "asyncio", fake)
    return fake


@pytest.fixture
def app_factory(temp_db_path):
    """Build isolated API apps. Defaults to a temp DB and test controls off."""
    created = []

    def _make(
        *,
        dlq=None,
        resilient=None,
        mock_client=None,
        test_controls=False,
        max_attempts=None,
        metrics=None,
    ):
        app = create_app(
            dlq=dlq if dlq is not None else DeadLetterQueue(temp_db_path),
            resilient=resilient,
            mock_client=mock_client,
            enable_test_controls=test_controls,
            max_attempts=max_attempts,
            metrics=metrics,
        )
        created.append(app)
        return app

    return _make


@pytest.fixture
def client_for():
    """Async context manager yielding an in-process API client.

    Runs the app's lifespan so startup/shutdown hooks execute where relevant.
    """

    @contextlib.asynccontextmanager
    async def _make(app):
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
                yield client

    return _make


@pytest.fixture
def mock_app():
    """Mock dependency with deterministic test controls enabled."""
    return create_mock_app(test_controls=True)


@pytest.fixture
async def mock_client(mock_app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=mock_app), base_url="http://mock:8001"
    ) as client:
        yield client


@pytest.fixture
def sample_event():
    def _event(event_id="evt-1", **overrides):
        if event_id is None:
            event_id = str(uuid.uuid4())
        elif not is_uuid(event_id):
            event_id = event_uuid(event_id)
        payload = {
            "event_id": event_id,
            "campaign_id": "CAMP-2026-10-A",
            "borrower_bucket": "0-30 DPD",
            "product_type": "bnpl",
            "timestamp": "2026-09-01T09:03:12Z",
            "call_duration_s": 0,
            "outcome": "connected",
            "retry_count": 0,
        }
        payload.update(overrides)
        return payload

    return _event


@pytest.fixture
def success_resilient(make_adapter):
    adapter = make_adapter()
    return ResilientPipeline(adapter, CircuitBreaker(failure_threshold=99), max_attempts=3)


@pytest.fixture(autouse=True)
def _no_real_backoff(monkeypatch):
    """Keep the suite fast: retries never sleep on real time by default.

    Tests that assert on backoff explicitly override these values.
    """
    import app.resilience as resilience

    monkeypatch.setattr(resilience, "BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(resilience, "MAX_DELAY_SECONDS", 0.0)
