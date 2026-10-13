"""Mock STT/LLM/TTS dependency (`integration`)."""
import time
import types

import httpx
import pytest
from httpx import ASGITransport

from mock.main import create_app as create_mock_app

pytestmark = pytest.mark.integration


def _fake_random(random_value, latency_ms=5.0):
    return types.SimpleNamespace(
        random=lambda: random_value,
        lognormvariate=lambda mu, sigma: latency_ms,
    )


async def test_health(mock_client):
    response = await mock_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_always_succeed_schema(mock_client):
    response = await mock_client.post(
        "/pipeline/process", json={"event_id": "e1", "scenario": "always_succeed"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["transcript"]
    assert body["response_audio_ref"]
    assert isinstance(body["latency_ms"], int)


async def test_always_fail_returns_required_error_payload(mock_client):
    response = await mock_client.post(
        "/pipeline/process", json={"event_id": "e1", "scenario": "always_fail"}
    )

    assert response.status_code == 503
    assert response.json() == {"status": "error", "reason": "downstream_timeout"}


async def test_fail_once_then_succeed_is_per_event(mock_client):
    first = await mock_client.post(
        "/pipeline/process", json={"event_id": "a", "scenario": "fail_once_then_succeed"}
    )
    second = await mock_client.post(
        "/pipeline/process", json={"event_id": "a", "scenario": "fail_once_then_succeed"}
    )
    other = await mock_client.post(
        "/pipeline/process", json={"event_id": "b", "scenario": "fail_once_then_succeed"}
    )

    assert first.status_code == 503
    assert second.status_code == 200
    # A different event id has an independent counter.
    assert other.status_code == 503


async def test_slow_scenario_actually_delays(mock_client):
    started = time.perf_counter()
    response = await mock_client.post(
        "/pipeline/process",
        json={"event_id": "e1", "scenario": "slow", "delay_ms": 50},
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 200
    assert elapsed_ms >= 40


async def test_unknown_scenario_is_rejected(mock_client):
    response = await mock_client.post(
        "/pipeline/process", json={"event_id": "e1", "scenario": "nonsense"}
    )

    assert response.status_code == 400
    assert response.json()["reason"] == "unknown_scenario"


async def test_default_failure_path(monkeypatch):
    monkeypatch.setattr("mock.main.random", _fake_random(random_value=0.0))
    app = create_mock_app(test_controls=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://mock"
    ) as client:
        response = await client.post("/pipeline/process", json={"event_id": "e1"})

    assert response.status_code == 503
    assert response.json()["reason"] == "downstream_timeout"


async def test_default_success_path_without_long_sleep(monkeypatch):
    monkeypatch.setattr(
        "mock.main.random", _fake_random(random_value=0.99, latency_ms=5.0)
    )
    app = create_mock_app(test_controls=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://mock"
    ) as client:
        response = await client.post("/pipeline/process", json={"event_id": "e1"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_scenario_ignored_when_test_controls_disabled(monkeypatch):
    monkeypatch.setattr(
        "mock.main.random", _fake_random(random_value=0.99, latency_ms=5.0)
    )
    app = create_mock_app(test_controls=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://mock"
    ) as client:
        # Even though a scenario is supplied, it must be ignored.
        response = await client.post(
            "/pipeline/process", json={"event_id": "e1", "scenario": "always_fail"}
        )

    assert response.status_code == 200
