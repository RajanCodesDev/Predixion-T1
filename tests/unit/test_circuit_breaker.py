"""Circuit breaker state machine (`unit`)."""
import pytest

from app.resilience import CircuitBreaker

pytestmark = pytest.mark.unit


def test_initial_state_is_closed():
    breaker = CircuitBreaker()
    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.probe_in_flight is False


def test_closed_allows_requests(clock):
    breaker = CircuitBreaker()
    assert breaker.allow_request() is True
    assert breaker.state == "closed"


def test_success_keeps_closed_and_resets_failures():
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.failures = 2
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.failures == 0


def test_failures_open_at_threshold(clock):
    breaker = CircuitBreaker(failure_threshold=3)

    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "closed"

    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.opened_at == clock.monotonic()


def test_open_rejects_until_recovery_elapsed(clock):
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
    breaker.record_failure()
    assert breaker.state == "open"

    clock.advance(9)
    assert breaker.allow_request() is False
    assert breaker.state == "open"


def test_open_allows_exactly_one_probe_after_recovery(clock):
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
    breaker.record_failure()
    clock.advance(10)

    assert breaker.allow_request() is True
    assert breaker.state == "half_open"
    assert breaker.probe_in_flight is True

    # A concurrent request must not receive a second probe.
    assert breaker.allow_request() is False


def test_half_open_success_closes_circuit():
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
    breaker.state = "half_open"
    breaker.probe_in_flight = True

    breaker.record_success()

    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.probe_in_flight is False


def test_half_open_failure_reopens_circuit(clock):
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
    breaker.state = "half_open"
    breaker.probe_in_flight = True

    breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.opened_at == clock.monotonic()
    assert breaker.probe_in_flight is False


def test_reset_forces_closed_state():
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()
    assert breaker.state == "open"

    breaker.reset()

    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.opened_at == 0.0
    assert breaker.probe_in_flight is False
