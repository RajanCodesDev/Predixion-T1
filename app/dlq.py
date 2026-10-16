"""Dead-letter queue backends.

Two interchangeable implementations behind the same synchronous interface:

``DeadLetterQueue``
    SQLite, the default. One short-lived connection per call, commits on
    success and rolls back on error, so database failures propagate instead of
    being silently swallowed. This is what local runs, ``docker compose`` and
    the test suite use.
``DynamoDBDeadLetterQueue``
    A single shared table, so dead letters survive task replacement and are
    visible to *every* task behind the load balancer. The SQLite file is
    per-task and ephemeral on Fargate, which makes the ``/dlq`` endpoints
    non-deterministic once the service scales past one task. Selected with
    ``DLQ_BACKEND=dynamodb`` plus ``DLQ_TABLE_NAME``; Terraform sets both for
    the ECS API task.

Both expose ``put``/``list``/``get``/``set_status``/``claim``. They are
synchronous (the existing tests and callers use them directly); the API invokes
them through ``asyncio.to_thread`` so the network-backed backend never blocks
the event loop.
"""
import contextlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from app import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissingDlqConfigError(RuntimeError):
    """``DLQ_BACKEND=dynamodb`` was selected without ``DLQ_TABLE_NAME``."""


def _is_conditional_check_failed(exc: Exception) -> bool:
    """True for the DynamoDB conditional-write failure, without importing boto3.

    ``botocore`` attaches a ``response`` dict to ``ClientError``; we only need
    the error code. Keeping this duck-typed avoids a hard botocore import for
    the SQLite-only path.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class DeadLetterQueue:
    """Persist failed call events so they can be inspected and replayed.

    Duplicate policy: ``event_id`` is the primary key. Re-dead-lettering an
    existing id **upserts** the row (payload and reason are the last write, the
    status is reset to ``pending``) so a re-failed event becomes replayable
    again. A later *success* for the same id does not delete the row.
    """

    def __init__(self, path=None):
        self.path = path if path is not None else config.DATABASE_PATH
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dead_letters (
                    event_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    failure_reason TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def put(self, event_id, payload, reason, retry_count):
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO dead_letters
                (event_id, payload, failure_reason, retry_count, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                  payload=excluded.payload, failure_reason=excluded.failure_reason,
                  retry_count=excluded.retry_count, status='pending', updated_at=excluded.updated_at
                """,
                (event_id, json.dumps(payload), reason, retry_count, now, now),
            )

    def list(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM dead_letters ORDER BY created_at"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def get(self, event_id):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dead_letters WHERE event_id=?", (event_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def set_status(self, event_id, status, reason=None):
        now = _now()
        with self._conn() as conn:
            if reason is None:
                conn.execute(
                    "UPDATE dead_letters SET status=?, updated_at=? WHERE event_id=?",
                    (status, now, event_id),
                )
            else:
                conn.execute(
                    "UPDATE dead_letters SET status=?, failure_reason=?, updated_at=? "
                    "WHERE event_id=?",
                    (status, reason, now, event_id),
                )

    def claim(self, event_id) -> bool:
        """Atomically mark an event as ``processing`` for replay.

        Returns ``True`` if this caller won the claim, ``False`` if the event is
        already being processed. The single guarded ``UPDATE`` makes concurrent
        replays safe (one 200, the others 409) without a read-then-write race.
        """
        now = _now()
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE dead_letters SET status='processing', updated_at=? "
                "WHERE event_id=? AND status!='processing'",
                (now, event_id),
            )
            return cursor.rowcount == 1


class DynamoDBDeadLetterQueue:
    """Shared, durable DLQ backed by a single DynamoDB table.

    Item shape mirrors the SQLite columns (``event_id`` is the partition key).
    ``payload`` is stored as a JSON string; ``retry_count`` as a number. Every
    write is a single conditional ``UpdateItem`` so concurrent replays stay
    safe across tasks, and ``put`` preserves the original ``created_at`` on
    re-dead-lettering (``if_not_exists``) to match the SQLite upsert.

    The boto3 client is created lazily on first use, so importing this module
    (or running the SQLite-backed tests) needs neither AWS credentials nor a
    region.
    """

    def __init__(self, table_name, client=None, region=None):
        if not table_name:
            raise MissingDlqConfigError("DLQ_TABLE_NAME must be set for the DynamoDB DLQ.")
        self.table_name = table_name
        self._client = client
        self.region = region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "dynamodb",
                region_name=self.region,
                config=Config(
                    connect_timeout=3,
                    read_timeout=3,
                    retries={"max_attempts": 1},
                ),
            )
        return self._client

    @staticmethod
    def _from_item(item):
        return {
            "event_id": item["event_id"]["S"],
            "payload": json.loads(item["payload"]["S"]),
            "failure_reason": item["failure_reason"]["S"],
            "retry_count": int(item["retry_count"]["N"]),
            "status": item["status"]["S"],
            "created_at": item["created_at"]["S"],
            "updated_at": item["updated_at"]["S"],
        }

    def put(self, event_id, payload, reason, retry_count):
        now = _now()
        self.client.update_item(
            TableName=self.table_name,
            Key={"event_id": {"S": event_id}},
            UpdateExpression=(
                "SET payload=:p, failure_reason=:r, retry_count=:rc, #s=:pending, "
                "updated_at=:u, created_at=if_not_exists(created_at, :c)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":p": {"S": json.dumps(payload)},
                ":r": {"S": reason},
                ":rc": {"N": str(int(retry_count))},
                ":pending": {"S": "pending"},
                ":u": {"S": now},
                ":c": {"S": now},
            },
        )

    def list(self):
        items = []
        kwargs = {"TableName": self.table_name}
        while True:
            response = self.client.scan(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        decoded = [self._from_item(item) for item in items]
        decoded.sort(key=lambda item: (item["created_at"], item["event_id"]))
        return decoded

    def get(self, event_id):
        response = self.client.get_item(
            TableName=self.table_name, Key={"event_id": {"S": event_id}}
        )
        item = response.get("Item")
        return self._from_item(item) if item else None

    def set_status(self, event_id, status, reason=None):
        now = _now()
        if reason is None:
            update = "SET #s=:s, updated_at=:u"
            values = {":s": {"S": status}, ":u": {"S": now}}
        else:
            update = "SET #s=:s, failure_reason=:r, updated_at=:u"
            values = {":s": {"S": status}, ":r": {"S": reason}, ":u": {"S": now}}
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={"event_id": {"S": event_id}},
                UpdateExpression=update,
                # A missing event is a no-op, matching the SQLite UPDATE that
                # touches zero rows rather than creating a partial item.
                ConditionExpression="attribute_exists(event_id)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised unless conditional
            if not _is_conditional_check_failed(exc):
                raise

    def claim(self, event_id) -> bool:
        """Atomically claim an event for replay (``status <> 'processing'``)."""
        now = _now()
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={"event_id": {"S": event_id}},
                UpdateExpression="SET #s=:processing, updated_at=:u",
                ConditionExpression="attribute_exists(event_id) AND #s <> :processing",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":processing": {"S": "processing"},
                    ":u": {"S": now},
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001 - re-raised unless conditional
            if _is_conditional_check_failed(exc):
                return False
            raise


def dlq_from_env(*, backend=None, table_name=None, database_path=None, client=None):
    """Build the configured DLQ backend.

    ``backend``/``table_name``/``database_path`` default to the environment
    (``DLQ_BACKEND``/``DLQ_TABLE_NAME``/``DATABASE_PATH``) but are injectable for
    tests. Fails fast when DynamoDB is selected without a table name so the task
    cannot start with an unusable DLQ.
    """
    selected = (backend if backend is not None else config.DLQ_BACKEND).strip().lower()
    if selected in ("", "sqlite"):
        return DeadLetterQueue(
            database_path if database_path is not None else config.DATABASE_PATH
        )
    if selected == "dynamodb":
        name = table_name if table_name is not None else config.DLQ_TABLE_NAME
        if not name or not name.strip():
            raise MissingDlqConfigError(
                "DLQ_BACKEND=dynamodb requires DLQ_TABLE_NAME to be set."
            )
        return DynamoDBDeadLetterQueue(name.strip(), client=client)
    raise ValueError(
        f"Unknown DLQ_BACKEND {selected!r}; expected 'sqlite' or 'dynamodb'."
    )
