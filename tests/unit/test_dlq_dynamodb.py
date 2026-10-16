"""DynamoDBDeadLetterQueue behaviour and backend selection (`unit`).

These run against an in-memory fake client (``tests.helpers.FakeDynamoDBClient``)
that speaks the typed-attribute protocol and conditional-write semantics, so no
AWS credentials or network are needed.
"""
import copy

import pytest

from app.dlq import (
    DeadLetterQueue,
    DynamoDBDeadLetterQueue,
    MissingDlqConfigError,
    _is_conditional_check_failed,
    dlq_from_env,
)
from tests.helpers import FakeDynamoDBClient

pytestmark = pytest.mark.unit

TABLE = "collections-challenge-dlq"


def make_dlq(client=None):
    client = client or FakeDynamoDBClient()
    return DynamoDBDeadLetterQueue(TABLE, client=client), client


def test_put_and_get_roundtrip():
    dlq, _ = make_dlq()

    dlq.put("e1", {"a": 1}, "boom", 2)

    item = dlq.get("e1")
    assert item["event_id"] == "e1"
    assert item["payload"] == {"a": 1}
    assert item["failure_reason"] == "boom"
    assert item["retry_count"] == 2
    assert item["status"] == "pending"
    assert item["created_at"]
    assert item["updated_at"]


def test_retry_count_is_an_int_not_decimal():
    dlq, _ = make_dlq()
    dlq.put("e1", {}, "r", 3)
    assert isinstance(dlq.get("e1")["retry_count"], int)


def test_get_missing_returns_none():
    dlq, _ = make_dlq()
    assert dlq.get("does-not-exist") is None


def test_duplicate_put_upserts_and_preserves_created_at():
    dlq, _ = make_dlq()
    dlq.put("e1", {"v": 1}, "first", 1)
    created = dlq.get("e1")["created_at"]
    dlq.set_status("e1", "completed")

    dlq.put("e1", {"v": 2}, "second", 2)

    item = dlq.get("e1")
    assert item["payload"] == {"v": 2}
    assert item["failure_reason"] == "second"
    assert item["retry_count"] == 2
    assert item["status"] == "pending"
    assert item["created_at"] == created
    assert len(dlq.list()) == 1


def test_list_returns_all_items_sorted_by_created_at():
    dlq, _ = make_dlq()
    dlq.put("b", {}, "r", 0)
    dlq.put("a", {}, "r", 0)

    items = dlq.list()

    assert {item["event_id"] for item in items} == {"a", "b"}
    ordering = [(item["created_at"], item["event_id"]) for item in items]
    assert ordering == sorted(ordering)


def test_set_status_without_reason():
    dlq, _ = make_dlq()
    dlq.put("e1", {}, "original", 0)

    dlq.set_status("e1", "completed")

    item = dlq.get("e1")
    assert item["status"] == "completed"
    assert item["failure_reason"] == "original"


def test_set_status_with_reason():
    dlq, _ = make_dlq()
    dlq.put("e1", {}, "original", 0)

    dlq.set_status("e1", "pending", "still down")

    item = dlq.get("e1")
    assert item["status"] == "pending"
    assert item["failure_reason"] == "still down"


def test_set_status_on_missing_event_is_noop():
    dlq, client = make_dlq()
    dlq.set_status("ghost", "completed", "n/a")
    assert dlq.get("ghost") is None
    assert "ghost" not in client._items


def test_claim_is_exclusive():
    dlq, _ = make_dlq()
    dlq.put("e1", {}, "r", 0)

    assert dlq.claim("e1") is True
    assert dlq.claim("e1") is False
    assert dlq.get("e1")["status"] == "processing"


def test_claim_missing_event_is_false():
    dlq, _ = make_dlq()
    assert dlq.claim("ghost") is False


def test_claim_can_be_replayed_after_completion():
    dlq, _ = make_dlq()
    dlq.put("e1", {}, "r", 0)
    dlq.set_status("e1", "completed")

    assert dlq.claim("e1") is True
    assert dlq.get("e1")["status"] == "processing"


def test_persistence_across_instances_sharing_a_client():
    client = FakeDynamoDBClient()
    first = DynamoDBDeadLetterQueue(TABLE, client=client)
    first.put("e1", {"v": 1}, "why", 3)

    reopened = DynamoDBDeadLetterQueue(TABLE, client=client)

    item = reopened.get("e1")
    assert item["payload"] == {"v": 1}
    assert item["failure_reason"] == "why"
    assert item["retry_count"] == 3


def test_client_errors_propagate():
    client = FakeDynamoDBClient(fail=RuntimeError("dynamodb unavailable"))
    dlq = DynamoDBDeadLetterQueue(TABLE, client=client)

    with pytest.raises(RuntimeError):
        dlq.put("e1", {}, "r", 0)
    with pytest.raises(RuntimeError):
        dlq.get("e1")
    with pytest.raises(RuntimeError):
        dlq.list()


def test_constructor_rejects_empty_table_name():
    with pytest.raises(MissingDlqConfigError):
        DynamoDBDeadLetterQueue("")


def test_conditional_helper_rejects_plain_exceptions():
    assert _is_conditional_check_failed(RuntimeError("nope")) is False


def test_claim_and_set_status_propagate_non_conditional_errors():
    dlq = DynamoDBDeadLetterQueue(
        TABLE, client=FakeDynamoDBClient(fail=RuntimeError("dynamodb unavailable"))
    )
    with pytest.raises(RuntimeError):
        dlq.set_status("e1", "pending")
    with pytest.raises(RuntimeError):
        dlq.claim("e1")


def test_list_follows_scan_pagination():
    client = FakeDynamoDBClient()
    seeded = DynamoDBDeadLetterQueue(TABLE, client=client)
    seeded.put("a", {}, "r", 0)
    seeded.put("b", {}, "r", 0)

    class Paged(FakeDynamoDBClient):
        def scan(self, *, TableName, ExclusiveStartKey=None):
            self.calls.append("scan")
            items = list(self._items.values())
            if ExclusiveStartKey is None:
                return {
                    "Items": [copy.deepcopy(items[0])],
                    "LastEvaluatedKey": {"event_id": items[0]["event_id"]},
                }
            return {"Items": [copy.deepcopy(item) for item in items[1:]]}

    paged = Paged()
    paged._items = client._items

    items = DynamoDBDeadLetterQueue(TABLE, client=paged).list()
    assert {item["event_id"] for item in items} == {"a", "b"}


def test_client_is_created_lazily(monkeypatch):
    import boto3

    created = {}

    def fake_client(service, region_name=None, config=None):
        created["service"] = service
        created["region"] = region_name
        return FakeDynamoDBClient()

    monkeypatch.setattr(boto3, "client", fake_client)

    dlq = DynamoDBDeadLetterQueue(TABLE, region="ap-south-1")
    assert "service" not in created  # nothing created at construction time

    _ = dlq.client

    assert created == {"service": "dynamodb", "region": "ap-south-1"}


# --- backend selection (dlq_from_env) --------------------------------------

def test_dlq_from_env_defaults_to_sqlite(tmp_path):
    backend = dlq_from_env(backend="sqlite", database_path=str(tmp_path / "dlq.sqlite3"))
    assert isinstance(backend, DeadLetterQueue)


def test_dlq_from_env_selects_dynamodb():
    client = FakeDynamoDBClient()
    backend = dlq_from_env(backend="dynamodb", table_name=TABLE, client=client)
    assert isinstance(backend, DynamoDBDeadLetterQueue)
    assert backend.table_name == TABLE


def test_dlq_from_env_dynamodb_without_table_fails_fast():
    with pytest.raises(MissingDlqConfigError):
        dlq_from_env(backend="dynamodb", table_name="")


def test_dlq_from_env_unknown_backend_raises():
    with pytest.raises(ValueError):
        dlq_from_env(backend="redis")
