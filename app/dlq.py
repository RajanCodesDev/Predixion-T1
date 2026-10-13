"""SQLite-backed dead-letter queue.

Only this service writes to the database. Every method opens a short-lived
connection, commits on success and rolls back on error, so database failures
propagate to the caller instead of being silently swallowed.
"""
import contextlib
import json
import sqlite3
from datetime import datetime, timezone

from app.config import DATABASE_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeadLetterQueue:
    """Persist failed call events so they can be inspected and replayed.

    Duplicate policy: ``event_id`` is the primary key. Re-dead-lettering an
    existing id **upserts** the row (payload and reason are the last write, the
    status is reset to ``pending``) so a re-failed event becomes replayable
    again. A later *success* for the same id does not delete the row.
    """

    def __init__(self, path=DATABASE_PATH):
        self.path = path
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
