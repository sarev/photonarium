"""Database-backed log storage for Photonarium.

Provides a :class:`logging.Handler` that writes log records to an SQLite
``logs`` table, and a standalone :func:`get_logs` query function for the
API endpoint.

Thread-safety model
-------------------
- **DatabaseLogHandler** buffers records in a RAM :class:`collections.deque`
  and batch-flushes them to the database every 3 seconds via a background
  timer thread.  This eliminates the per-record INSERT+COMMIT that previously
  caused SQLite write contention ("database is locked") during intensive
  pipeline stages.
- ``emit()`` only appends to the deque (thread-safe in CPython) — no DB
  access, no locks.
- ``flush()`` drains the deque and writes a single ``executemany`` +
  ``commit``.  A non-blocking ``_flush_lock`` prevents concurrent flushes.
- **get_logs()** opens a short-lived read-only connection per call (cheap for
  SQLite), so it requires no lock.  WAL mode safely supports concurrent
  readers alongside the handler's single writer.

Both functions target the same database file (``photonarium.db`` by default),
but operate on fully independent connections — no contention with the main
``ImageDatabase._db_lock``.
"""

from __future__ import annotations

import collections
import logging
import sqlite3
import sys
import threading

# ---------------------------------------------------------------------------
# Table DDL — imported by imagedb._init_database() for schema creation
# ---------------------------------------------------------------------------

SQL_CREATE_LOGS = """
CREATE TABLE IF NOT EXISTS logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    level     TEXT    NOT NULL,
    logger    TEXT    NOT NULL,
    message   TEXT    NOT NULL
)
"""

SQL_CREATE_LOGS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs (level)
"""


# ---------------------------------------------------------------------------
# DatabaseLogHandler — logging.Handler that writes to SQLite
# ---------------------------------------------------------------------------


class DatabaseLogHandler(logging.Handler):
    """Logging handler that buffers records in RAM and batch-flushes to SQLite.

    Records are appended to a :class:`collections.deque` in ``emit()`` (no DB
    access) and periodically flushed to the ``logs`` table by a background
    daemon thread every 3 seconds.  This eliminates per-record write
    transactions that cause SQLite lock contention during intensive pipeline
    stages.

    The handler maintains its own dedicated connection, independent of any
    other database connections in the application.

    Args:
        db_path: Path to the SQLite database file.
        max_lines: Maximum number of log rows to keep.  Older rows are
            trimmed periodically (every ~500 inserts).
    """

    # Flush interval in seconds — controls maximum log latency in the
    # in-app log viewer.
    _FLUSH_INTERVAL = 3.0

    # Safety cap on the RAM buffer (~1 MB at ~200 bytes/record).
    _BUFFER_MAXLEN = 5000

    # Trim the logs table every this many inserts (less frequent than the
    # previous per-record handler to reduce write amplification).
    _TRIM_INTERVAL = 500

    def __init__(self, db_path: str, max_lines: int) -> None:
        super().__init__()
        self._max_lines = max_lines
        self._insert_count = 0

        # Track silent failures so we can surface the first few via stderr
        # (can't use ``logger`` inside a handler — infinite recursion).
        self._error_count = 0

        # RAM buffer — deque.append() is thread-safe in CPython.
        self._buffer: collections.deque[tuple[str, str, str, str]] = collections.deque(
            maxlen=self._BUFFER_MAXLEN,
        )

        # Non-blocking lock prevents concurrent flushes (timer thread vs.
        # explicit flush() calls from the pipeline or shutdown).
        self._flush_lock = threading.Lock()

        # Open a dedicated connection for log writes.  The ``logs`` table is
        # created by imagedb.init_database() during startup — the handler must
        # be attached after that call so the table exists.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA busy_timeout=5000')
        self._conn.commit()

        # Background daemon thread that periodically flushes buffered records.
        self._stop_event = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            name='log-flush',
            daemon=True,
        )
        self._flush_thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer a log record for later batch write.

        No database access or locking — just a deque append.  If formatting
        fails the record is silently dropped (standard logging behaviour).
        """
        try:
            msg = self.format(record)
            ts = self._format_timestamp(record)
            self._buffer.append((ts, record.levelname, record.name, msg))
        except Exception:
            pass

    def flush(self) -> None:
        """Batch-write all buffered records to the database.

        Safe to call from any thread (pipeline stage boundaries, shutdown,
        or the background flush thread).  Uses a non-blocking lock so
        concurrent callers skip rather than queue up.
        """
        if not self._buffer:
            return

        if not self._flush_lock.acquire(blocking=False):
            return  # Another flush in progress — skip

        try:
            # Drain the buffer into a local list.
            batch: list[tuple[str, str, str, str]] = []
            while self._buffer:
                try:
                    batch.append(self._buffer.popleft())
                except IndexError:
                    break  # Concurrent drain — deque emptied

            if not batch:
                return

            self._conn.executemany(
                'INSERT INTO logs (timestamp, level, logger, message) VALUES (?, ?, ?, ?)',
                batch,
            )
            self._conn.commit()

            self._insert_count += len(batch)
            if self._insert_count >= self._TRIM_INTERVAL:
                self._trim()
                self._insert_count = 0
        except Exception as exc:
            # Re-buffer failed records so they aren't lost — prepend them
            # back so the next flush picks them up.  The deque's maxlen cap
            # will silently drop the oldest if the buffer overflows.
            for row in reversed(batch):
                self._buffer.appendleft(row)
            self._error_count += 1
            if self._error_count <= 3:
                print(
                    f'[DatabaseLogHandler] flush failed ({type(exc).__name__}: {exc})',
                    file=sys.stderr,
                )
        finally:
            self._flush_lock.release()

    def close(self) -> None:
        """Stop the flush thread, perform a final flush, and close the connection."""
        # Signal the flush thread to stop (it does a final flush before exiting).
        self._stop_event.set()
        self._flush_thread.join(timeout=5.0)

        # Belt-and-braces final flush in case the thread didn't complete.
        self.flush()

        try:
            self._conn.close()
        except Exception:
            pass
        super().close()

    # -- internal helpers ----------------------------------------------------

    def _flush_loop(self) -> None:
        """Background loop: flush buffered records every ``_FLUSH_INTERVAL`` seconds."""
        while not self._stop_event.wait(timeout=self._FLUSH_INTERVAL):
            self.flush()
        # Final flush before the thread exits.
        self.flush()

    @staticmethod
    def _format_timestamp(record: logging.LogRecord) -> str:
        """Format the record's creation time as an ISO-ish string."""
        import time as _time

        t = _time.localtime(record.created)
        return _time.strftime('%Y-%m-%d %H:%M:%S', t)

    def _trim(self) -> None:
        """Delete rows exceeding *max_lines*, keeping the most recent."""
        try:
            self._conn.execute(
                'DELETE FROM logs WHERE id <= (  SELECT id FROM logs ORDER BY id DESC LIMIT 1 OFFSET ?)',
                (self._max_lines,),
            )
            self._conn.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Query function for the API endpoint
# ---------------------------------------------------------------------------


def get_logs(
    db_path: str,
    level: str | None = None,
    limit: int = 500,
) -> list[dict[str, str]]:
    """Retrieve recent log entries from the database.

    Opens a transient read-only connection, so it can run concurrently with
    the handler's writer connection under WAL mode.

    Args:
        db_path: Path to the SQLite database file.
        level: Optional log level filter (e.g. ``'ERROR'``).
        limit: Maximum number of rows to return (most recent last).

    Returns:
        List of dicts with keys ``timestamp``, ``level``, ``logger``,
        ``message``, ordered oldest-first (most recent at the end).
    """
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            # Check that the logs table exists — it won't if logging is
            # disabled (log_retention_lines == 0) and has never been created.
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='logs'").fetchone()
            if not tables:
                return []

            # Subquery fetches the most recent N rows (DESC), then the
            # outer query re-sorts them chronologically (ASC) for display.
            if level:
                rows = conn.execute(
                    'SELECT timestamp, level, logger, message FROM ('
                    '  SELECT * FROM logs WHERE level = ? ORDER BY id DESC LIMIT ?'
                    ') ORDER BY id ASC',
                    (level.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT timestamp, level, logger, message FROM ('
                    '  SELECT * FROM logs ORDER BY id DESC LIMIT ?'
                    ') ORDER BY id ASC',
                    (limit,),
                ).fetchall()

            return [
                {
                    'timestamp': r['timestamp'],
                    'level': r['level'],
                    'logger': r['logger'],
                    'message': r['message'],
                }
                for r in rows
            ]
        finally:
            conn.close()
    except Exception:
        return []
