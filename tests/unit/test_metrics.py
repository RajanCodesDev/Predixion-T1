"""CloudWatch metric instrumentation and publisher (`unit`).

These tests are AWS-free: the publisher is always given a fake client (or no
client at all) and the collector/middleware are exercised in-process.
"""
import asyncio

import pytest

from app import config
from app.metrics import (
    DEFAULT_MAX_LATENCY_SAMPLES,
    IN_FLIGHT_METRIC,
    LATENCY_METRIC,
    MAX_DATUMS_PER_CALL,
    REQUEST_COUNT_METRIC,
    CloudWatchPublisher,
    MissingMetricDimensionsError,
    RequestMetrics,
    RequestMetricsMiddleware,
    metrics_from_env,
    status_class,
)
from tests.helpers import RecordingCloudWatch

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
FakeCloudWatch = RecordingCloudWatch


class InnerApp:
    """Minimal ASGI app returning a fixed status, hooking before responding."""

    def __init__(self, status=200, error=None, hook=None):
        self.status = status
        self.error = error
        self.hook = hook
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.hook is not None:
            await self.hook()
        await send(
            {"type": "http.response.start", "status": self.status, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})


def make_middleware(inner, metrics, *, excluded_paths=None, clock=None):
    kwargs = {}
    if excluded_paths is not None:
        kwargs["excluded_paths"] = excluded_paths
    if clock is not None:
        kwargs["clock"] = clock
    return RequestMetricsMiddleware(inner, metrics, **kwargs)


async def drive(middleware, path="/calls"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware({"type": "http", "path": path, "method": "POST"}, receive, send)
    return sent


# --------------------------------------------------------------------------- #
# status_class helper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, "2xx"), (204, "2xx"), (307, "3xx"), (404, "4xx"), (500, "5xx"), (503, "5xx")],
)
def test_status_class_buckets(status, expected):
    assert status_class(status) == expected


# --------------------------------------------------------------------------- #
# Middleware: gauge, status classes, latency, cancellation
# --------------------------------------------------------------------------- #
async def test_in_flight_increments_then_decrements_on_success():
    metrics = RequestMetrics()
    seen = {}

    async def hook():
        seen["in_flight_during"] = metrics.in_flight

    middleware = make_middleware(InnerApp(status=200, hook=hook), metrics)

    sent = await drive(middleware)

    assert sent[-1]["type"] == "http.response.body"
    assert seen["in_flight_during"] == 1
    assert metrics.in_flight == 0
    assert metrics.counts == {"2xx": 1}
    assert len(metrics.latency_samples) == 1


async def test_in_flight_decrements_when_handler_raises():
    metrics = RequestMetrics()
    middleware = make_middleware(InnerApp(error=RuntimeError("boom")), metrics)

    with pytest.raises(RuntimeError, match="boom"):
        await drive(middleware)

    # finally block guarantees the gauge is not left elevated...
    assert metrics.in_flight == 0
    # ...and an unstarted response is recorded as a 5xx.
    assert metrics.counts == {"5xx": 1}


async def test_in_flight_decrements_when_request_is_cancelled():
    metrics = RequestMetrics()
    entered = asyncio.Event()

    async def hook():
        entered.set()
        await asyncio.Event().wait()  # never completes; waits for cancellation

    middleware = make_middleware(InnerApp(status=200, hook=hook), metrics)

    task = asyncio.create_task(drive(middleware))
    await entered.wait()
    assert metrics.in_flight == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert metrics.in_flight == 0


async def test_concurrent_requests_share_the_gauge():
    metrics = RequestMetrics()
    gate = asyncio.Event()
    entered = 0

    async def hook():
        nonlocal entered
        entered += 1
        await gate.wait()

    middleware = make_middleware(InnerApp(status=201, hook=hook), metrics)
    tasks = [asyncio.create_task(drive(middleware)) for _ in range(3)]

    for _ in range(100):
        if metrics.in_flight == 3:
            break
        await asyncio.sleep(0)

    assert metrics.in_flight == 3
    assert entered == 3

    gate.set()
    await asyncio.gather(*tasks)

    assert metrics.in_flight == 0
    assert metrics.counts == {"2xx": 3}


async def test_status_class_counting_covers_2xx_4xx_5xx():
    metrics = RequestMetrics()
    for status in (200, 404, 500, 503, 422):
        await drive(make_middleware(InnerApp(status=status), metrics))

    assert metrics.counts == {"2xx": 1, "4xx": 2, "5xx": 2}


async def test_latency_is_measured_with_injected_clock():
    metrics = RequestMetrics()

    class FakeClock:
        def __init__(self):
            self.t = 100.0

        def __call__(self):
            return self.t

    clock = FakeClock()

    async def hook():
        clock.t += 0.25  # handler "takes" 250ms

    await drive(make_middleware(InnerApp(status=200, hook=hook), metrics, clock=clock))

    assert metrics.latency_samples == [250.0]


async def test_health_path_is_excluded_from_metrics():
    metrics = RequestMetrics()
    inner = InnerApp(status=200)
    middleware = make_middleware(inner, metrics)

    await drive(middleware, path="/health")

    assert inner.calls == 1            # the endpoint still runs normally
    assert metrics.in_flight == 0
    assert metrics.counts == {}
    assert metrics.latency_samples == []


# --------------------------------------------------------------------------- #
# Publisher: datums, dimensions, units, batching
# --------------------------------------------------------------------------- #
def _publisher(client, **overrides):
    kwargs = dict(
        namespace="CollectionsChallenge",
        cluster_name="collections-challenge",
        service_name="api",
        interval=10.0,
        client=client,
    )
    kwargs.update(overrides)
    return CloudWatchPublisher(**kwargs)


async def test_flush_publishes_namespace_dimensions_and_units():
    fake = FakeCloudWatch()
    publisher = _publisher(fake)
    collector = publisher.collector

    collector.request_started()                  # one request stays in flight
    collector.request_started()
    collector.request_started()
    collector.request_finished(200, 12.5)
    collector.request_finished(503, 40.0)

    await publisher.flush()

    assert fake.calls[0]["Namespace"] == "CollectionsChallenge"

    (gauge,) = fake.named(IN_FLIGHT_METRIC)
    assert gauge["Value"] == 1.0
    assert gauge["Unit"] == "Count"
    assert gauge["Dimensions"] == [
        {"Name": "ClusterName", "Value": "collections-challenge"},
        {"Name": "ServiceName", "Value": "api"},
    ]

    counts = {d["Dimensions"][-1]["Value"]: d for d in fake.named(REQUEST_COUNT_METRIC)}
    assert set(counts) == {"2xx", "5xx"}
    assert counts["2xx"]["Value"] == 1.0
    assert counts["5xx"]["Value"] == 1.0
    for datum in counts.values():
        assert datum["Unit"] == "Count"
        assert datum["Dimensions"][:2] == [
            {"Name": "ClusterName", "Value": "collections-challenge"},
            {"Name": "ServiceName", "Value": "api"},
        ]
        assert datum["Dimensions"][-1]["Name"] == "StatusClass"

    latencies = sorted(d["Value"] for d in fake.named(LATENCY_METRIC))
    assert latencies == [12.5, 40.0]
    assert all(d["Unit"] == "Milliseconds" for d in fake.named(LATENCY_METRIC))
    # No sensitive or high-cardinality data ever lands in dimensions.
    allowed = {"ClusterName", "ServiceName", "StatusClass"}
    assert all(
        {d["Name"] for d in datum["Dimensions"]} <= allowed for datum in fake.datums
    )


async def test_gauge_is_current_not_cumulative_and_counts_are_deltas():
    fake = FakeCloudWatch()
    publisher = _publisher(fake)
    collector = publisher.collector

    collector.request_started()
    collector.request_started()  # two concurrent requests in flight

    await publisher.flush()
    assert fake.named(IN_FLIGHT_METRIC)[-1]["Value"] == 2.0
    assert fake.named(REQUEST_COUNT_METRIC) == []

    collector.request_finished(200, 5.0)
    collector.request_finished(200, 6.0)

    await publisher.flush()
    # Gauge drops back to zero rather than accumulating the four requests.
    assert fake.named(IN_FLIGHT_METRIC)[-1]["Value"] == 0.0
    # Only the two completions since the previous flush are counted.
    (counts,) = fake.named(REQUEST_COUNT_METRIC)
    assert counts["Value"] == 2.0
    assert counts["Dimensions"][-1] == {"Name": "StatusClass", "Value": "2xx"}


async def test_flush_without_required_dimensions_publishes_nothing(caplog):
    """A gauge lacking ClusterName/ServiceName cannot match the scaling policy.

    The publisher must therefore refuse to emit *any* datum rather than pollute
    the namespace with datapoints the target-tracking alarm and dashboard can
    never read.
    """
    fake = FakeCloudWatch()
    publisher = _publisher(fake, cluster_name="", service_name="")
    publisher.collector.request_finished(200, 5.0)

    with caplog.at_level("ERROR"):
        await publisher.flush()

    assert fake.calls == []
    # The staged data is retained (not silently dropped) should dimensions ever
    # be configured, and the refusal is logged exactly once.
    assert publisher.collector.counts == {"2xx": 1}
    refusals = [r for r in caplog.records if "Refusing to publish" in r.message]
    assert len(refusals) == 1

    await publisher.flush()  # second attempt logs nothing new
    assert len([r for r in caplog.records if "Refusing to publish" in r.message]) == 1


async def test_missing_required_dimensions_reports_unset_names():
    publisher = _publisher(FakeCloudWatch(), cluster_name="", service_name="api")
    assert publisher.missing_required_dimensions() == ["ClusterName"]

    publisher = _publisher(FakeCloudWatch(), cluster_name="c", service_name="")
    assert publisher.missing_required_dimensions() == ["ServiceName"]

    publisher = _publisher(FakeCloudWatch(), cluster_name="c", service_name="api")
    assert publisher.missing_required_dimensions() == []


async def test_failed_flush_retains_counts_and_latency_for_next_flush():
    """A transient CloudWatch failure must not lose a whole interval."""
    fake = FakeCloudWatch(fail=True)
    publisher = _publisher(fake)
    publisher.collector.request_finished(200, 11.0)
    publisher.collector.request_finished(503, 22.0)

    await publisher.flush()  # fails; nothing published, data retained
    assert fake.calls == []
    assert publisher.collector.counts == {"2xx": 1, "5xx": 1}
    assert sorted(publisher.collector.latency_samples) == [11.0, 22.0]

    # A further request arrives before CloudWatch recovers.
    publisher.collector.request_finished(200, 33.0)

    # Recovery: the next successful flush publishes everything exactly once.
    fake.fail = False
    await publisher.flush()

    counts = {d["Dimensions"][-1]["Value"]: d["Value"] for d in fake.named(REQUEST_COUNT_METRIC)}
    assert counts == {"2xx": 2.0, "5xx": 1.0}
    assert sorted(d["Value"] for d in fake.named(LATENCY_METRIC)) == [11.0, 22.0, 33.0]
    # Buffers are empty after a successful commit.
    assert publisher.collector.counts == {}
    assert publisher.collector.latency_samples == []


async def test_failed_flush_retention_is_bounded_by_latency_window():
    """Repeated failures never grow memory: the pending buffer is capped."""
    fake = FakeCloudWatch(fail=True)
    publisher = _publisher(fake)

    for _ in range(3):
        for i in range(DEFAULT_MAX_LATENCY_SAMPLES):
            publisher.collector.request_finished(200, float(i))
        await publisher.flush()

    assert publisher.collector.counts == {"2xx": 3 * DEFAULT_MAX_LATENCY_SAMPLES}
    assert len(publisher.collector.latency_samples) == DEFAULT_MAX_LATENCY_SAMPLES


async def test_flush_commits_only_after_success_and_stages_new_requests_separately():
    """Requests that arrive during a slow publish are not lost or double-counted."""
    fake = FakeCloudWatch()
    publisher = _publisher(fake)
    publisher.collector.request_finished(200, 1.0)

    await publisher.flush()  # commits the staged snapshot
    assert publisher.collector.counts == {}

    publisher.collector.request_finished(200, 2.0)
    await publisher.flush()
    counts = [d["Value"] for d in fake.named(REQUEST_COUNT_METRIC)]
    assert counts == [1.0, 1.0]  # one per flush, not 1 then 2


async def test_client_creation_failure_retains_data(monkeypatch):
    """No client (e.g. no credentials) must not consume the buffered interval."""
    publisher = _publisher(None, region="ap-south-1")
    publisher.collector.request_finished(200, 4.0)

    def boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(publisher, "_ensure_client", boom)
    await publisher.flush()  # must not raise

    assert publisher.collector.counts == {"2xx": 1}
    assert publisher.collector.latency_samples == [4.0]


async def test_latency_buffer_is_bounded_and_flush_drains_it():
    fake = FakeCloudWatch()
    publisher = _publisher(fake)
    for i in range(DEFAULT_MAX_LATENCY_SAMPLES + 100):
        publisher.collector.request_finished(200, float(i))

    assert len(publisher.collector.latency_samples) == DEFAULT_MAX_LATENCY_SAMPLES

    await publisher.flush()
    assert len(fake.named(LATENCY_METRIC)) == DEFAULT_MAX_LATENCY_SAMPLES
    assert publisher.collector.latency_samples == []


async def test_flush_batches_large_payloads():
    fake = FakeCloudWatch()
    publisher = _publisher(fake)
    for i in range(MAX_DATUMS_PER_CALL + 50):
        publisher.collector.request_finished(200, float(i))

    await publisher.flush()

    # 150 latency samples + 1 status-class count + 1 gauge = 152 datums, split
    # into bounded PutMetricData calls.
    total = len(fake.datums)
    assert total == (MAX_DATUMS_PER_CALL + 50) + 2
    assert len(fake.calls) == 2
    assert all(len(c["MetricData"]) <= MAX_DATUMS_PER_CALL for c in fake.calls)
    assert sum(len(c["MetricData"]) for c in fake.calls) == total


async def test_flush_failure_is_swallowed():
    fake = FakeCloudWatch(fail=True)
    publisher = _publisher(fake)
    publisher.collector.request_finished(500, 9.0)

    await publisher.flush()  # must not raise
    assert fake.calls == []


async def test_flush_survives_client_creation_failure(monkeypatch):
    """A missing AWS client/credentials must not break a request or flush."""
    publisher = _publisher(None, region="ap-south-1")
    publisher.collector.request_finished(200, 1.0)

    def boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(publisher, "_ensure_client", boom)

    await publisher.flush()  # must not raise


async def test_flush_with_no_datums_is_a_noop(monkeypatch):
    fake = FakeCloudWatch()
    publisher = _publisher(fake)
    monkeypatch.setattr(publisher, "build_datums", lambda: [])

    await publisher.flush()

    assert fake.calls == []


def test_ensure_client_is_lazy_and_cached():
    """Production path: a real boto3 client is built once, on first use."""
    publisher = _publisher(None, region="ap-south-1")

    assert publisher._client is None
    client = publisher._ensure_client()
    assert client.meta.service_model.service_name == "cloudwatch"
    assert publisher._ensure_client() is client  # cached, not rebuilt

    # Bounded client configuration: the publisher must fail fast, not pile up
    # retries that would delay the next flush.
    config = client.meta.config
    assert client.meta.region_name == "ap-south-1"
    assert config.connect_timeout == 3
    assert config.read_timeout == 3
    assert config.retries["total_max_attempts"] == 2  # 1 attempt + one retry


async def test_cancelled_flush_retains_staged_data():
    """A cancelled flush (e.g. shutdown) must not consume the buffered interval."""
    import time as _time

    class BlockingCloudWatch:
        def __init__(self):
            self.calls = []

        def put_metric_data(self, *, Namespace, MetricData):
            _time.sleep(0.2)  # simulate an in-progress network call
            self.calls.append((Namespace, list(MetricData)))

    publisher = _publisher(BlockingCloudWatch(), interval=3600)
    publisher.collector.request_finished(200, 7.0)

    task = asyncio.create_task(publisher.flush())
    await asyncio.sleep(0.02)  # let flush stage and enter to_thread
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The staged interval survived: a later flush will publish it.
    assert publisher.collector.counts == {"2xx": 1}
    assert publisher.collector.latency_samples == [7.0]
    await asyncio.sleep(0.25)  # let the orphaned worker thread finish


# --------------------------------------------------------------------------- #
# Publisher lifecycle
# --------------------------------------------------------------------------- #
async def test_start_is_idempotent_and_stop_is_safe():
    fake = FakeCloudWatch()
    publisher = _publisher(fake, interval=0.01)

    assert publisher.started is False
    await publisher.start()
    task = publisher._task
    assert publisher.started is True

    await publisher.start()  # second start is a no-op
    assert publisher._task is task

    for _ in range(100):
        if fake.calls:
            break
        await asyncio.sleep(0.005)
    assert fake.calls  # the loop published

    await publisher.stop()
    assert publisher.started is False

    published = len(fake.calls)
    await asyncio.sleep(0.03)
    assert len(fake.calls) == published  # loop really stopped


async def test_stop_without_start_is_a_noop():
    fake = FakeCloudWatch()
    publisher = _publisher(fake)

    await publisher.stop()

    assert publisher.started is False
    assert fake.calls == []


async def test_stop_makes_a_final_flush_of_the_current_gauge():
    fake = FakeCloudWatch()
    publisher = _publisher(fake, interval=3600)  # loop will not tick on its own
    await publisher.start()

    publisher.collector.request_started()  # pretend one request is still running
    await publisher.stop()

    (gauge,) = fake.named(IN_FLIGHT_METRIC)
    assert gauge["Value"] == 1.0

    await publisher.stop()  # idempotent
    assert len(fake.named(IN_FLIGHT_METRIC)) == 1


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        _publisher(FakeCloudWatch(), interval=0)


async def test_concurrent_flushes_do_not_double_publish():
    """The flush lock prevents two flushes staging the same interval twice."""
    fake = FakeCloudWatch()
    publisher = _publisher(fake)
    publisher.collector.request_finished(200, 5.0)

    await asyncio.gather(publisher.flush(), publisher.flush())

    # Exactly one flush publishes the 2xx count; the other publishes only the
    # (always-present) in-flight gauge.
    assert [d["Value"] for d in fake.named(REQUEST_COUNT_METRIC)] == [1.0]
    assert len(fake.named(IN_FLIGHT_METRIC)) == 2
    assert publisher.collector.counts == {}


# --------------------------------------------------------------------------- #
# Configuration wiring
# --------------------------------------------------------------------------- #
def test_metrics_from_env_disabled(monkeypatch):
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_ENABLED", False)

    assert metrics_from_env() is None


def test_metrics_from_env_reads_namespace_and_dimensions(monkeypatch):
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_ENABLED", True)
    monkeypatch.setattr(config, "CLOUDWATCH_METRIC_NAMESPACE", "TestNamespace")
    monkeypatch.setattr(config, "ECS_CLUSTER_NAME", "cluster-a")
    monkeypatch.setattr(config, "ECS_SERVICE_NAME", "api")
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_INTERVAL", 15.0)

    publisher = metrics_from_env()

    assert publisher is not None
    assert publisher.namespace == "TestNamespace"
    assert publisher.cluster_name == "cluster-a"
    assert publisher.service_name == "api"
    assert publisher.interval == 15.0
    # The boto3 client is created lazily: building the publisher is AWS-free.
    assert publisher._client is None


@pytest.mark.parametrize(
    ("cluster", "service", "missing"),
    [
        ("", "api", "ECS_CLUSTER_NAME"),
        ("cluster-a", "", "ECS_SERVICE_NAME"),
        ("   ", "api", "ECS_CLUSTER_NAME"),
        ("", "", None),
    ],
)
def test_metrics_from_env_requires_ecs_dimensions(monkeypatch, cluster, service, missing):
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_ENABLED", True)
    monkeypatch.setattr(config, "ECS_CLUSTER_NAME", cluster)
    monkeypatch.setattr(config, "ECS_SERVICE_NAME", service)

    with pytest.raises(MissingMetricDimensionsError) as excinfo:
        metrics_from_env()

    message = str(excinfo.value)
    if missing is None:
        assert "ECS_CLUSTER_NAME" in message and "ECS_SERVICE_NAME" in message
    else:
        assert missing in message


def test_metrics_from_env_disabled_ignores_missing_dimensions(monkeypatch):
    """Disabling metrics is always allowed, even with empty dimensions."""
    monkeypatch.setattr(config, "CLOUDWATCH_METRICS_ENABLED", False)
    monkeypatch.setattr(config, "ECS_CLUSTER_NAME", "")
    monkeypatch.setattr(config, "ECS_SERVICE_NAME", "")

    assert metrics_from_env() is None


def test_missing_metric_dimensions_error_is_runtime_error():
    assert issubclass(MissingMetricDimensionsError, RuntimeError)


async def test_real_boto3_client_accepts_published_metric_shapes():
    """The real SDK (stubbed, no network) accepts our datums and dimensions."""
    import boto3
    from botocore.config import Config
    from botocore.stub import Stubber

    client = boto3.client(
        "cloudwatch",
        region_name="ap-south-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 0}),
    )
    stubber = Stubber(client)
    stubber.add_response(
        "put_metric_data",
        {},
        expected_params={
            "Namespace": "CollectionsChallenge",
            "MetricData": [
                {
                    "MetricName": IN_FLIGHT_METRIC,
                    "Value": 0.0,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "ClusterName", "Value": "collections-challenge"},
                        {"Name": "ServiceName", "Value": "api"},
                    ],
                },
                {
                    "MetricName": REQUEST_COUNT_METRIC,
                    "Value": 1.0,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "ClusterName", "Value": "collections-challenge"},
                        {"Name": "ServiceName", "Value": "api"},
                        {"Name": "StatusClass", "Value": "2xx"},
                    ],
                },
                {
                    "MetricName": LATENCY_METRIC,
                    "Value": 12.5,
                    "Unit": "Milliseconds",
                    "Dimensions": [
                        {"Name": "ClusterName", "Value": "collections-challenge"},
                        {"Name": "ServiceName", "Value": "api"},
                    ],
                },
            ],
        },
    )

    publisher = _publisher(client)
    publisher.collector.request_finished(200, 12.5)

    stubber.activate()
    try:
        await publisher.flush()
    finally:
        stubber.deactivate()
    stubber.assert_no_pending_responses()
