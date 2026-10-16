"""Metrics middleware wired through the FastAPI application (`api`).

Each test injects a publisher with a fake CloudWatch client, so nothing ever
reaches AWS and no credentials are required.
"""
import asyncio

import pytest

from app import config
from app.metrics import (
    IN_FLIGHT_METRIC,
    LATENCY_METRIC,
    REQUEST_COUNT_METRIC,
    CloudWatchPublisher,
    MissingMetricDimensionsError,
)
from app.resilience import CircuitBreaker, ResilientPipeline
from tests.helpers import RecordingCloudWatch, ScriptedAdapter

pytestmark = pytest.mark.api


def make_publisher(fail: bool = False, **overrides):
    kwargs = dict(
        namespace="CollectionsChallenge",
        cluster_name="collections-challenge",
        service_name="api",
        interval=3600.0,  # no automatic flush during a short test
        client=RecordingCloudWatch(fail=fail),
    )
    kwargs.update(overrides)
    return CloudWatchPublisher(**kwargs)


async def test_requests_are_recorded_through_the_app(
    app_factory, client_for, success_resilient, sample_event
):
    publisher = make_publisher()
    app = app_factory(resilient=success_resilient, metrics=publisher)

    async with client_for(app) as client:
        response = await client.post("/calls", json=sample_event("metrics-ok"))
        await client.get("/health")  # excluded from every metric

        assert response.status_code == 200
        # Asserted before shutdown: lifespan stop() flushes (and drains) the
        # per-interval deltas as a final best-effort publish.
        assert publisher.collector.in_flight == 0
        assert publisher.collector.counts == {"2xx": 1}  # /health not counted


async def test_status_classes_counted_by_the_middleware(
    app_factory, client_for, sample_event
):
    publisher = make_publisher()
    adapter = ScriptedAdapter([{"status": "ok"}, RuntimeError("downstream 503")])
    resilient = ResilientPipeline(
        adapter, CircuitBreaker(failure_threshold=99), max_attempts=1
    )
    app = app_factory(resilient=resilient, metrics=publisher)

    async with client_for(app) as client:
        ok = await client.post("/calls", json=sample_event("m-ok"))
        invalid = await client.post("/calls", json={})  # validation -> 422
        failed = await client.post("/calls", json=sample_event("m-fail"))

        assert (ok.status_code, invalid.status_code, failed.status_code) == (
            200,
            422,
            503,
        )
        assert publisher.collector.counts == {"2xx": 1, "4xx": 1, "5xx": 1}
        assert publisher.collector.in_flight == 0


async def test_concurrent_requests_are_gauged_through_the_app(
    app_factory, client_for, sample_event
):
    publisher = make_publisher()
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return {"status": "ok"}

    resilient = ResilientPipeline(
        ScriptedAdapter([slow, slow]), CircuitBreaker(), max_attempts=1
    )
    app = app_factory(resilient=resilient, metrics=publisher)

    async with client_for(app) as client:
        tasks = [
            asyncio.create_task(
                client.post("/calls", json=sample_event(f"conc-{i}"))
            )
            for i in range(2)
        ]
        for _ in range(200):
            if publisher.collector.in_flight == 2:
                break
            await asyncio.sleep(0)
        assert publisher.collector.in_flight == 2

        gate.set()
        responses = await asyncio.gather(*tasks)

        assert [r.status_code for r in responses] == [200, 200]
        assert publisher.collector.in_flight == 0
        # Asserted before shutdown: the lifespan's final flush drains deltas.
        assert publisher.collector.counts == {"2xx": 2}


async def test_flush_after_real_requests_publishes_expected_data(
    app_factory, client_for, success_resilient, sample_event
):
    publisher = make_publisher()
    app = app_factory(resilient=success_resilient, metrics=publisher)

    async with client_for(app) as client:
        response = await client.post("/calls", json=sample_event("dims"))
        await publisher.flush()

    assert response.status_code == 200
    fake = publisher._client
    assert fake.calls[0]["Namespace"] == "CollectionsChallenge"

    # The first flush captured the post-request state; the lifespan's final
    # flush (after this context) adds a second, identical idle gauge datum.
    gauge = fake.named(IN_FLIGHT_METRIC)[0]
    assert gauge["Value"] == 0.0
    assert gauge["Dimensions"] == [
        {"Name": "ClusterName", "Value": "collections-challenge"},
        {"Name": "ServiceName", "Value": "api"},
    ]

    (count,) = fake.named(REQUEST_COUNT_METRIC)
    assert count["Value"] == 1.0
    assert count["Dimensions"][-1] == {"Name": "StatusClass", "Value": "2xx"}

    (latency,) = fake.named(LATENCY_METRIC)
    assert latency["Unit"] == "Milliseconds"
    assert latency["Value"] >= 0.0

    # Only bounded dimension names are ever published.
    allowed = {"ClusterName", "ServiceName", "StatusClass"}
    assert all(
        {d["Name"] for d in datum["Dimensions"]} <= allowed for datum in fake.datums
    )


async def test_cloudwatch_failure_never_breaks_api_requests(
    app_factory, client_for, success_resilient, sample_event
):
    publisher = make_publisher(fail=True)
    app = app_factory(resilient=success_resilient, metrics=publisher)

    async with client_for(app) as client:
        first = await client.post("/calls", json=sample_event("cw-1"))
        await publisher.flush()  # a forced failing publish must be swallowed
        second = await client.post("/calls", json=sample_event("cw-2"))
        health = await client.get("/health")

        assert first.status_code == 200
        assert second.status_code == 200
        assert health.status_code == 200
        # Instrumentation keeps working locally even though publishing fails.
        # The failed flush retained its staged count, so both requests are still
        # buffered and will be published once CloudWatch recovers — no data loss.
        assert publisher.collector.counts == {"2xx": 2}


async def test_metrics_disabled_leaves_app_unchanged(
    app_factory, client_for, success_resilient, sample_event, monkeypatch
):
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_ENABLED", False)

    app = app_factory(resilient=success_resilient)

    assert app.state.metrics is None  # no middleware, no publisher, no boto3

    async with client_for(app) as client:
        response = await client.post("/calls", json=sample_event("disabled"))
    assert response.status_code == 200


def test_metrics_enabled_from_config_wires_publisher(app_factory, monkeypatch):
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_ENABLED", True)
    monkeypatch.setattr(config, "CLOUDWATCH_METRIC_NAMESPACE", "ConfigNS")
    monkeypatch.setattr(config, "ECS_CLUSTER_NAME", "cluster-x")
    monkeypatch.setattr(config, "ECS_SERVICE_NAME", "api")
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_INTERVAL", 15.0)

    app = app_factory()

    publisher = app.state.metrics
    assert publisher is not None
    assert publisher.namespace == "ConfigNS"
    assert publisher.cluster_name == "cluster-x"
    assert publisher.service_name == "api"
    assert publisher.interval == 15.0
    # The AWS client stays lazy: wiring the app never touches AWS.
    assert publisher._client is None


def test_metrics_enabled_without_dimensions_fails_fast(app_factory, monkeypatch):
    """A misconfigured ECS task must fail to start, not run without autoscaling.

    Terraform's target-tracking policy needs both dimensions; emitting a gauge
    without them would silently never scale. Failing during create_app means the
    container never becomes healthy and the ECS deployment circuit breaker rolls
    the bad revision back.
    """
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_ENABLED", True)
    monkeypatch.setattr(config, "ECS_CLUSTER_NAME", "")
    monkeypatch.setattr(config, "ECS_SERVICE_NAME", "api")

    with pytest.raises(MissingMetricDimensionsError):
        app_factory()


async def test_lifespan_starts_and_stops_the_publisher(
    app_factory, client_for, success_resilient, sample_event
):
    publisher = make_publisher()
    app = app_factory(resilient=success_resilient, metrics=publisher)

    assert publisher.started is False
    async with client_for(app) as client:
        assert publisher.started is True
        assert (await client.get("/health")).status_code == 200

    assert publisher.started is False
    # The final shutdown flush published the idle gauge (task looks idle).
    (gauge,) = publisher._client.named(IN_FLIGHT_METRIC)
    assert gauge["Value"] == 0.0
