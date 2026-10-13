"""Mock latency distribution and the reported ``latency_ms`` contract."""
import random
import types

import httpx
import pytest
from httpx import ASGITransport

from mock.main import (
    FAILURE_RATE,
    LATENCY_MU,
    LATENCY_SIGMA,
    create_app as create_mock_app,
)


def _percentile(ordered, pct):
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[k]


def _sample(count=20_000, seed=1234):
    rng = random.Random(seed)
    return sorted(
        max(1, int(rng.lognormvariate(LATENCY_MU, LATENCY_SIGMA))) for _ in range(count)
    )


@pytest.mark.unit
def test_latency_percentiles_hit_appendix_b_targets():
    samples = _sample()
    p50 = _percentile(samples, 50)
    p90 = _percentile(samples, 90)
    p99 = _percentile(samples, 99)

    assert 260 <= p50 <= 340, p50          # ~300 ms
    assert 600 <= p90 <= 790, p90          # ~700 ms
    assert 1050 <= p99 <= 1420, p99        # ~1200 ms


@pytest.mark.unit
def test_tuned_sigma_beats_the_original_035_tail():
    tuned = _sample()
    original = sorted(
        max(1, int(random.Random(1234).lognormvariate(5.7, 0.35))) for _ in range(20_000)
    )
    # The original sigma undershoots p90 badly; the tuned sigma is clearly closer.
    assert abs(_percentile(tuned, 90) - 700) < abs(_percentile(original, 90) - 700)


@pytest.mark.unit
def test_failure_rate_constant_is_20_percent():
    assert FAILURE_RATE == 0.20


@pytest.mark.integration
async def test_success_reports_sampled_latency_not_elapsed(monkeypatch):
    fake = types.SimpleNamespace(
        random=lambda: 0.99,
        lognormvariate=lambda mu, sigma: 7.0,
    )
    monkeypatch.setattr("mock.main.random", fake)
    app = create_mock_app(test_controls=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://mock"
    ) as client:
        response = await client.post("/pipeline/process", json={"event_id": "e1"})

    assert response.status_code == 200
    # latency_ms is the sampled value the mock slept for, not a wall-clock reading.
    assert response.json()["latency_ms"] == 7


@pytest.mark.integration
async def test_failure_path_skips_latency_sampling(monkeypatch):
    calls = {"lognorm": 0}

    def _lognorm(mu, sigma):
        calls["lognorm"] += 1
        return 300.0

    fake = types.SimpleNamespace(random=lambda: 0.0, lognormvariate=_lognorm)
    monkeypatch.setattr("mock.main.random", fake)
    app = create_mock_app(test_controls=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://mock"
    ) as client:
        response = await client.post("/pipeline/process", json={"event_id": "e1"})

    assert response.status_code == 503
    assert response.json() == {"status": "error", "reason": "downstream_timeout"}
    assert calls["lognorm"] == 0  # failure injected before latency
