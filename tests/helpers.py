"""Shared, dependency-free test doubles.

Importable as ``tests.helpers`` because the repository root is on ``sys.path``
(see ``pythonpath`` in pytest.ini).
"""
import copy
import re
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


class RecordingCloudWatch:
    """Fake CloudWatch client that records PutMetricData calls.

    Exposes a synchronous ``put_metric_data`` (the publisher calls it through
    ``asyncio.to_thread``), so tests never need AWS credentials or boto3.
    """

    def __init__(self, fail: bool = False):
        self.calls = []
        self.fail = fail

    def put_metric_data(self, *, Namespace, MetricData):
        if self.fail:
            raise RuntimeError("cloudwatch unavailable")
        self.calls.append({"Namespace": Namespace, "MetricData": list(MetricData)})

    @property
    def datums(self):
        return [datum for call in self.calls for datum in call["MetricData"]]

    def named(self, metric_name):
        return [d for d in self.datums if d["MetricName"] == metric_name]


def _split_top_level(text):
    """Split on commas that are not nested inside parentheses."""
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current)
    return parts


def _resolve(name, names):
    name = name.strip()
    if name.startswith("#"):
        if name not in names:
            raise AssertionError(f"undefined expression attribute name {name}")
        return names[name]
    return name


def _conditional_check_failed():
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "the conditional request failed",
            }
        },
        "UpdateItem",
    )


class FakeDynamoDBClient:
    """In-memory DynamoDB subset used by :class:`DynamoDBDeadLetterQueue`.

    Items are stored as typed attribute maps (``{"S": ...}``/``{"N": ...}``),
    exactly like the real API, and the narrow ``UpdateItem`` grammar the DLQ
    emits is interpreted: ``SET a=:x, ..., created_at=if_not_exists(created_at,
    :c)`` plus the conditions ``attribute_exists(x)`` / ``x <> :y`` joined by
    ``AND``. Conditional failures raise a real ``ClientError`` so the
    production conditional-failure handling is what gets exercised.
    """

    def __init__(self, fail=None):
        self._items = {}
        self.fail = fail
        self.calls = []

    def _maybe_fail(self):
        if self.fail is not None:
            raise self.fail

    def get_item(self, *, TableName, Key):
        self._maybe_fail()
        self.calls.append("get_item")
        item = self._items.get(Key["event_id"]["S"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def scan(self, *, TableName, ExclusiveStartKey=None):
        self._maybe_fail()
        self.calls.append("scan")
        return {"Items": [copy.deepcopy(item) for item in self._items.values()]}

    def update_item(
        self,
        *,
        TableName,
        Key,
        UpdateExpression,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
    ):
        self._maybe_fail()
        self.calls.append("update_item")
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        event_id = Key["event_id"]["S"]
        item = self._items.get(event_id)
        if ConditionExpression is not None and not self._eval_condition(
            item, ConditionExpression, names, values
        ):
            raise _conditional_check_failed()
        if item is None:
            item = {"event_id": copy.deepcopy(Key["event_id"])}
            self._items[event_id] = item
        self._apply_set(item, UpdateExpression, names, values)

    @staticmethod
    def _eval_condition(item, condition, names, values):
        for clause in condition.split(" AND "):
            clause = clause.strip()
            exists = re.fullmatch(r"attribute_exists\(\s*(\S+?)\s*\)", clause)
            if exists:
                if item is None or _resolve(exists.group(1), names) not in item:
                    return False
                continue
            not_equal = re.fullmatch(r"(\S+)\s*<>\s*(:\w+)", clause)
            if not_equal:
                if item is None:
                    return False
                if item.get(_resolve(not_equal.group(1), names)) == values[not_equal.group(2)]:
                    return False
                continue
            raise AssertionError(f"unsupported condition clause: {clause!r}")
        return True

    @staticmethod
    def _apply_set(item, update_expression, names, values):
        if not update_expression.startswith("SET "):
            raise AssertionError(f"unsupported update expression: {update_expression!r}")
        for assignment in _split_top_level(update_expression[4:]):
            target, _, source = assignment.partition("=")
            target = _resolve(target, names)
            source = source.strip()
            fn = re.fullmatch(r"if_not_exists\(\s*(\S+?)\s*,\s*(:\w+)\s*\)", source)
            if fn:
                existing = _resolve(fn.group(1), names)
                if existing not in item:
                    item[target] = copy.deepcopy(values[fn.group(2)])
            elif source.startswith(":"):
                item[target] = copy.deepcopy(values[source])
            else:
                raise AssertionError(f"unsupported SET source: {source!r}")

