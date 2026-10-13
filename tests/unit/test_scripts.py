"""CLI/script helpers (`unit`): event generation and the traffic runner loop."""
import argparse
import random

import pytest

from app.models import CallEvent
from scripts.generate_events import make_event as gen_event
from scripts.latency_sample import measure
from scripts.traffic_profile import _apply_demo_preset, _build_parser, run

pytestmark = pytest.mark.unit


def test_generate_events_produce_valid_appendix_a_events():
    rng = random.Random(7)
    for _ in range(50):
        event = gen_event(rng)
        parsed = CallEvent.model_validate(event)
        assert parsed.event_id is not None
        assert parsed.timestamp is not None


def test_generate_events_include_failed_downstream():
    rng = random.Random(3)
    outcomes = {gen_event(rng)["outcome"] for _ in range(400)}
    assert "failed_downstream" in outcomes


def test_latency_sample_report_shape():
    report = measure(5000, seed=42)
    assert report["p50_ms"] < report["p90_ms"] < report["p99_ms"]
    assert report["count"] == 5000
    assert report["sigma"] == pytest.approx(0.62)


def test_demo_preset_is_compressed_and_scaled():
    args = _build_parser().parse_args(["--demo"])
    _apply_demo_preset(args)
    assert args.speed == 3600.0            # 1 sim day == 24 real seconds
    assert args.duration_hours == 24.0     # traverses both campaign spikes
    assert 0 < args.volume_scale < 1.0


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


_POSTED: list = []


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in that always succeeds."""

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, path, json=None):
        _POSTED.append(json)
        return _FakeResponse(200, {"status": "ok", "retry_count": 1})

    async def get(self, path):
        return _FakeResponse(200, {"items": []})


async def test_traffic_runner_loop_sends_valid_events():
    _POSTED.clear()
    args = argparse.Namespace(
        base_url="http://api",
        speed=3600.0,
        duration_hours=1.0,
        volume_scale=0.05,
        max_concurrency=5,
        timeout=5.0,
        seed=11,
    )

    summary = await run(args, client_factory=_FakeClient)

    assert summary.launched >= 1
    assert summary.completed == summary.launched
    assert summary.ok == summary.launched
    assert summary.retries == summary.launched   # fake returns retry_count 1
    # Every generated payload round-trips through the Appendix A schema.
    assert len(_POSTED) == summary.launched
    for payload in _POSTED:
        CallEvent.model_validate(payload)

