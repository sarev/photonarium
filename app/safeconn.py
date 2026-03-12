"""Thread-safe SQLite connection wrapper for Photonarium.

Prevents the "database is locked" connection-poisoning problem that occurs
when a ``conn.execute()`` or ``conn.commit()`` raises
``sqlite3.OperationalError`` and the implicit transaction is left open,
making all subsequent writes on that connection fail until the process is
restarted.

Usage::

    from safeconn import SafeConnection

    # Shared connection (Flask + pipeline) — pass the shared RLock
    safe = SafeConnection(raw_conn, lock=shared_rlock, name='shared')

    # Worker / thread-local connection — gets its own RLock
    safe = SafeConnection(raw_conn, name='worker-3')

    # Individual calls auto-lock (fine for reads, simple single-statement writes):
    row = safe.execute('SELECT ...').fetchone()

    # Broader atomic scope (read-modify-write, multi-statement transactions):
    with safe:
        row = safe.execute('SELECT ... WHERE id = ?', (pk,)).fetchone()
        safe.execute('UPDATE ... SET x = ? WHERE id = ?', (val, pk))
        safe.commit()
    # __exit__ auto-rolls-back if an exception escaped the block.

Design
------
- **RLock per call**: every ``execute``, ``executemany``, ``commit``,
  ``rollback`` acquires the connection's RLock.  Since it is reentrant,
  calls inside a ``with safe:`` block nest harmlessly.
- **Retry on transient lock errors**: write operations retry up to
  *retries* times with linear back-off before giving up.
- **Rollback on failure**: if the final retry fails, the pending
  transaction is rolled back so the connection stays usable.
- **Context-manager rollback**: ``__exit__`` rolls back automatically
  when the block exits with an exception, then re-raises.
- **Diagnostic logging**: under DEBUG level, logs lock wait times,
  retries, rollbacks, and context-manager spans for diagnosing
  contention issues.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

# Default retry count for transient "database is locked" errors.
_DEFAULT_RETRIES = 3

# Back-off factor (seconds) multiplied by attempt number.
_BACKOFF_FACTOR = 0.2

# Warn if acquiring the RLock takes longer than this (seconds).
_LOCK_WARN_THRESHOLD = 2.0


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    """Return True if *exc* is a transient SQLite lock error."""
    return 'database is locked' in str(exc)


class SafeConnection:
    """Thread-safe wrapper around :class:`sqlite3.Connection`.

    All public methods serialise access through an :class:`threading.RLock`.
    Write operations (``execute`` with DML, ``executemany``, ``commit``)
    retry on transient ``database is locked`` errors and always roll back
    after a final failure so the connection remains usable.

    Args:
        conn: The raw SQLite connection to wrap.
        lock: Optional shared :class:`threading.RLock`.  If *None*, a
            private RLock is created (suitable for thread-local
            connections that are never shared).
        retries: Number of attempts for write operations before giving up.
        name: Human-readable label for log messages (e.g. ``'shared'``,
            ``'worker-3'``, ``'dup-manager'``).  Defaults to the
            connection's ``id()``.
    """

    __slots__ = ('_conn', '_debug', '_lock', '_name', '_retries')

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.RLock | None = None,
        retries: int = _DEFAULT_RETRIES,
        name: str | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()
        self._retries = retries
        self._name = name or f'conn-{id(conn):x}'
        # Cache the debug flag so we don't call isEnabledFor() on every
        # execute() — rechecked lazily via the property when needed.
        self._debug = logger.isEnabledFor(logging.DEBUG)

    # ------------------------------------------------------------------
    # Lock helpers with timing diagnostics
    # ------------------------------------------------------------------

    def _acquire_lock(self, operation: str) -> None:
        """Acquire the RLock, warning if it takes too long."""
        if not self._debug:
            self._lock.acquire()
            return

        # Try non-blocking first — fast path for uncontested locks
        if self._lock.acquire(blocking=False):
            return

        # Contested — time the wait
        t0 = time.monotonic()
        thread = threading.current_thread().name
        logger.debug(
            '[%s] %s waiting for lock (thread=%s)',
            self._name, operation, thread,
        )
        self._lock.acquire()
        wait = time.monotonic() - t0
        if wait >= _LOCK_WARN_THRESHOLD:
            logger.warning(
                '[%s] %s waited %.2fs for lock (thread=%s)',
                self._name, operation, wait, thread,
            )
        else:
            logger.debug(
                '[%s] %s acquired lock after %.3fs (thread=%s)',
                self._name, operation, wait, thread,
            )

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def execute(self, sql: str, parameters: tuple | list = ()) -> sqlite3.Cursor:
        """Execute a single SQL statement with retry and rollback.

        For read-only statements the retry/rollback path is effectively
        a no-op (reads don't get "database is locked" in WAL mode), but
        the lock still serialises access to the connection object.
        """
        self._acquire_lock('execute')
        try:
            for attempt in range(self._retries):
                try:
                    return self._conn.execute(sql, parameters)
                except sqlite3.OperationalError as exc:
                    if _is_locked_error(exc) and attempt < self._retries - 1:
                        logger.warning(
                            '[%s] execute() locked, retrying (%d/%d): %s',
                            self._name, attempt + 1, self._retries,
                            sql[:80],
                        )
                        time.sleep(_BACKOFF_FACTOR * (attempt + 1))
                        continue
                    logger.warning(
                        '[%s] execute() failed after %d attempts, '
                        'rolling back: %s',
                        self._name, attempt + 1, sql[:80],
                    )
                    self._rollback_quietly()
                    raise
        finally:
            self._lock.release()
        # Unreachable, but keeps type-checkers happy
        raise RuntimeError('retry loop exited unexpectedly')  # pragma: no cover

    def executemany(self, sql: str, seq_of_parameters) -> sqlite3.Cursor:
        """Execute a parameterised statement against a sequence of rows."""
        self._acquire_lock('executemany')
        try:
            for attempt in range(self._retries):
                try:
                    return self._conn.executemany(sql, seq_of_parameters)
                except sqlite3.OperationalError as exc:
                    if _is_locked_error(exc) and attempt < self._retries - 1:
                        logger.warning(
                            '[%s] executemany() locked, retrying (%d/%d): %s',
                            self._name, attempt + 1, self._retries,
                            sql[:80],
                        )
                        time.sleep(_BACKOFF_FACTOR * (attempt + 1))
                        continue
                    logger.warning(
                        '[%s] executemany() failed after %d attempts, '
                        'rolling back: %s',
                        self._name, attempt + 1, sql[:80],
                    )
                    self._rollback_quietly()
                    raise
        finally:
            self._lock.release()
        raise RuntimeError('retry loop exited unexpectedly')  # pragma: no cover

    def commit(self) -> None:
        """Commit the current transaction with retry and rollback."""
        self._acquire_lock('commit')
        try:
            for attempt in range(self._retries):
                try:
                    self._conn.commit()
                    return
                except sqlite3.OperationalError as exc:
                    if _is_locked_error(exc) and attempt < self._retries - 1:
                        logger.warning(
                            '[%s] commit() locked, retrying (%d/%d)',
                            self._name, attempt + 1, self._retries,
                        )
                        time.sleep(_BACKOFF_FACTOR * (attempt + 1))
                        continue
                    logger.warning(
                        '[%s] commit() failed after %d attempts, '
                        'rolling back',
                        self._name, attempt + 1,
                    )
                    self._rollback_quietly()
                    raise
        finally:
            self._lock.release()

    def rollback(self) -> None:
        """Roll back the current transaction (never raises)."""
        self._acquire_lock('rollback')
        try:
            self._rollback_quietly()
        finally:
            self._lock.release()

    def close(self) -> None:
        """Close the underlying connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Context manager — broader atomic scope
    # ------------------------------------------------------------------

    def __enter__(self) -> SafeConnection:
        """Acquire the lock for a broader read-modify-write span."""
        self._acquire_lock('__enter__')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Release the lock; auto-rollback if an exception occurred."""
        if exc_type is not None:
            if self._debug:
                logger.debug(
                    '[%s] __exit__ with exception %s, rolling back',
                    self._name, exc_type.__name__,
                )
            self._rollback_quietly()
        self._lock.release()
        return False  # never swallow exceptions

    # ------------------------------------------------------------------
    # Property pass-throughs
    # ------------------------------------------------------------------

    @property
    def row_factory(self):
        """Row factory of the underlying connection."""
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    @property
    def in_transaction(self) -> bool:
        """Whether the underlying connection has an open transaction."""
        return self._conn.in_transaction

    @property
    def total_changes(self) -> int:
        """Total number of rows modified since the connection was opened."""
        return self._conn.total_changes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rollback_quietly(self) -> None:
        """Roll back without raising — used in error-recovery paths."""
        try:
            self._conn.rollback()
        except Exception:
            pass

    def __repr__(self) -> str:
        return f'<SafeConnection {self._name!r} wrapping {self._conn!r}>'
