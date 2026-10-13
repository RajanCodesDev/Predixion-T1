"""DLQ endpoints and replay behaviour (`api`)."""
import asyncio

import pytest

from app.resilience import CircuitBreaker, ResilientPipeline
from tests.helpers import AlwaysFail, ScriptedAdapter

pytestmark = pytest.mark.api

PAYLOAD = {
    "event_id": "e1",
    "campaign_id": "CAMP-2026-10-A",
    "borrower_bucket": "0-30 DPD",
    "product_type": "bnpl",
    "call_duration_s": 0,
    "outcome": "connected",
    "retry_count": 0,
}


def _success_pipeline(max_attempts=3):
    return ResilientPipeline(ScriptedAdapter(), CircuitBreaker(), max_attempts=max_attempts)


async def test_dlq_list_empty(app_factory, client_for):
    app = app_factory(resilient=_success_pipeline())

    async with client_for(app) as client:
        response = await client.get("/dlq")

    assert response.status_code == 200
    assert response.json() == {"items": []}


async def test_get_existing_event(app_factory, client_for, dlq):
    dlq.put("e1", PAYLOAD, "boom", 2)
    app = app_factory(dlq=dlq, resilient=_success_pipeline())

    async with client_for(app) as client:
        response = await client.get("/dlq/e1")

    assert response.status_code == 200
    assert response.json()["event_id"] == "e1"
    assert response.json()["failure_reason"] == "boom"


async def test_get_missing_event_returns_404(app_factory, client_for):
    app = app_factory(resilient=_success_pipeline())

    async with client_for(app) as client:
        response = await client.get("/dlq/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "event_not_found"


async def test_replay_missing_event_returns_404(app_factory, client_for):
    app = app_factory(resilient=_success_pipeline())

    async with client_for(app) as client:
        response = await client.post("/dlq/missing/replay")

    assert response.status_code == 404


async def test_replay_conflict_when_processing(app_factory, client_for, dlq):
    dlq.put("e1", PAYLOAD, "boom", 1)
    dlq.set_status("e1", "processing")
    app = app_factory(dlq=dlq, resilient=_success_pipeline())

    async with client_for(app) as client:
        response = await client.post("/dlq/e1/replay")

    assert response.status_code == 409
    assert response.json()["detail"] == "event_already_processing"


async def test_replay_success_marks_completed(app_factory, client_for, dlq):
    dlq.put("e1", PAYLOAD, "boom", 2)
    app = app_factory(dlq=dlq, resilient=_success_pipeline())

    async with client_for(app) as client:
        response = await client.post("/dlq/e1/replay")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["retry_count"] == 0
    assert body["pipeline"] == {"status": "ok"}
    assert dlq.get("e1")["status"] == "completed"


async def test_replay_failure_returns_event_to_pending(app_factory, client_for, dlq):
    dlq.put("e1", PAYLOAD, "original reason", 1)
    resilient = ResilientPipeline(
        AlwaysFail(RuntimeError("still down")),
        CircuitBreaker(failure_threshold=99),
        max_attempts=1,
    )
    app = app_factory(dlq=dlq, resilient=resilient, max_attempts=1)

    async with client_for(app) as client:
        response = await client.post("/dlq/e1/replay")

    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "pending"
    assert "still down" in response.json()["detail"]["reason"]

    item = dlq.get("e1")
    assert item["status"] == "pending"
    assert "still down" in item["failure_reason"]


async def test_replay_circuit_open_returns_to_pending(app_factory, client_for, dlq):
    dlq.put("e1", PAYLOAD, "original", 1)
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.state = "open"
    breaker.opened_at = 1e9  # far future -> not yet recoverable
    resilient = ResilientPipeline(ScriptedAdapter(), breaker, max_attempts=1)
    app = app_factory(dlq=dlq, resilient=resilient)

    async with client_for(app) as client:
        response = await client.post("/dlq/e1/replay")

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "circuit_open"
    assert dlq.get("e1")["status"] == "pending"


async def test_replay_forwards_scenario_when_enabled(app_factory, client_for, dlq):
    dlq.put("e1", PAYLOAD, "boom", 1)
    adapter = ScriptedAdapter()
    app = app_factory(
        dlq=dlq,
        resilient=ResilientPipeline(adapter, CircuitBreaker(), max_attempts=3),
        test_controls=True,
    )

    async with client_for(app) as client:
        response = await client.post(
            "/dlq/e1/replay", headers={"X-Test-Scenario": "always_succeed"}
        )

    assert response.status_code == 200
    assert adapter.requests[-1].scenario == "always_succeed"


async def test_concurrent_replay_conflicts(app_factory, client_for, dlq):
    """While one replay is in flight, a second replay of the same id is 409."""
    dlq.put("e1", PAYLOAD, "boom", 1)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingAdapter:
        async def process(self, request):
            started.set()
            await release.wait()
            return {"status": "ok"}

    app = app_factory(
        dlq=dlq,
        resilient=ResilientPipeline(BlockingAdapter(), CircuitBreaker(), max_attempts=1),
    )

    async with client_for(app) as client:
        first = asyncio.create_task(client.post("/dlq/e1/replay"))
        await started.wait()
        second = await client.post("/dlq/e1/replay")
        release.set()
        first_response = await first

    assert first_response.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "event_already_processing"
    assert dlq.get("e1")["status"] == "completed"

