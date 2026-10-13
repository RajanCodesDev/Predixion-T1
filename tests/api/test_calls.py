"""POST /calls behaviour (`api`)."""
import pytest

from app.resilience import CircuitBreaker, ResilientPipeline
from tests.helpers import AlwaysFail, ScriptedAdapter, event_uuid

pytestmark = pytest.mark.api


async def test_success_response_schema(app_factory, client_for, make_adapter, sample_event):
    adapter = make_adapter()
    resilient = ResilientPipeline(adapter, CircuitBreaker(), max_attempts=3)
    app = app_factory(resilient=resilient)

    async with client_for(app) as client:
        response = await client.post("/calls", json=sample_event("evt-1"))

    body = response.json()
    assert response.status_code == 200
    assert body["event_id"] == event_uuid("evt-1")
    assert body["outcome"] == "connected"
    assert body["retry_count"] == 0
    assert body["pipeline"] == {"status": "ok"}


async def test_client_supplied_uuid_roundtrips(app_factory, client_for, make_adapter):
    app = app_factory(
        resilient=ResilientPipeline(make_adapter(), CircuitBreaker(), max_attempts=3)
    )
    payload = {
        "event_id": event_uuid("roundtrip"),
        "campaign_id": "CAMP-2026-10-A",
        "borrower_bucket": "0-30 DPD",
        "product_type": "bnpl",
        "timestamp": "2026-09-01T09:03:12Z",
        "outcome": "connected",
    }

    async with client_for(app) as client:
        response = await client.post("/calls", json=payload)

    assert response.status_code == 200
    assert response.json()["event_id"] == event_uuid("roundtrip")


async def test_missing_event_id_is_rejected(app_factory, client_for, make_adapter):
    """Appendix A marks event_id as required; it is no longer auto-generated."""
    app = app_factory(
        resilient=ResilientPipeline(make_adapter(), CircuitBreaker(), max_attempts=3)
    )
    payload = {
        "campaign_id": "CAMP-2026-10-A",
        "borrower_bucket": "0-30 DPD",
        "timestamp": "2026-09-01T09:03:12Z",
        "outcome": "connected",
    }

    async with client_for(app) as client:
        response = await client.post("/calls", json=payload)

    assert response.status_code == 422


async def test_retry_count_reported(app_factory, client_for, make_adapter, sample_event):
    adapter = make_adapter([RuntimeError("503"), {"status": "ok"}])
    resilient = ResilientPipeline(adapter, CircuitBreaker(), max_attempts=3)
    app = app_factory(resilient=resilient)

    async with client_for(app) as client:
        response = await client.post("/calls", json=sample_event("evt-retry"))

    assert response.status_code == 200
    assert response.json()["retry_count"] == 1
    assert adapter.calls == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"event_id": None},                      # required
        {"event_id": "not-a-uuid"},              # UUID format
        {"campaign_id": None},                   # required
        {"borrower_bucket": "not-a-bucket"},     # enum
        {"outcome": "unknown"},                  # enum
        {"timestamp": None},                     # required
        {"timestamp": "not-a-timestamp"},        # date-time
        {"product_type": "car"},                 # enum (optional but validated)
        {"call_duration_s": -1},                 # numeric minimum
        {"retry_count": -1},                     # numeric minimum
    ],
)
async def test_request_validation_rejects_bad_input(
    app_factory, client_for, make_adapter, sample_event, patch
):
    app = app_factory(
        resilient=ResilientPipeline(make_adapter(), CircuitBreaker(), max_attempts=3)
    )
    payload = sample_event("evt-bad")
    for key, value in patch.items():
        if value is None:
            payload.pop(key)
        else:
            payload[key] = value

    async with client_for(app) as client:
        response = await client.post("/calls", json=payload)

    assert response.status_code == 422


async def test_optional_fields_may_be_omitted(app_factory, client_for, make_adapter):
    """product_type / call_duration_s / retry_count are optional in Appendix A."""
    app = app_factory(
        resilient=ResilientPipeline(make_adapter(), CircuitBreaker(), max_attempts=3)
    )
    payload = {
        "event_id": event_uuid("optional"),
        "campaign_id": "CAMP-2026-10-A",
        "borrower_bucket": "31-60 DPD",
        "timestamp": "2026-09-01T09:04:01Z",
        "outcome": "no_answer",
    }

    async with client_for(app) as client:
        response = await client.post("/calls", json=payload)

    assert response.status_code == 200


async def test_all_attempts_fail_dead_letters_event(
    app_factory, client_for, sample_event
):
    adapter = AlwaysFail(RuntimeError("downstream 503"))
    resilient = ResilientPipeline(
        adapter, CircuitBreaker(failure_threshold=99), max_attempts=3
    )
    app = app_factory(resilient=resilient)
    eid = event_uuid("evt-fail")

    async with client_for(app) as client:
        response = await client.post("/calls", json=sample_event("evt-fail"))
        stored = await client.get(f"/dlq/{eid}")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["event_id"] == eid
    assert detail["status"] == "dead_lettered"
    assert "downstream 503" in detail["reason"]
    assert adapter.calls == 3

    item = stored.json()
    assert item["status"] == "pending"
    assert item["retry_count"] == 2  # attempts - 1
    assert "downstream 503" in item["failure_reason"]
    assert item["payload"]["event_id"] == eid


async def test_non_retryable_failure_not_retried_and_zero_retries(
    app_factory, client_for, sample_event
):
    """A non-retryable error short-circuits after one attempt."""
    import httpx

    request = httpx.Request("POST", "http://mock:8001/pipeline/process")
    response = httpx.Response(400, request=request)
    error = httpx.HTTPStatusError("bad request", request=request, response=response)
    adapter = ScriptedAdapter([error])
    resilient = ResilientPipeline(
        adapter, CircuitBreaker(failure_threshold=99), max_attempts=3
    )
    app = app_factory(resilient=resilient)
    eid = event_uuid("evt-400")

    async with client_for(app) as client:
        result = await client.post("/calls", json=sample_event("evt-400"))
        stored = (await client.get(f"/dlq/{eid}")).json()

    assert result.status_code == 503
    assert adapter.calls == 1                 # not retried
    assert stored["retry_count"] == 0         # accurate, not max_attempts-1
    assert "400" in stored["failure_reason"]


async def test_circuit_open_dead_letters_without_calling_downstream(
    app_factory, client_for, sample_event
):
    adapter = AlwaysFail()
    resilient = ResilientPipeline(
        adapter, CircuitBreaker(failure_threshold=1), max_attempts=1
    )
    app = app_factory(resilient=resilient, max_attempts=1)

    async with client_for(app) as client:
        first = await client.post("/calls", json=sample_event("e1"))
        second = await client.post("/calls", json=sample_event("e2"))
        stored = await client.get(f"/dlq/{event_uuid('e2')}")

    assert first.status_code == 503
    assert second.status_code == 503
    assert second.json()["detail"]["reason"] == "circuit_open"
    # The second request short-circuited: the downstream was never called again.
    assert adapter.calls == 1

    item = stored.json()
    assert item["failure_reason"] == "circuit_open"
    assert item["retry_count"] == 0


async def test_scenario_header_ignored_when_controls_disabled(
    app_factory, client_for, make_adapter, sample_event
):
    adapter = make_adapter()
    resilient = ResilientPipeline(adapter, CircuitBreaker(), max_attempts=3)
    app = app_factory(resilient=resilient, test_controls=False)

    async with client_for(app) as client:
        response = await client.post(
            "/calls",
            json=sample_event("evt-1"),
            headers={"X-Test-Scenario": "always_fail"},
        )

    assert response.status_code == 200
    assert adapter.requests[-1].scenario is None


async def test_scenario_header_forwarded_when_enabled(
    app_factory, client_for, make_adapter, sample_event
):
    adapter = make_adapter()
    resilient = ResilientPipeline(adapter, CircuitBreaker(), max_attempts=3)
    app = app_factory(resilient=resilient, test_controls=True)

    async with client_for(app) as client:
        response = await client.post(
            "/calls",
            json=sample_event("evt-1"),
            headers={"X-Test-Scenario": "always_succeed"},
        )

    assert response.status_code == 200
    assert adapter.requests[-1].scenario == "always_succeed"


async def test_unknown_scenario_rejected_when_enabled(
    app_factory, client_for, make_adapter, sample_event
):
    app = app_factory(
        resilient=ResilientPipeline(make_adapter(), CircuitBreaker(), max_attempts=3),
        test_controls=True,
    )

    async with client_for(app) as client:
        response = await client.post(
            "/calls",
            json=sample_event("evt-1"),
            headers={"X-Test-Scenario": "bogus"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "unknown_scenario"


async def test_duplicate_event_id_upserts_dlq_row(app_factory, client_for, sample_event):
    resilient = ResilientPipeline(
        AlwaysFail(RuntimeError("boom")),
        CircuitBreaker(failure_threshold=99),
        max_attempts=1,
    )
    app = app_factory(resilient=resilient, max_attempts=1)

    async with client_for(app) as client:
        await client.post("/calls", json=sample_event("dup", product_type="bnpl"))
        await client.post("/calls", json=sample_event("dup", product_type="credit_card"))
        items = (await client.get("/dlq")).json()["items"]

    assert len(items) == 1
    assert items[0]["event_id"] == event_uuid("dup")
    assert items[0]["payload"]["product_type"] == "credit_card"  # last write wins


async def test_connection_error_dead_lettered_with_useful_reason(
    app_factory, client_for, sample_event
):
    import httpx

    adapter = ScriptedAdapter([httpx.ConnectError("All connection attempts failed")])
    resilient = ResilientPipeline(
        adapter, CircuitBreaker(failure_threshold=99), max_attempts=1
    )
    app = app_factory(resilient=resilient, max_attempts=1)

    async with client_for(app) as client:
        response = await client.post("/calls", json=sample_event("e-conn"))

    assert response.status_code == 503
    assert "ConnectError" in response.json()["detail"]["reason"]


async def test_success_does_not_remove_existing_dlq_row(
    app_factory, client_for, make_adapter, sample_event
):
    # First call fails and is dead-lettered; a later success for the same event
    # id does not silently clear the DLQ row (idempotency key, no auto-cleanup).
    adapter = make_adapter([RuntimeError("boom"), {"status": "ok"}])
    resilient = ResilientPipeline(
        adapter, CircuitBreaker(failure_threshold=99), max_attempts=1
    )
    app = app_factory(resilient=resilient, max_attempts=1)

    async with client_for(app) as client:
        failed = await client.post("/calls", json=sample_event("dup"))
        succeeded = await client.post("/calls", json=sample_event("dup"))
        stored = await client.get(f"/dlq/{event_uuid('dup')}")

    assert failed.status_code == 503
    assert succeeded.status_code == 200
    assert stored.status_code == 200
    assert stored.json()["status"] == "pending"
