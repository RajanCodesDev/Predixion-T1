"""Retry / backoff behaviour of ResilientPipeline (`unit`)."""
import httpx
import pytest

from app.models import PipelineRequest
from app.resilience import CircuitBreaker, CircuitOpenError, ResilientPipeline

pytestmark = pytest.mark.unit


async def test_first_attempt_success_zero_retries(make_adapter, fake_asyncio):
    adapter = make_adapter([{"status": "ok"}])
    pipeline = ResilientPipeline(adapter, CircuitBreaker(), max_attempts=3)

    result, retries = await pipeline.process(PipelineRequest(event_id="e1"))

    assert result == {"status": "ok"}
    assert retries == 0
    assert adapter.calls == 1
    assert fake_asyncio.sleeps == []


async def test_first_fails_second_succeeds(make_adapter, fake_asyncio):
    adapter = make_adapter([RuntimeError("503"), {"status": "ok"}])
    pipeline = ResilientPipeline(adapter, CircuitBreaker(), max_attempts=3)

    result, retries = await pipeline.process(PipelineRequest(event_id="e1"))

    assert result == {"status": "ok"}
    assert retries == 1
    assert adapter.calls == 2


async def test_two_failures_then_success(make_adapter, fake_asyncio):
    adapter = make_adapter([RuntimeError("503"), RuntimeError("503"), {"status": "ok"}])
    pipeline = ResilientPipeline(adapter, CircuitBreaker(), max_attempts=3)

    result, retries = await pipeline.process(PipelineRequest(event_id="e1"))

    assert result == {"status": "ok"}
    assert retries == 2
    assert adapter.calls == 3


async def test_all_attempts_fail_raises_and_counts_one_logical_failure(fake_asyncio):
    from tests.helpers import AlwaysFail

    adapter = AlwaysFail(RuntimeError("boom"))
    breaker = CircuitBreaker(failure_threshold=5)
    pipeline = ResilientPipeline(adapter, breaker, max_attempts=3)

    with pytest.raises(RuntimeError, match="boom"):
        await pipeline.process(PipelineRequest(event_id="e1"))

    assert adapter.calls == 3  # three total HTTP attempts
    assert breaker.failures == 1  # but only one logical-event failure


async def test_max_attempts_one_no_retry(fake_asyncio):
    from tests.helpers import AlwaysFail

    adapter = AlwaysFail()
    pipeline = ResilientPipeline(adapter, CircuitBreaker(), max_attempts=1)

    with pytest.raises(RuntimeError):
        await pipeline.process(PipelineRequest(event_id="e1"))

    assert adapter.calls == 1
    assert fake_asyncio.sleeps == []


async def test_backoff_is_exponential_and_capped(monkeypatch, fake_asyncio):
    import app.resilience as resilience

    monkeypatch.setattr(resilience, "BASE_DELAY_SECONDS", 0.1)
    monkeypatch.setattr(resilience, "MAX_DELAY_SECONDS", 0.25)
    monkeypatch.setattr(resilience, "random", _ConstantRandom(1.0))

    from tests.helpers import AlwaysFail

    pipeline = ResilientPipeline(AlwaysFail(), CircuitBreaker(failure_threshold=99), max_attempts=5)
    with pytest.raises(RuntimeError):
        await pipeline.process(PipelineRequest(event_id="e1"))

    # attempt 0..3 sleep; the final attempt does not sleep.
    assert fake_asyncio.sleeps == [0.1, 0.2, 0.25, 0.25]


async def test_backoff_jitter_uses_0_8_to_1_2(monkeypatch, fake_asyncio):
    import app.resilience as resilience

    monkeypatch.setattr(resilience, "BASE_DELAY_SECONDS", 1.0)
    monkeypatch.setattr(resilience, "MAX_DELAY_SECONDS", 100.0)
    recorder = _RecordingRandom(return_value=0.8)
    monkeypatch.setattr(resilience, "random", recorder)

    from tests.helpers import AlwaysFail

    pipeline = ResilientPipeline(AlwaysFail(), CircuitBreaker(failure_threshold=99), max_attempts=2)
    with pytest.raises(RuntimeError):
        await pipeline.process(PipelineRequest(event_id="e1"))

    assert (0.8, 1.2) in recorder.calls
    assert fake_asyncio.sleeps == [pytest.approx(0.8)]


async def test_open_circuit_raises_without_calling_adapter(make_adapter, clock):
    adapter = make_adapter()
    breaker = CircuitBreaker(failure_threshold=1)
    pipeline = ResilientPipeline(adapter, breaker, max_attempts=1)

    breaker.state = "open"
    breaker.opened_at = clock.monotonic()

    with pytest.raises(CircuitOpenError):
        await pipeline.process(PipelineRequest(event_id="e1"))

    assert adapter.calls == 0


class _ConstantRandom:
    def __init__(self, value):
        self.value = value

    def uniform(self, a, b):
        return self.value


class _RecordingRandom:
    def __init__(self, return_value):
        self.return_value = return_value
        self.calls = []

    def uniform(self, a, b):
        self.calls.append((a, b))
        return self.return_value


# --------------------------------------------------------------------------- #
# Retryability classification + accurate retry accounting
# --------------------------------------------------------------------------- #
def _status_error(status_code):
    request = httpx.Request("POST", "http://mock:8001/pipeline/process")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"status {status_code}", request=request, response=response)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_status_error(500), True),
        (_status_error(503), True),
        (_status_error(400), False),
        (_status_error(404), False),
        (httpx.ReadTimeout(""), True),
        (httpx.ConnectError("boom"), True),
        (RuntimeError("boom"), True),
        (ValueError("bad"), False),
        (TypeError("bad"), False),
    ],
)
def test_is_retryable_classification(exc, expected):
    from app.resilience import is_retryable

    assert is_retryable(exc) is expected


async def test_non_retryable_error_short_circuits(fake_asyncio):
    from app.resilience import NonRetryableError

    class Adapter:
        def __init__(self):
            self.calls = 0

        async def process(self, request):
            self.calls += 1
            raise NonRetryableError("permanent")

    adapter = Adapter()
    breaker = CircuitBreaker(failure_threshold=1)
    pipeline = ResilientPipeline(adapter, breaker, max_attempts=3)

    with pytest.raises(NonRetryableError) as caught:
        await pipeline.process(PipelineRequest(event_id="e1"))

    assert adapter.calls == 1              # not retried
    assert caught.value.retries == 0
    assert caught.value.non_retryable is True
    assert fake_asyncio.sleeps == []
    # Permanent errors do not trip the breaker (the dependency is healthy).
    assert breaker.failures == 0
    assert breaker.state == "closed"


async def test_terminal_retryable_failure_records_actual_retries(fake_asyncio):
    from tests.helpers import AlwaysFail

    adapter = AlwaysFail(RuntimeError("boom"))
    breaker = CircuitBreaker(failure_threshold=99)
    pipeline = ResilientPipeline(adapter, breaker, max_attempts=3)

    with pytest.raises(RuntimeError) as caught:
        await pipeline.process(PipelineRequest(event_id="e1"))

    assert caught.value.retries == 2       # attempts - 1
    assert caught.value.non_retryable is False
    assert breaker.failures == 1


async def test_cancelled_probe_releases_half_open(monkeypatch, clock):
    import asyncio as real_asyncio

    class BlockingAdapter:
        async def process(self, request):
            await real_asyncio.sleep(3600)

    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=1)
    breaker.record_failure()          # open
    clock.advance(1)                  # recovery elapsed -> half_open probe allowed
    pipeline = ResilientPipeline(BlockingAdapter(), breaker, max_attempts=1)

    task = real_asyncio.create_task(pipeline.process(PipelineRequest(event_id="e1")))
    await real_asyncio.sleep(0)       # let the probe start and set probe_in_flight
    assert breaker.probe_in_flight is True
    task.cancel()
    with pytest.raises(real_asyncio.CancelledError):
        await task

    # The probe flag was released so the breaker is not wedged.
    assert breaker.probe_in_flight is False
    assert breaker.state == "half_open"

