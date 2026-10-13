"""DeadLetterQueue persistence and policy (`unit`)."""
import pytest

from app.dlq import DeadLetterQueue

pytestmark = pytest.mark.unit


def test_put_and_get_roundtrip(dlq):
    dlq.put("e1", {"a": 1}, "boom", 2)

    item = dlq.get("e1")

    assert item["event_id"] == "e1"
    assert item["payload"] == {"a": 1}
    assert item["failure_reason"] == "boom"
    assert item["retry_count"] == 2
    assert item["status"] == "pending"
    assert item["created_at"]
    assert item["updated_at"]


def test_get_missing_returns_none(dlq):
    assert dlq.get("does-not-exist") is None


def test_list_returns_all_items(dlq):
    dlq.put("a", {}, "r", 0)
    dlq.put("b", {}, "r", 0)

    ids = {item["event_id"] for item in dlq.list()}

    assert ids == {"a", "b"}


def test_set_status_without_reason(dlq):
    dlq.put("e1", {}, "original", 0)

    dlq.set_status("e1", "completed")

    item = dlq.get("e1")
    assert item["status"] == "completed"
    assert item["failure_reason"] == "original"


def test_set_status_with_reason_overwrites_reason(dlq):
    dlq.put("e1", {}, "original", 0)

    dlq.set_status("e1", "pending", "updated reason")

    item = dlq.get("e1")
    assert item["status"] == "pending"
    assert item["failure_reason"] == "updated reason"


def test_duplicate_event_id_upserts_single_row(dlq):
    dlq.put("e1", {"v": 1}, "first", 1)
    dlq.put("e1", {"v": 2}, "second", 2)

    assert len(dlq.list()) == 1
    item = dlq.get("e1")
    assert item["payload"] == {"v": 2}
    assert item["failure_reason"] == "second"
    assert item["retry_count"] == 2
    # An upsert resets status to pending so a re-failed event is replayable.
    assert item["status"] == "pending"


def test_set_status_on_missing_event_is_noop(dlq):
    dlq.set_status("ghost", "completed", "n/a")
    assert dlq.get("ghost") is None


def test_persistence_survives_reopening(temp_db_path):
    first = DeadLetterQueue(temp_db_path)
    first.put("e1", {"v": 1}, "why", 3)

    reopened = DeadLetterQueue(temp_db_path)

    item = reopened.get("e1")
    assert item["payload"] == {"v": 1}
    assert item["failure_reason"] == "why"
    assert item["retry_count"] == 3


def test_claim_is_exclusive(dlq):
    dlq.put("e1", {}, "r", 0)

    assert dlq.claim("e1") is True
    # A second claim (e.g. concurrent replay) must lose.
    assert dlq.claim("e1") is False
    assert dlq.get("e1")["status"] == "processing"


def test_claim_missing_event_is_false(dlq):
    assert dlq.claim("ghost") is False


def test_claim_can_be_replayed_after_completion(dlq):
    dlq.put("e1", {}, "r", 0)
    dlq.set_status("e1", "completed")

    assert dlq.claim("e1") is True
    assert dlq.get("e1")["status"] == "processing"


def test_database_errors_are_not_swallowed(tmp_path):
    # Pointing the DLQ at a directory makes sqlite raise instead of silently
    # succeeding.
    import sqlite3

    with pytest.raises(sqlite3.Error):
        DeadLetterQueue(str(tmp_path))

