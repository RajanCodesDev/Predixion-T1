"""Retry-with-backoff and circuit-breaking around the model dependency boundary.

The resilience layer is deliberately model-agnostic: it only knows the
``Pipeline`` protocol (``async process(request) -> dict``) and a small set of
exception types used to decide whether a failure is worth retrying. Swapping the
HTTP mock for a real open-weight STT/LLM/TTS adapter requires no changes here.

Configuration (all overridable via environment variables, see ``app.config``):

``MAX_ATTEMPTS``
    Total number of attempts for one logical event, *including* the first.
    ``MAX_ATTEMPTS=3`` means one initial attempt plus up to two retries.
``BASE_DELAY_SECONDS`` / ``MAX_DELAY_SECONDS``
    Exponential backoff base and cap. The delay before retry *n* (0-indexed) is
    ``min(BASE * 2**n, MAX)`` multiplied by a uniform jitter in ``[0.8, 1.2]``.
``CB_FAILURE_THRESHOLD``
    Consecutive logical-event failures that open the circuit.
``CB_RECOVERY_SECONDS``
    How long the circuit stays open before a single half-open probe is allowed.
"""
import asyncio
import random
import time
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from app.config import (
    BASE_DELAY_SECONDS,
    CB_FAILURE_THRESHOLD,
    CB_RECOVERY_SECONDS,
    MAX_ATTEMPTS,
    MAX_DELAY_SECONDS,
)


class CircuitOpenError(Exception):
    """Raised when the circuit is open and the call is rejected fast."""


class NonRetryableError(Exception):
    """Raised by an adapter for failures that must never be retried.

    Use this for schema/validation errors and other permanent failures. The
    resilience layer treats it as terminal on the first attempt.
    """


def is_retryable(exc: BaseException) -> bool:
    """Return whether ``exc`` represents a transient failure worth retrying.

    * ``NonRetryableError`` never retries.
    * HTTP 4xx responses are client/permanent errors -> no retry.
    * HTTP 5xx responses, timeouts and transport errors -> retry.
    * Validation/type errors from this service -> no retry.
    * Anything unrecognised defaults to retryable (favours resilience and is
      surfaced through the breaker + DLQ rather than being silently dropped).
    """
    if isinstance(exc, NonRetryableError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return False
    return True


@dataclass
class CircuitBreaker:
    """Three-state breaker: closed -> open -> half_open -> closed.

    The breaker is mutated only from a single event loop and none of its methods
    ``await``, so each transition is atomic with respect to other coroutines.
    """

    failure_threshold: int = CB_FAILURE_THRESHOLD
    recovery_seconds: float = CB_RECOVERY_SECONDS
    state: str = "closed"
    failures: int = 0
    opened_at: float = 0.0
    probe_in_flight: bool = False

    def allow_request(self) -> bool:
        now = time.monotonic()
        if self.state == "closed":
            return True
        if self.state == "open":
            if now - self.opened_at < self.recovery_seconds:
                return False
            self.state = "half_open"
            self.probe_in_flight = False
        if self.state == "half_open" and not self.probe_in_flight:
            self.probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self.state = "closed"
        self.failures = 0
        self.probe_in_flight = False

    def record_failure(self) -> None:
        self.probe_in_flight = False
        if self.state == "half_open":
            # A failed probe re-opens the circuit and restarts the timer.
            self.state = "open"
            self.opened_at = time.monotonic()
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = time.monotonic()

    def release_probe(self) -> None:
        """Release a half-open probe without recording success or failure.

        Called when an in-flight probe is cancelled so the breaker is not wedged
        in half-open forever; the next request is free to probe again.
        """
        self.probe_in_flight = False

    def reset(self) -> None:
        """Force the breaker back to a clean closed state.

        Used by the test-only circuit-reset control; has no effect on normal
        request processing.
        """
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.probe_in_flight = False


class ResilientPipeline:
    """Wrap a ``Pipeline`` adapter with retry, backoff and circuit breaking."""

    def __init__(self, adapter, breaker=None, max_attempts=MAX_ATTEMPTS):
        self.adapter = adapter
        self.breaker = breaker or CircuitBreaker()
        self.max_attempts = max_attempts

    def _backoff_delay(self, attempt: int) -> float:
        base = min(BASE_DELAY_SECONDS * (2 ** attempt), MAX_DELAY_SECONDS)
        return base * random.uniform(0.8, 1.2)

    async def process(self, request):
        """Run one logical event through the adapter.

        Returns ``(result, retry_count)`` on success, where ``retry_count`` is
        the number of retries performed (``attempts - 1``). On terminal failure
        the original exception is re-raised with two extra attributes:

        * ``.retries`` -- the actual number of retries performed (accurate even
          when a non-retryable error short-circuits the loop).
        * ``.non_retryable`` -- whether the failure was classified as permanent.
        """
        if not self.breaker.allow_request():
            raise CircuitOpenError("circuit_open")

        last_error: BaseException | None = None
        retries = 0
        try:
            for attempt in range(self.max_attempts):
                try:
                    result = await self.adapter.process(request)
                except Exception as exc:  # noqa: BLE001 - re-raised below
                    last_error = exc
                    retryable = is_retryable(exc)
                    if not retryable or attempt == self.max_attempts - 1:
                        retries = attempt
                        break
                    retries = attempt + 1
                    await asyncio.sleep(self._backoff_delay(attempt))
                    continue
                self.breaker.record_success()
                return result, attempt
        except asyncio.CancelledError:
            # A cancelled probe must not leave the breaker stuck half-open.
            self.breaker.release_probe()
            raise

        # Terminal failure: count one logical-event failure, not each attempt.
        if is_retryable(last_error):
            self.breaker.record_failure()
        last_error.retries = retries
        last_error.non_retryable = not is_retryable(last_error)
        raise last_error
