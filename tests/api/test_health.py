"""Health, dependency and test-control endpoints (`api`)."""
import pytest

from app.pipeline import MockPipeline
from app.resilience import CircuitBreaker, ResilientPipeline
from tests.helpers import AlwaysFail, ScriptedAdapter

pytestmark = pytest.mark.api


def _success_pipeline():
    return ResilientPipeline(ScriptedAdapter(), CircuitBreaker(), max_attempts=3)


async def test_health_ok(app_factory, client_for):
    app = app_factory(resilient=_success_pipeline())

    async with client_for(app) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "circuit_state": "closed"}


async def test_health_reports_open_circuit(app_factory, client_for, sample_event):
    resilient = ResilientPipeline(
        AlwaysFail(), CircuitBreaker(failure_threshold=1), max_attempts=1
    )
    app = app_factory(resilient=resilient)

    async with client_for(app) as client:
        failed = await client.post("/calls", json=sample_event("e1"))
        health = await client.get("/health")

    assert failed.status_code == 503
    assert health.json()["circuit_state"] == "open"


async def test_dependencies_reports_mock_reachable(app_factory, client_for, mock_client):
    resilient = ResilientPipeline(MockPipeline(mock_client), CircuitBreaker(), max_attempts=3)
    app = app_factory(resilient=resilient, mock_client=mock_client)

    async with client_for(app) as client:
        response = await client.get("/dependencies")

    body = response.json()
    assert response.status_code == 200
    assert body["api"]["status"] == "ok"
    assert body["api"]["circuit_state"] == "closed"
    assert body["mock"]["reachable"] is True
    assert body["mock"]["status"] == "ok"


async def test_dependencies_without_mock_client(app_factory, client_for):
    app = app_factory(resilient=_success_pipeline())

    async with client_for(app) as client:
        body = (await client.get("/dependencies")).json()

    assert body["mock"]["reachable"] is None


async def test_test_scenarios_disabled_by_default(app_factory, client_for):
    app = app_factory(resilient=_success_pipeline())

    async with client_for(app) as client:
        body = (await client.get("/test/scenarios")).json()

    assert body["enabled"] is False
    assert "always_fail" in body["scenarios"]
    assert body["max_attempts"] == 3


async def test_test_scenarios_enabled(app_factory, client_for):
    app = app_factory(resilient=_success_pipeline(), test_controls=True)

    async with client_for(app) as client:
        body = (await client.get("/test/scenarios")).json()

    assert body["enabled"] is True


async def test_circuit_reset_requires_test_controls(app_factory, client_for):
    app = app_factory(resilient=_success_pipeline(), test_controls=False)

    async with client_for(app) as client:
        response = await client.post("/test/circuit/reset")

    assert response.status_code == 403
    assert response.json()["detail"] == "test_controls_disabled"


async def test_circuit_reset_closes_breaker(app_factory, client_for, sample_event):
    resilient = ResilientPipeline(
        AlwaysFail(), CircuitBreaker(failure_threshold=1), max_attempts=1
    )
    app = app_factory(resilient=resilient, test_controls=True)

    async with client_for(app) as client:
        await client.post("/calls", json=sample_event("e1"))
        assert (await client.get("/health")).json()["circuit_state"] == "open"

        reset = await client.post("/test/circuit/reset")
        health = await client.get("/health")

    assert reset.status_code == 200
    assert reset.json()["circuit_state"] == "closed"
    assert health.json()["circuit_state"] == "closed"
