"""Single-writer queue architecture for SQLite in Photonarium.

Eliminates ``database is locked`` errors by routing all writes through a
dedicated writer thread.  Reads go to a separate read-only connection
under WAL mode, allowing concurrent readers without blocking the writer.

Architecture::

    Flask threads  ──┐
    Pipeline       ──┤──► write_queue ──► Writer thread ──► write connection
    Log handler    ──┤    (queue.Queue)   (drains FIFO)
    Stage 1 workers──┘

    All threads ──────────────────────► Read connection (WAL concurrent readers)

Usage::

    from safeconn import SafeConnection

    # Create — opens read + write connections, starts writer thread
    safe = SafeConnection('/path/to/db.sqlite', name='shared')

    # Reads — routed to the read connection automatically
    row = safe.execute('SELECT * FROM images WHERE id = ?', (pk,)).fetchone()

    # Writes — routed to the writer queue, blocks until committed
    safe.execute('UPDATE images SET rating = ? WHERE id = ?', (5, pk))
    safe.commit()

    # Multi-statement transaction (atomic on writer thread)
    with safe:
        safe.execute('UPDATE images SET rating = ? WHERE id = ?', (5, pk))
        safe.execute('DELETE FROM faces WHERE image_id = ?', (pk,))
        safe.commit()
    # __exit__ auto-rolls-back if an exception escaped the block.

    # Arbitrary write function (complex operations on writer thread)
    def migrate(conn):
        conn.execute('ALTER TABLE ...')
        conn.execute('INSERT INTO ...')
        conn.commit()
    safe.write_fn(migrate)

    # Fire-and-forget (log handler only — no result, no blocking)
    safe.write_fn_async(lambda conn: (conn.executemany(..., batch), conn.commit()))

Design
------
- **Single writer**: one ``sqlite3.Connection`` for writes, accessed only
  by the writer thread.  No cross-connection contention, no ``SQLITE_BUSY``,
  no ``SQLITE_BUSY_SNAPSHOT``, no retry logic needed.
- **Concurrent readers**: a separate read-only connection (``PRAGMA
  query_only=ON``) with a ``threading.Lock`` for thread safety.  WAL mode
  allows readers to proceed without blocking the writer.
- **Automatic routing**: ``execute()`` inspects the SQL prefix to decide
  whether to route to the reader or writer.  Callers don't need to change.
- **Context manager**: ``with safe:`` routes all enclosed operations
  (reads and writes) to the writer thread atomically via a per-transaction
  sub-queue.  Re-entrant — nested ``with`` blocks are no-ops.
- **Error propagation**: write errors are captured on the writer thread
  and re-raised on the calling thread.  The writer auto-rolls-back after
  any error to keep the connection clean.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar('T')

# SQL prefixes that are read-only.  Everything else routes to the writer.
# Conservative — routing a read to the writer is correct (just slower);
# routing a write to the reader fails loudly (PRAGMA query_only=ON).
_READ_PREFIXES = frozenset({'SELECT', 'WITH', 'EXPLAIN'})


class _WriteCursor:
    """Lightweight cursor proxy returned from write operations.

    Raw ``sqlite3.Cursor`` objects hold a reference to their parent
    connection.  Returning them from the writer thread to the calling
    thread means the cursor's ``__del__`` (and any ``fetchone`` /
    ``fetchall`` calls) would touch the writer connection from a
    non-writer thread, risking corruption of internal C-level state.

    This proxy snapshots the essential cursor attributes on the writer
    thread and presents the same read-only interface to the caller.
    Write operations rarely need fetched rows (INSERT/UPDATE/DELETE
    don't produce result sets), but we snapshot them for the uncommon
    ``INSERT ... RETURNING`` and ``PRAGMA`` patterns.
    """

    __slots__ = ('_rows', 'description', 'lastrowid', 'rowcount')

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        self.description = cursor.description
        # Eagerly fetch all rows — safe because write result sets are
        # tiny (usually empty, or a handful of RETURNING rows).
        self._rows = cursor.fetchall()

    def fetchone(self) -> Any:
        """Return the next row, or None."""
        if self._rows:
            return self._rows.pop(0)
        return None

    def fetchall(self) -> list:
        """Return all remaining rows."""
        rows = self._rows
        self._rows = []
        return rows

    def __iter__(self):
        return iter(self._rows)


# Sentinel placed on the write queue to trigger clean shutdown.
_SHUTDOWN = object()

# Sentinel placed on a transaction sub-queue to end the scope.
_TXN_END = object()

# Warn if a synchronous write takes longer than this (seconds).
_WRITE_WARN_THRESHOLD = 5.0

# Wait this long (ms) for a contended write lock before giving up.  The main
# data path is single-writer (this queue), so it never self-contends — but a few
# connections deliberately stay independent, notably the log handler, which
# keeps its own writer so it can still record even if the main writer is wedged.
# Independent *writers* serialise on SQLite's one file write-lock, so without a
# busy timeout the loser fails instantly with "database is locked" the moment two
# overlap.  A few seconds lets it wait for the other's (sub-millisecond) commit
# instead.  SQLite still returns immediately on a genuine deadlock, so this can
# never hang.
_BUSY_TIMEOUT_MS = 5000


def _is_read_only(sql: str) -> bool:
    """Return True if *sql* is a read-only statement.

    Uses a conservative heuristic: only SELECT, WITH, and EXPLAIN are
    treated as reads.  Everything else (INSERT, UPDATE, DELETE, PRAGMA,
    CREATE, DROP, BEGIN, COMMIT, etc.) is routed to the writer.
    """
    stripped = sql.lstrip()
    if not stripped:
        return True
    first_word = stripped.split(None, 1)[0].upper()
    return first_word in _READ_PREFIXES


@dataclass
class _WriteJob:
    """A unit of work submitted to the writer thread."""

    fn: Callable[[sqlite3.Connection], Any]
    event: threading.Event | None = None  # None = fire-and-forget
    result: Any = field(default=None, repr=False)
    exception: BaseException | None = field(default=None, repr=False)


class _TransactionScope:
    """Per-thread state for a ``with safe_conn:`` block.

    Operations inside the block are routed to a sub-queue that the writer
    thread drains while it holds the transaction open.  This guarantees
    atomicity — no other writes can interleave.
    """

    __slots__ = ('depth', 'done', 'ready', 'sub_queue')

    def __init__(self) -> None:
        self.sub_queue: queue.Queue = queue.Queue()
        self.ready = threading.Event()  # Set when writer enters the sub-loop
        self.done = threading.Event()  # Set when writer exits the sub-loop
        self.depth = 0  # Re-entrancy counter


class SafeConnection:
    """Single-writer queue architecture for SQLite.

    Reads are dispatched directly on a shared read-only connection.
    Writes are dispatched as callables to a dedicated writer thread via
    a :class:`queue.Queue`.  The caller blocks on a
    :class:`threading.Event` until the write completes (or fails).

    Args:
        db_path: Path to the SQLite database file.
        name: Human-readable label for log messages.
        pragmas: List of ``(pragma_name, value)`` tuples applied to both
            connections after creation (e.g. ``[('cache_size', '-102400')]``).
            ``journal_mode``, ``query_only``, and ``foreign_keys`` are
            managed internally and should not be included.
        row_factory: Row factory for both connections (default:
            ``sqlite3.Row`` for dict-like access).
    """

    def __init__(
        self,
        db_path: str,
        *,
        name: str = 'default',
        pragmas: list[tuple[str, str]] | None = None,
        row_factory: Any = sqlite3.Row,
    ) -> None:
        self._db_path = str(db_path)
        self._name = name
        self._closed = False
        self._pragmas = pragmas or []
        self._row_factory = row_factory

        # Thread-local state for transaction scoping
        self._local = threading.local()

        # -- Read connection (opened here, used by any thread via Lock) --
        self._read_conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
        )
        # Set busy_timeout first, so even enabling WAL waits for a momentary
        # lock (e.g. another instance starting up) instead of failing instantly.
        self._read_conn.execute(f'PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}')
        self._read_conn.execute('PRAGMA journal_mode=WAL')
        self._read_conn.execute('PRAGMA query_only=ON')
        for pragma_name, value in self._pragmas:
            self._read_conn.execute(f'PRAGMA {pragma_name}={value}')
        self._read_conn.row_factory = row_factory
        self._read_lock = threading.Lock()

        # -- Writer thread and queue --
        self._write_queue: queue.Queue = queue.Queue()
        self._writer_ready = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name=f'db-writer-{name}',
            daemon=False,  # Clean shutdown — drains queue before exiting
        )
        self._writer_thread.start()
        self._writer_ready.wait()
        logger.debug('[%s] SafeConnection ready (writer thread started)', name)

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        """Main loop for the writer thread.

        Creates the write connection, then drains the queue until a
        shutdown sentinel is received.  Each job is a callable that
        receives the raw ``sqlite3.Connection``.
        """
        # Create the write connection on this thread
        write_conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
        )
        # busy_timeout first (see read connection): wait for a contended write
        # lock rather than failing instantly when an independent writer (e.g. the
        # log handler) overlaps with this one.
        write_conn.execute(f'PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}')
        write_conn.execute('PRAGMA journal_mode=WAL')
        write_conn.execute('PRAGMA foreign_keys=ON')
        for pragma_name, value in self._pragmas:
            write_conn.execute(f'PRAGMA {pragma_name}={value}')
        write_conn.row_factory = self._row_factory
        self._writer_ready.set()

        while True:
            job = self._write_queue.get()
            if job is _SHUTDOWN:
                break

            try:
                job.result = job.fn(write_conn)
            except BaseException as exc:
                job.exception = exc
                # Auto-rollback to keep the connection clean for the
                # next job.  Without this, a failed INSERT leaves an
                # open transaction that poisons subsequent writes.
                try:
                    write_conn.rollback()
                except Exception:
                    pass
            finally:
                if job.event is not None:
                    job.event.set()

        # Clean up
        try:
            write_conn.close()
        except Exception:
            pass
        logger.debug('[%s] Writer thread exiting', self._name)

    # ------------------------------------------------------------------
    # Write submission helpers
    # ------------------------------------------------------------------

    def _submit_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Submit a write job and block until the writer thread completes it.

        Raises whatever exception the writer encountered, preserving the
        original traceback for the caller.
        """
        job: _WriteJob = _WriteJob(fn=fn, event=threading.Event())
        t0 = None
        if logger.isEnabledFor(logging.DEBUG):
            t0 = __import__('time').monotonic()

        self._write_queue.put(job)
        job.event.wait()

        if t0 is not None:
            elapsed = __import__('time').monotonic() - t0
            if elapsed >= _WRITE_WARN_THRESHOLD:
                logger.warning(
                    '[%s] write took %.2fs (queue depth may be high)',
                    self._name,
                    elapsed,
                )

        if job.exception is not None:
            raise job.exception
        return job.result

    def _submit_write_async(self, fn: Callable[[sqlite3.Connection], Any]) -> None:
        """Submit a write job without waiting.  Errors are logged, not raised."""

        def _wrapped(conn: sqlite3.Connection) -> None:
            try:
                fn(conn)
            except Exception:
                logger.exception('[%s] fire-and-forget write failed', self._name)
                try:
                    conn.rollback()
                except Exception:
                    pass

        job: _WriteJob = _WriteJob(fn=_wrapped, event=None)
        self._write_queue.put(job)

    # ------------------------------------------------------------------
    # Transaction scope (context manager)
    # ------------------------------------------------------------------

    def _get_txn_scope(self) -> _TransactionScope | None:
        """Return the current thread's active transaction scope, or None."""
        return getattr(self._local, 'txn_scope', None)

    def __enter__(self) -> SafeConnection:
        """Begin an atomic transaction scope on the writer thread.

        All ``execute``, ``executemany``, ``commit``, and ``rollback``
        calls within the ``with`` block are routed to the writer thread
        via a per-scope sub-queue.  This guarantees that no other writes
        interleave — the writer thread is exclusively serving this scope
        until ``__exit__``.

        Re-entrant: nested ``with safe:`` blocks from the same thread
        increment a depth counter and are otherwise no-ops.
        """
        scope = self._get_txn_scope()
        if scope is not None:
            # Re-entrant — the outer scope already owns the writer
            scope.depth += 1
            return self

        scope = _TransactionScope()
        self._local.txn_scope = scope

        # Submit a job that makes the writer thread enter a sub-queue
        # loop.  The writer blocks in this loop until we send _TXN_END,
        # so no other writes from the main queue can interleave.
        def _enter_txn_loop(conn: sqlite3.Connection) -> None:
            scope.ready.set()  # Signal that the sub-loop is running
            while True:
                sub_job = scope.sub_queue.get()
                if sub_job is _TXN_END:
                    break
                try:
                    sub_job.result = sub_job.fn(conn)
                except BaseException as exc:
                    sub_job.exception = exc
                    # Don't auto-rollback here — let the caller decide
                    # (they may want to handle the error and continue).
                finally:
                    if sub_job.event is not None:
                        sub_job.event.set()
            scope.done.set()

        # The job has no event — the writer blocks inside the loop,
        # and we wait on scope.ready instead.
        job = _WriteJob(fn=_enter_txn_loop, event=None)
        self._write_queue.put(job)

        if not scope.ready.wait(timeout=30.0):
            self._local.txn_scope = None
            raise RuntimeError(
                f'[{self._name}] Writer thread failed to enter transaction scope within 30s — possible deadlock',
            )

        return self

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> bool:
        """End the transaction scope.

        Auto-rolls-back if an exception escaped the ``with`` block.
        Signals the writer thread to exit the sub-queue loop and resume
        the main queue.
        """
        scope = self._get_txn_scope()
        if scope is None:
            return False

        if scope.depth > 0:
            # Re-entrant exit — decrement, don't end the outer scope
            scope.depth -= 1
            return False

        # Auto-rollback on exception
        if exc_type is not None:
            logger.debug(
                '[%s] __exit__ with exception %s, rolling back',
                self._name,
                exc_type.__name__,
            )
            try:
                self._txn_submit(scope, lambda conn: conn.rollback())
            except Exception:
                pass

        # Signal the writer to exit the sub-loop
        scope.sub_queue.put(_TXN_END)
        scope.done.wait(timeout=30.0)
        self._local.txn_scope = None

        return False  # Never swallow exceptions

    def _txn_submit(self, scope: _TransactionScope, fn: Callable) -> Any:
        """Submit a job to the transaction scope's sub-queue and wait."""
        job: _WriteJob = _WriteJob(fn=fn, event=threading.Event())
        scope.sub_queue.put(job)
        job.event.wait()
        if job.exception is not None:
            raise job.exception
        return job.result

    # ------------------------------------------------------------------
    # Public API — reads
    # ------------------------------------------------------------------

    def execute(self, sql: str, parameters: tuple | list = ()) -> sqlite3.Cursor | _WriteCursor:
        """Execute a single SQL statement.

        Automatically routes to the read connection (for SELECT/WITH) or
        the writer thread (for INSERT/UPDATE/DELETE/etc.).  Inside a
        ``with`` block, all operations go to the writer thread to
        maintain transaction isolation.

        Write operations return a :class:`_WriteCursor` proxy instead of
        a raw ``sqlite3.Cursor`` — this avoids cross-thread cursor
        finalisation issues.  The proxy has the same ``fetchone``,
        ``fetchall``, ``lastrowid``, ``rowcount``, and ``description``
        interface.
        """
        scope = self._get_txn_scope()

        if scope is not None:
            # Inside a transaction scope — everything goes to the writer.
            # Wrap in _WriteCursor to avoid cross-thread cursor GC issues.
            return self._txn_submit(scope, lambda conn: _WriteCursor(conn.execute(sql, parameters)))

        if _is_read_only(sql):
            with self._read_lock:
                return self._read_conn.execute(sql, parameters)

        return self._submit_write(lambda conn: _WriteCursor(conn.execute(sql, parameters)))

    def executemany(self, sql: str, seq_of_parameters: Any) -> sqlite3.Cursor | _WriteCursor:
        """Execute a parameterised statement against a sequence of rows.

        Always routed to the writer thread (``executemany`` is inherently
        a write operation).  Returns a :class:`_WriteCursor` proxy.
        """
        scope = self._get_txn_scope()

        if scope is not None:
            return self._txn_submit(scope, lambda conn: _WriteCursor(conn.executemany(sql, seq_of_parameters)))

        return self._submit_write(lambda conn: _WriteCursor(conn.executemany(sql, seq_of_parameters)))

    def commit(self) -> None:
        """Commit the current transaction on the writer thread."""
        scope = self._get_txn_scope()

        if scope is not None:
            self._txn_submit(scope, lambda conn: conn.commit())
            return

        self._submit_write(lambda conn: conn.commit())

    def rollback(self) -> None:
        """Roll back the current transaction on the writer thread."""
        scope = self._get_txn_scope()

        if scope is not None:
            self._txn_submit(scope, lambda conn: conn.rollback())
            return

        self._submit_write(lambda conn: conn.rollback())

    # ------------------------------------------------------------------
    # Public API — write functions
    # ------------------------------------------------------------------

    def write_fn(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Execute an arbitrary callable on the writer thread.

        The callable receives the raw ``sqlite3.Connection`` and can
        perform any combination of reads and writes.  Blocks until the
        callable returns (or raises).

        Use this for complex multi-statement operations that need
        atomicity without the context-manager overhead::

            def migrate(conn):
                conn.execute('ALTER TABLE images ADD COLUMN foo TEXT')
                conn.execute('UPDATE images SET foo = bar')
                conn.commit()
            safe.write_fn(migrate)
        """
        scope = self._get_txn_scope()

        if scope is not None:
            return self._txn_submit(scope, fn)

        return self._submit_write(fn)

    def write_fn_async(self, fn: Callable[[sqlite3.Connection], None]) -> None:
        """Execute a callable on the writer thread without waiting.

        Errors are logged but not raised.  Use sparingly — only for
        background work where the caller genuinely does not need to know
        whether the write succeeded (e.g. log handler flush).
        """
        self._submit_write_async(fn)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Drain the write queue and close both connections.

        Blocks until all pending writes have completed and the writer
        thread has exited.  Safe to call multiple times.
        """
        if self._closed:
            return
        self._closed = True

        logger.debug('[%s] Closing SafeConnection (draining write queue)', self._name)

        # Signal writer thread to stop — it processes all pending jobs
        # before seeing the sentinel, so no writes are lost.
        self._write_queue.put(_SHUTDOWN)
        self._writer_thread.join(timeout=30.0)
        if self._writer_thread.is_alive():
            logger.warning(
                '[%s] Writer thread did not exit within 30s',
                self._name,
            )

        # Close read connection
        with self._read_lock:
            try:
                self._read_conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Property pass-throughs
    # ------------------------------------------------------------------

    @property
    def row_factory(self) -> Any:
        """Row factory applied to both connections."""
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._row_factory = value
        with self._read_lock:
            self._read_conn.row_factory = value
        # Update the write connection on its own thread
        self._submit_write(lambda conn: setattr(conn, 'row_factory', value))

    @property
    def in_transaction(self) -> bool:
        """Whether the write connection has an open transaction.

        Queries the writer thread synchronously — use sparingly (diagnostics only).
        """
        return self._submit_write(lambda conn: conn.in_transaction)

    @property
    def total_changes(self) -> int:
        """Total rows modified since the write connection was opened.

        Queries the writer thread synchronously — use sparingly (diagnostics only).
        """
        return self._submit_write(lambda conn: conn.total_changes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        state = 'closed' if self._closed else 'open'
        return f'<SafeConnection {self._name!r} ({state})>'
