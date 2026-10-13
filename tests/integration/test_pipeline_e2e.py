"""Full end-to-end flow: API -> resilience -> mock -> DLQ (`integration`).

These tests wire the real components together in-process using an ASGI
transport for both the API and the mock, so no network ports are required and
the mock's deterministic scenarios replace the random 20% failure rate.
"""
import contextlib

import httpx
import pytest
from httpx import ASGITransport

from app.dlq import DeadLetterQueue
from app.main import create_app
from app.pipeline import MockPipeline
from app.resilience import CircuitBreaker, ResilientPipeline
from mock.main import create_app as create_mock_app
from tests.helpers import ScriptedAdapter, event_uuid

pytestmark = pytest.mark.integration


@contextlib.asynccontextmanager
async def stack(tmp_path, *, breaker=None, max_attempts=3):
    mock_app = create_mock_app(test_controls=True)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=mock_app),
        base_url="http://mock:8001",
    ) as mock_client:
        dlq = DeadLetterQueue(str(tmp_path / "dlq.sqlite3"))
        resilient = ResilientPipeline(
            MockPipeline(mock_client),
            breaker or CircuitBreaker(failure_threshold=99),
            max_attempts=max_attempts,
        )
        app = create_app(
            dlq=dlq,
            resilient=resilient,
            mock_client=mock_client,
            enable_test_controls=True,
            max_attempts=max_attempts,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://api"
            ) as api:
                yield api, dlq, resilient


async def test_full_e2e_flow(tmp_path, sample_event):
    async with stack(tmp_path) as (api, dlq, resilient):
        # 1-3: submit a synthetic event, invoke the mock, verify the response.
        ok = await api.post(
            "/calls",
            json=sample_event("e2e-ok"),
            headers={"X-Test-Scenario": "always_succeed"},
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["retry_count"] == 0
        assert body["pipeline"]["status"] == "ok"
        assert body["pipeline"]["transcript"] == "synthetic transcript"

        # 4-7: force deterministic downstream failure, retries, DLQ persistence.
        failed = await api.post(
            "/calls",
            json=sample_event("e2e-fail"),
            headers={"X-Test-Scenario": "always_fail"},
        )
        assert failed.status_code == 503
        assert failed.json()["detail"]["status"] == "dead_lettered"

        listed = (await api.get("/dlq")).json()["items"]
        assert [item["event_id"] for item in listed] == [event_uuid("e2e-fail")]

        item = (await api.get(f"/dlq/{event_uuid('e2e-fail')}")).json()
        assert item["status"] == "pending"
        assert item["retry_count"] == 2  # max_attempts (3) - 1
        assert "503" in item["failure_reason"]
        # No internal dependency URL leaks through the failure reason.
        assert "://" not in item["failure_reason"]

        # 8-10: restore downstream availability and replay to completion.
        replay = await api.post(
            f"/dlq/{event_uuid('e2e-fail')}/replay",
            headers={"X-Test-Scenario": "always_succeed"},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "completed"

        final = (await api.get(f"/dlq/{event_uuid('e2e-fail')}")).json()
        assert final["status"] == "completed"


async def test_fail_once_then_succeed_is_retried_end_to_end(tmp_path, sample_event):
    async with stack(tmp_path) as (api, dlq, resilient):
        response = await api.post(
            "/calls",
            json=sample_event("e2e-once"),
            headers={"X-Test-Scenario": "fail_once_then_succeed"},
        )

    assert response.status_code == 200
    assert response.json()["retry_count"] == 1
    assert dlq.get(event_uuid("e2e-once")) is None  # never dead-lettered


async def test_timeout_is_dead_lettered_with_useful_reason(tmp_path, sample_event):
    """Timeouts usually have an empty str(); describe_exception must still help.

    ASGITransport does not enforce httpx timeouts, so this is asserted against a
    real ``httpx.ReadTimeout`` raised by the adapter rather than a slow mock.
    """
    import httpx

    adapter = ScriptedAdapter([httpx.ReadTimeout("")])
    resilient = ResilientPipeline(
        adapter, CircuitBreaker(failure_threshold=99), max_attempts=1
    )
    dlq = DeadLetterQueue(str(tmp_path / "dlq.sqlite3"))
    app = create_app(
        dlq=dlq,
        resilient=resilient,
        enable_test_controls=False,
        max_attempts=1,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api"
        ) as api:
            response = await api.post("/calls", json=sample_event("e2e-timeout"))

    assert response.status_code == 503
    reason = response.json()["detail"]["reason"]
    assert reason == "ReadTimeout"  # never an empty string
    assert dlq.get(event_uuid("e2e-timeout"))["failure_reason"] == "ReadTimeout"


async def test_circuit_opens_and_recovers_end_to_end(tmp_path, sample_event, clock):
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=10)
    async with stack(tmp_path, breaker=breaker, max_attempts=1) as (api, dlq, resilient):
        first = await api.post(
            "/calls",
            json=sample_event("c1"),
            headers={"X-Test-Scenario": "always_fail"},
        )
        assert first.status_code == 503
        assert (await api.get("/health")).json()["circuit_state"] == "closed"

        second = await api.post(
            "/calls",
            json=sample_event("c2"),
            headers={"X-Test-Scenario": "always_fail"},
        )
        assert second.status_code == 503
        assert (await api.get("/health")).json()["circuit_state"] == "open"

        # Open circuit fails fast without invoking the (healthy) mock.
        third = await api.post(
            "/calls",
            json=sample_event("c3"),
            headers={"X-Test-Scenario": "always_succeed"},
        )
        assert third.status_code == 503
        assert third.json()["detail"]["reason"] == "circuit_open"

        # After the recovery window a single probe is allowed and succeeds.
        clock.advance(10)
        recovered = await api.post(
            "/calls",
            json=sample_event("c4"),
            headers={"X-Test-Scenario": "always_succeed"},
        )
        assert recovered.status_code == 200
        assert (await api.get("/health")).json()["circuit_state"] == "closed"


async def test_dead_lettered_event_survives_api_restart(tmp_path, sample_event):
    """A new app instance on the same DB path still sees the dead-lettered event."""
    db_path = str(tmp_path / "dlq.sqlite3")

    async with stack(tmp_path) as (api, dlq, resilient):
        failed = await api.post(
            "/calls",
            json=sample_event("restart"),
            headers={"X-Test-Scenario": "always_fail"},
        )
        assert failed.status_code == 503

    # Fresh app + fresh DeadLetterQueue pointing at the same file (as a restart
    # with the same volume would).
    restarted_dlq = DeadLetterQueue(db_path)
    app = create_app(
        dlq=restarted_dlq,
        resilient=ResilientPipeline(
            ScriptedAdapter(), CircuitBreaker(failure_threshold=99), max_attempts=3
        ),
        enable_test_controls=True,
        max_attempts=3,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api"
        ) as api:
            item = (await api.get(f"/dlq/{event_uuid('restart')}")).json()

    assert item["event_id"] == event_uuid("restart")
    assert item["status"] == "pending"
    assert item["payload"]["event_id"] == event_uuid("restart")
