"""Database-backed log storage for Photonarium.

Provides a :class:`logging.Handler` that writes log records to an SQLite
``logs`` table, and a standalone :func:`get_logs` query function for the
API endpoint.

Thread-safety model
-------------------
- **DatabaseLogHandler** opens its own dedicated SQLite connection (WAL mode,
  ``check_same_thread=False``) and serialises writes via an internal
  ``threading.Lock``.  This matches the pattern used throughout ``imagedb.py``
  — every connection has its own lock because SQLite connections are not
  thread-safe.
- **get_logs()** opens a short-lived read-only connection per call (cheap for
  SQLite), so it requires no lock.  WAL mode safely supports concurrent
  readers alongside the handler's single writer.

Both functions target the same database file (``photonarium.db`` by default),
but operate on fully independent connections — no contention with the main
``ImageDatabase._db_lock``.
"""

from __future__ import annotations

import logging
import sqlite3
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
    """Logging handler that persists records into an SQLite ``logs`` table.

    The handler maintains its own dedicated connection and lock, independent
    of any other database connections in the application.

    Args:
        db_path: Path to the SQLite database file.
        max_lines: Maximum number of log rows to keep.  Older rows are
            trimmed periodically (every ~100 inserts).
    """

    def __init__(self, db_path: str, max_lines: int) -> None:
        super().__init__()
        self._max_lines = max_lines
        self._lock_db = threading.Lock()
        self._insert_count = 0

        # Open a dedicated connection for log writes.  The ``logs`` table is
        # created by imagedb._init_database() during startup — we just need
        # our own WAL-mode connection for concurrent writes.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.commit()

    def emit(self, record: logging.LogRecord) -> None:
        """Write a log record to the database.

        Wrapped in a blanket ``try/except`` so that logging failures never
        propagate and crash the application.
        """
        try:
            msg = self.format(record)
            ts = self._format_timestamp(record)
            with self._lock_db:
                self._conn.execute(
                    'INSERT INTO logs (timestamp, level, logger, message) VALUES (?, ?, ?, ?)',
                    (ts, record.levelname, record.name, msg),
                )
                self._conn.commit()
                self._insert_count += 1
                if self._insert_count % 100 == 0:
                    self._trim()
        except Exception:
            # Must never crash the application
            pass

    def close(self) -> None:
        """Close the dedicated database connection."""
        with self._lock_db:
            try:
                self._conn.close()
            except Exception:
                pass
        super().close()

    # -- internal helpers ----------------------------------------------------

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
