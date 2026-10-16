"""CloudWatch custom metrics for ECS target tracking and the ops dashboard.

The API publishes three bounded metrics into the ``CLOUDWATCH_METRIC_NAMESPACE``
namespace (``CollectionsChallenge`` in ECS):

``InFlightRequestsPerTask``
    A **gauge**: the number of requests *currently* being served by *this* task,
    sampled at flush time. Every task publishes only its own value with the
    ``ClusterName``/``ServiceName`` dimensions, and the Terraform target-tracking
    policy reads the ``Average`` statistic. CloudWatch therefore averages the
    datapoints across all tasks, yielding "average concurrent requests per task"
    — the quantity Application Auto Scaling needs. Publishing a cumulative or
    fleet-wide number would be wrong: it would be multiplied by the task count.
``ApiRequests``
    A ``Count`` delta per flush, dimensioned by ``StatusClass`` (``2xx`` /
    ``4xx`` / ``5xx``, plus any other class that appears). The dashboard plots it
    with the ``Sum`` statistic.
``ApiLatencyMs``
    One datapoint per sampled request, in ``Milliseconds``. Individual samples
    (not an average) are published so the dashboard can use percentile
    statistics such as ``p90`` and ``p99``.

Design guarantees:

* The request path only mutates in-memory counters — it never calls AWS and
  never totals more than a few arithmetic operations, so a CloudWatch outage
  cannot fail or noticeably slow a request.
* Counters and latency samples are **bounded and lossless on failure**. Each
  flush stages the current deltas into a pending buffer and only discards that
  buffer after every ``PutMetricData`` call succeeds. A failed flush therefore
  retains its data (bounded by the number of status classes and
  :data:`DEFAULT_MAX_LATENCY_SAMPLES`) and the next successful flush publishes
  it, rather than silently dropping an interval. There is no retry queue and no
  unbounded growth: an outage that outlasts the latency window simply evicts the
  oldest samples.
* Publication is batched: one :class:`CloudWatchPublisher` task flushes every
  ``CLOUDWATCH_METRICS_INTERVAL`` seconds (default 10s) in chunks of at most
  :data:`MAX_DATUMS_PER_CALL`, keeping call volume far inside the PutMetricData
  quota. boto3's blocking call runs in a worker thread via ``asyncio.to_thread``
  so the event loop is never blocked. A single ``asyncio.Lock`` serialises
  flushes so a shutdown flush cannot interleave with the periodic loop.
* The publisher is lifecycle-managed (``start``/``stop`` from the FastAPI
  lifespan). It uses a single ``asyncio.Task`` — no unmanaged threads — and all
  publication errors are logged and swallowed.
* The publisher refuses to emit anything unless both ``ClusterName`` and
  ``ServiceName`` are configured, and :func:`metrics_from_env` fails fast at
  startup when metrics are enabled without them, so an invalid
  ``InFlightRequestsPerTask`` gauge can never reach the target-tracking policy
  (see :class:`MissingMetricDimensionsError`).
* Metrics are published every interval even when idle (in-flight ``0``), so the
  gauge stays fresh and target tracking can scale in when traffic stops. If a
  task dies its datapoints age out of the alarm window within one period.
* Only bounded dimensions are used (``ClusterName``, ``ServiceName``,
  ``StatusClass``). Request paths, headers, bodies and credentials are never
  recorded.

``/health`` is deliberately excluded from every metric: the ALB target group
and the ECS container health check poll it roughly every 30s per task, so
counting probes would inflate ``ApiRequests`` and hold phantom in-flight slots
while idle, which is exactly when scale-in should happen.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque

from app import config

logger = logging.getLogger(__name__)

# Metric names — must match infra/terraform/main.tf (target tracking + dashboard).
IN_FLIGHT_METRIC = "InFlightRequestsPerTask"
REQUEST_COUNT_METRIC = "ApiRequests"
LATENCY_METRIC = "ApiLatencyMs"

# Dimension names — again, exactly what Terraform expects.
DIM_CLUSTER = "ClusterName"
DIM_SERVICE = "ServiceName"
DIM_STATUS_CLASS = "StatusClass"

#: Request paths excluded from every metric (see module docstring).
EXCLUDED_PATHS = frozenset({"/health"})

#: Latency samples buffered between flushes. The most recent N are kept, which
#: bounds memory and the number of datums per flush under sustained load. This
#: is a documented approximation: under extreme load percentiles are computed
#: over the most recent samples rather than every request.
DEFAULT_MAX_LATENCY_SAMPLES = 500

#: PutMetricData accepts up to 1000 datums per call; we stay well below that so
#: each flush costs only a handful of calls (far inside the default quota).
MAX_DATUMS_PER_CALL = 100


class MissingMetricDimensionsError(RuntimeError):
    """Raised at startup when metrics are enabled but a required ECS dimension is
    empty.

    The Terraform target-tracking policy and dashboard query ``ClusterName`` and
    ``ServiceName`` together, so publishing without them produces a metric the
    scaling policy can never match — autoscaling would silently do nothing while
    the namespace accumulated orphan datapoints. Failing fast converts that
    silent misconfiguration into an obvious startup error: the ECS task never
    becomes healthy and the service's deployment circuit breaker rolls the
    change back instead of running without working autoscaling.
    """


def status_class(status_code: int) -> str:
    """Bucket an HTTP status into a bounded class label such as ``2xx``."""
    return f"{int(status_code) // 100}xx"


class RequestMetrics:
    """In-memory request activity for a single task.

    Mutated only from the application's event loop (middleware start/finish and
    the publisher's flush), so no locking is required. It performs no I/O and
    cannot raise, which is what makes it safe to touch in a ``finally`` block.

    Delivered data is staged before publication and discarded only by
    :meth:`commit` after a *successful* publish, so a transient CloudWatch
    failure loses nothing: the staged deltas roll into the next flush. Both
    buffers stay bounded (one entry per status class and at most
    ``max_latency_samples`` samples), so a prolonged outage cannot grow memory.
    """

    def __init__(self, max_latency_samples: int = DEFAULT_MAX_LATENCY_SAMPLES):
        self._in_flight = 0
        # Delivered since the last stage().
        self._counts: dict[str, int] = {}
        self._latency: deque[float] = deque(maxlen=max_latency_samples)
        # Staged and awaiting a successful publish; cleared by commit().
        self._pending_counts: dict[str, int] = {}
        self._pending_latency: deque[float] = deque(maxlen=max_latency_samples)

    @property
    def in_flight(self) -> int:
        """Current number of requests being served by this task (the gauge)."""
        return self._in_flight

    @property
    def counts(self) -> dict[str, int]:
        """Completed-request counts per status class not yet successfully published."""
        merged = dict(self._pending_counts)
        for cls, count in self._counts.items():
            merged[cls] = merged.get(cls, 0) + count
        return merged

    @property
    def latency_samples(self) -> list[float]:
        """Buffered latency samples not yet successfully published."""
        return list(self._pending_latency) + list(self._latency)

    def request_started(self) -> None:
        self._in_flight += 1

    def request_finished(self, status_code: int, latency_ms: float) -> None:
        # ``max(0, ...)`` is defensive: a bookkeeping bug must never publish a
        # misleading negative concurrency value.
        self._in_flight = max(0, self._in_flight - 1)
        cls = status_class(status_code)
        self._counts[cls] = self._counts.get(cls, 0) + 1
        self._latency.append(float(latency_ms))

    def stage(self) -> tuple[dict[str, int], list[float]]:
        """Move delivered deltas into the pending buffer and return them.

        The pending buffer is *not* cleared here — call :meth:`commit` once the
        snapshot has been published so a failed flush retains its data. The
        gauge is never staged because it represents the present moment, not an
        interval.
        """
        for cls, count in self._counts.items():
            self._pending_counts[cls] = self._pending_counts.get(cls, 0) + count
        self._counts = {}
        self._pending_latency.extend(self._latency)
        self._latency.clear()
        return dict(self._pending_counts), list(self._pending_latency)

    def commit(self) -> None:
        """Discard the staged snapshot after it was published successfully."""
        self._pending_counts = {}
        self._pending_latency.clear()


class RequestMetricsMiddleware:
    """Raw ASGI middleware that records request activity.

    Only status code and elapsed time are recorded; the path is used solely to
    decide whether to skip excluded endpoints. Request bodies, headers, query
    strings and full URLs are never inspected or stored.
    """

    def __init__(
        self,
        app,
        metrics: RequestMetrics,
        *,
        excluded_paths: frozenset[str] = EXCLUDED_PATHS,
        clock=time.monotonic,
    ):
        self.app = app
        self.metrics = metrics
        self.excluded_paths = frozenset(excluded_paths)
        self.clock = clock

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path", "/") in self.excluded_paths:
            await self.app(scope, receive, send)
            return

        self.metrics.request_started()
        started_at = self.clock()
        # If the downstream never starts a response (unhandled error), the
        # request is recorded as a 5xx.
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Runs for success, exceptions and cancellation, so the gauge can
            # never be left permanently elevated by a failed request.
            self.metrics.request_finished(
                status_code, (self.clock() - started_at) * 1000.0
            )


class CloudWatchPublisher:
    """Batched, lifecycle-managed publisher for the three custom metrics.

    ``collector`` may be supplied so middleware and publisher share one
    :class:`RequestMetrics`; otherwise a fresh one is created. Tests inject a
    fake ``client`` exposing a synchronous ``put_metric_data(**kwargs)`` method;
    production leaves ``client=None`` and a boto3 client is created lazily on
    the first successful flush. The boto3 import is deferred for the same
    reason: metrics must be free when disabled.
    """

    def __init__(
        self,
        collector: RequestMetrics | None = None,
        *,
        namespace: str,
        cluster_name: str = "",
        service_name: str = "",
        interval: float = 10.0,
        region: str | None = None,
        client=None,
    ):
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.collector = collector if collector is not None else RequestMetrics()
        self.namespace = namespace
        self.cluster_name = cluster_name
        self.service_name = service_name
        self.interval = float(interval)
        self.region = region
        self._client = client
        self._task: asyncio.Task | None = None
        # Serialises flushes: the periodic loop, the shutdown flush and any
        # injected call cannot stage/publish concurrently. Created lazily on the
        # running loop the first time it is awaited.
        self._flush_lock: asyncio.Lock | None = None
        self._dimension_error_logged = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the flush loop once. Safe to call repeatedly."""
        if self.started:
            return
        self._task = asyncio.create_task(
            self._run(), name="cloudwatch-metrics-publisher"
        )

    async def stop(self) -> None:
        """Cancel the flush loop and make one final best-effort flush.

        Idempotent and safe to call when never started. The final flush
        publishes the (normally zero) in-flight gauge so the task is not left
        looking busy as it shuts down.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # ``flush`` swallows its own errors; suppress cancellation so a
        # cancelled shutdown cannot propagate out of the lifespan handler.
        with contextlib.suppress(asyncio.CancelledError):
            await self.flush()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive, flush swallows
                logger.warning("Unexpected error in metrics flush loop", exc_info=True)

    # -- publication -------------------------------------------------------

    def dimensions(self, extra: dict | None = None) -> list[dict]:
        """Build bounded dimensions, omitting unset cluster/service names."""
        dims: list[dict] = []
        if self.cluster_name:
            dims.append({"Name": DIM_CLUSTER, "Value": self.cluster_name})
        if self.service_name:
            dims.append({"Name": DIM_SERVICE, "Value": self.service_name})
        if extra:
            dims.append(extra)
        return dims

    def missing_required_dimensions(self) -> list[str]:
        """Names of required dimensions that are unset for this publisher."""
        missing = []
        if not self.cluster_name:
            missing.append(DIM_CLUSTER)
        if not self.service_name:
            missing.append(DIM_SERVICE)
        return missing

    def build_datums(self) -> list[dict]:
        """Stage the collector and build PutMetricData datums.

        Returns an empty list (publishing nothing) if either required dimension
        is missing: without ``ClusterName``/``ServiceName`` the
        ``InFlightRequestsPerTask`` gauge cannot match the target-tracking
        policy, so emitting it would be invalid autoscaling data. Counts and
        latencies are withheld too, since every docstring/dashboard query treats
        the two dimensions as mandatory. :func:`metrics_from_env` additionally
        fails fast at startup, so this is defence in depth.
        """
        missing = self.missing_required_dimensions()
        if missing:
            if not self._dimension_error_logged:
                logger.error(
                    "Refusing to publish CloudWatch metrics: missing required "
                    "dimension(s) %s. Their names must match the Terraform "
                    "target-tracking policy and dashboard.",
                    ", ".join(missing),
                )
                self._dimension_error_logged = True
            return []

        counts, latencies = self.collector.stage()
        base = self.dimensions()
        datums: list[dict] = [
            {
                "MetricName": IN_FLIGHT_METRIC,
                "Value": float(self.collector.in_flight),
                "Unit": "Count",
                "Dimensions": base,
            }
        ]
        for cls in sorted(counts):
            datums.append(
                {
                    "MetricName": REQUEST_COUNT_METRIC,
                    "Value": float(counts[cls]),
                    "Unit": "Count",
                    "Dimensions": self.dimensions(
                        {"Name": DIM_STATUS_CLASS, "Value": cls}
                    ),
                }
            )
        for latency_ms in latencies:
            datums.append(
                {
                    "MetricName": LATENCY_METRIC,
                    "Value": round(float(latency_ms), 3),
                    "Unit": "Milliseconds",
                    "Dimensions": self.dimensions(),
                }
            )
        return datums

    def _ensure_client(self):
        if self._client is None:
            # Deferred import: keeps app import cheap and lets the test suite run
            # without boto3 installed / without AWS credentials.
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "cloudwatch",
                region_name=self.region,
                config=Config(
                    connect_timeout=3,
                    read_timeout=3,
                    retries={"max_attempts": 1},
                ),
            )
        return self._client

    def _put(self, client, chunk: list[dict]) -> None:
        client.put_metric_data(Namespace=self.namespace, MetricData=chunk)

    async def flush(self) -> None:
        """Publish one batch. Publication failures are logged, never raised.

        The collector is staged (not cleared) while building the batch and only
        :meth:`RequestMetrics.commit`-ed once every chunk succeeds. A failed
        publish therefore retains the staged counts/latencies for the next
        flush, so a transient CloudWatch outage causes a catch-up rather than
        data loss. If only some chunks of a multi-chunk batch succeed, the whole
        snapshot is retained and the succeeded chunks may be published twice on
        recovery — an accepted, bounded trade-off (there is no retry queue, and
        CloudWatch failures are usually systemic rather than per-chunk).
        """
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
        async with self._flush_lock:
            datums = self.build_datums()
            if not datums:
                return  # missing dimensions, or nothing to publish
            try:
                client = self._ensure_client()
            except Exception:
                logger.warning(
                    "CloudWatch client unavailable; %d metric datum(s) retained "
                    "for the next flush",
                    len(datums),
                    exc_info=True,
                )
                return
            committed = True
            for start in range(0, len(datums), MAX_DATUMS_PER_CALL):
                chunk = datums[start : start + MAX_DATUMS_PER_CALL]
                try:
                    await asyncio.to_thread(self._put, client, chunk)
                except asyncio.CancelledError:
                    # Shutdown/cancellation: keep the staged data so nothing is
                    # lost if another flush runs, then propagate.
                    committed = False
                    raise
                except Exception as exc:
                    # Best-effort: a CloudWatch failure must never affect
                    # requests or shutdown. Keep the staged snapshot for retry.
                    committed = False
                    logger.warning(
                        "PutMetricData failed (%s); %d metric datum(s) retained "
                        "for the next flush",
                        exc,
                        len(chunk),
                    )
            if committed:
                self.collector.commit()


def metrics_from_env() -> CloudWatchPublisher | None:
    """Build the publisher from configuration, or ``None`` when disabled.

    When metrics are enabled, both ``ECS_CLUSTER_NAME`` and ``ECS_SERVICE_NAME``
    are required: they are the dimensions the Terraform target-tracking policy
    and dashboard query. A missing value is a deployment misconfiguration, so we
    raise :class:`MissingMetricDimensionsError` at startup (loud, fail-fast)
    instead of publishing a gauge the scaling policy can never match. The ECS
    service's deployment circuit breaker rolls the bad revision back, so this
    does not leave a broken revision serving traffic.
    """
    if not config.CLOUDWATCH_METRICS_ENABLED:
        return None
    missing = [
        name
        for name, value in (
            ("ECS_CLUSTER_NAME", config.ECS_CLUSTER_NAME),
            ("ECS_SERVICE_NAME", config.ECS_SERVICE_NAME),
        )
        if not value or not value.strip()
    ]
    if missing:
        raise MissingMetricDimensionsError(
            "CLOUDWATCH_METRICS_ENABLED is true but required ECS dimension "
            f"environment variable(s) {', '.join(missing)} are empty. The "
            "target-tracking policy and dashboard query ClusterName and "
            "ServiceName, so metrics published without them cannot drive "
            "autoscaling. Set them (Terraform does this for the ECS API task) "
            "or disable CLOUDWATCH_METRICS_ENABLED."
        )
    return CloudWatchPublisher(
        namespace=config.CLOUDWATCH_METRIC_NAMESPACE,
        cluster_name=config.ECS_CLUSTER_NAME,
        service_name=config.ECS_SERVICE_NAME,
        interval=config.CLOUDWATCH_METRICS_INTERVAL,
        region=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or None,
    )
