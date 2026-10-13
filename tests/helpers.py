"""Shared, dependency-free test doubles.

Importable as ``tests.helpers`` because the repository root is on ``sys.path``
(see ``pythonpath`` in pytest.ini).
"""
import uuid

from app.models import PipelineRequest


def event_uuid(label: str) -> str:
    """Deterministic, valid UUID for a short test label.

    ``CallEvent.event_id`` is validated as a UUID, so tests derive stable UUIDs
    from human-readable labels (e.g. ``event_uuid("evt-1")``).
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"collections-test:{label}"))


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class ScriptedAdapter:
    """Adapter that returns/raises a scripted outcome per call.

    Each outcome may be a dict (returned), an exception instance (raised), or an
    async callable taking the request. When the script is exhausted it succeeds.
    """

    def __init__(self, outcomes=()):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.requests = []

    async def process(self, request: PipelineRequest) -> dict:
        self.calls += 1
        self.requests.append(request)
        outcome = self._outcomes.pop(0) if self._outcomes else {"status": "ok"}
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome(request)
        return outcome


class AlwaysFail:
    """Adapter that always raises the given exception."""

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("downstream 503")
        self.calls = 0
        self.requests = []

    async def process(self, request: PipelineRequest) -> dict:
        self.calls += 1
        self.requests.append(request)
        raise self.exc


# Backwards-friendly alias used in some fixtures/tests.
AlwaysFailAdapter = AlwaysFail


class FakeTime:
    def __init__(self, start=1_000.0):
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
