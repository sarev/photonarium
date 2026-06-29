"""The compute arbiter: a single owner thread funnelling all model work.

All model inference flows through one thread that holds the device and owns the
resident model set.  Consumers submit a *job* (one batch/tile of work, naming
the model it needs) and block on the result.  One job runs at a time, so
execution collisions are impossible by construction; contention manifests as
*latency*, never as a "busy" error.

This mirrors the single-writer ``SafeConnection`` design that eliminated
``SQLITE_BUSY``: one owner, one queue, orderly waiting.

Determinism: given a fixed arrival order of submissions, the scheduler's
decisions are fully determined.  Non-determinism enters only via real-thread
arrival jitter at the edges — which is exactly what the stress tests target.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from .backend import ArbiterError, ArbiterShutdown, DeviceBackend, HealthSink, InsufficientMemory, Priority
from .residency import ResidencyManager


class _Job:
    """A single unit of GPU work submitted to the arbiter."""

    __slots__ = ('fn', 'future', 'model_key', 'priority', 'seq')

    def __init__(
        self,
        priority: Priority,
        seq: int,
        model_key: str | None,
        fn: Callable[[Any], Any],
        future: Future,
    ) -> None:
        self.priority = priority
        self.seq = seq
        self.model_key = model_key
        self.fn = fn
        self.future = future


class ComputeArbiter:
    """Serialises and prioritises all model work on a single owner thread."""

    def __init__(
        self,
        backend: DeviceBackend,
        health: HealthSink,
        *,
        idle_evict_s: float = 30.0,
        bulk_starvation_limit: int = 8,
        clock: Callable[[], float] = time.monotonic,
        debug: bool = False,
    ) -> None:
        """Start the arbiter and its owner thread.

        Args:
            backend: Device backend (load/run/evict/free_memory).
            health: Health authority; receives genuine failures only.
            idle_evict_s: Evict resident models idle longer than this (<=0 off).
            bulk_starvation_limit: Serve a waiting BULK job after at most this
                many consecutive INTERACTIVE jobs (forward-progress guarantee).
            clock: Monotonic time source (injectable for deterministic tests).
            debug: When True, assert residency invariants after every job.
        """
        self._backend = backend
        self._health = health
        self._res = ResidencyManager(backend, clock=clock)
        self._idle_evict_s = idle_evict_s
        self._starvation_limit = max(1, int(bulk_starvation_limit))
        self._debug = debug

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queues: dict[Priority, deque[_Job]] = {p: deque() for p in Priority}
        self._seq = 0
        self._consec_interactive = 0
        self._stopping = False

        # Observability counters (read under lock).
        self._stats = {'served': 0, 'oom_events': 0, 'cpu_fallbacks': 0}
        # Debug-mode invariant breach record (None == healthy). Recorded rather
        # than raised, so a violation never kills the owner thread.
        self._invariant_violation: str | None = None

        self._thread = threading.Thread(target=self._run, name='compute-arbiter', daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API (the narrow seam Photonarium sees)
    # ------------------------------------------------------------------
    def register(self, model_key: str, loader: Callable[[], Any], est_cost: int) -> None:
        """Register a model's loader and estimated memory cost.

        Call once at startup, before submitting work for ``model_key``.
        """
        with self._lock:
            self._res.register(model_key, loader, est_cost)

    def run(self, model_key: str, fn: Callable[[Any], Any], priority: Priority = Priority.BULK) -> Any:
        """Submit one batch/tile of work and block until it completes.

        ``fn`` is handed a guaranteed-resident model; whether that involved a
        (re)load is invisible to the caller.  Raises whatever ``fn`` raises, or
        :class:`ArbiterShutdown` if the arbiter is stopping.
        """
        # A job's ``fn`` running on the owner thread must never re-submit work:
        # it would block waiting for a turn the owner thread can never give.
        if threading.current_thread() is self._thread:
            raise ArbiterError('reentrant arbiter.run() from within a job would deadlock')

        future: Future = Future()
        with self._cv:
            if self._stopping:
                raise ArbiterShutdown('arbiter is shutting down')
            self._seq += 1
            job = _Job(priority, self._seq, model_key, fn, future)
            self._queues[priority].append(job)
            self._cv.notify()
        # Block on the OS-level future — zero CPU, woken when the owner thread
        # sets the result.  No polling, no busy-wait.
        return future.result()

    def run_exclusive(self, fn: Callable[[], Any], priority: Priority = Priority.BULK) -> Any:
        """Serialise + prioritise a GPU callable the arbiter does NOT own.

        For consumers whose model lifecycle is managed elsewhere (e.g. a
        pipeline stage that constructs its own model).  ``fn`` takes no model
        and is run on the owner thread, so any lazy model load performed inside
        it is serialised against all other GPU work too — preventing concurrent
        loads, the main out-of-memory risk.

        Unlike :meth:`run`, the arbiter performs no residency management for
        these models and leaves error/health handling to the caller (it simply
        propagates whatever ``fn`` raises).  Converting these consumers to
        registered (owned) models is the eviction-era follow-up.
        """
        if threading.current_thread() is self._thread:
            raise ArbiterError('reentrant arbiter call from within a job would deadlock')

        future: Future = Future()
        with self._cv:
            if self._stopping:
                raise ArbiterShutdown('arbiter is shutting down')
            self._seq += 1
            # model_key=None marks an exclusive (borrowed) job; the stored fn
            # ignores the (absent) model argument.
            job = _Job(priority, self._seq, None, lambda _m: fn(), future)
            self._queues[priority].append(job)
            self._cv.notify()
        return future.result()

    def status(self) -> dict[str, Any]:
        """Snapshot of arbiter state for ``/api/status`` and diagnostics."""
        with self._lock:
            return {
                'device': self._backend.device,
                'queued': {p.name: len(q) for p, q in self._queues.items()},
                'resident': self._res.resident_keys(),
                'loads': self._res.loads,
                'evictions': self._res.evictions,
                'stopping': self._stopping,
                'invariant_violation': self._invariant_violation,
                **self._stats,
            }

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop accepting work, fail anything pending, evict models, join thread."""
        with self._cv:
            if self._stopping:
                return
            self._stopping = True
            self._cv.notify_all()
        self._thread.join(timeout)

    # ------------------------------------------------------------------
    # Owner thread
    # ------------------------------------------------------------------
    def _run(self) -> None:
        """Owner-thread main loop: pick the best job, serve it, repeat."""
        while True:
            job = self._next_job()
            if job is None:  # shutting down
                break
            self._serve(job)
        self._drain()

    def _next_job(self) -> _Job | None:
        """Select the next job by priority with a BULK forward-progress guard.

        Returns None when the arbiter is stopping.
        """
        with self._cv:
            while True:
                if self._stopping:
                    return None
                inter = self._queues[Priority.INTERACTIVE]
                bulk = self._queues[Priority.BULK]
                if not inter and not bulk:
                    self._cv.wait()
                    continue
                # Forward-progress: if BULK is waiting and we've served the
                # starvation limit of consecutive INTERACTIVE jobs (or there is
                # no INTERACTIVE work), serve BULK next.
                if bulk and (not inter or self._consec_interactive >= self._starvation_limit):
                    self._consec_interactive = 0
                    return bulk.popleft()
                self._consec_interactive += 1
                return inter.popleft()

    def _serve(self, job: _Job) -> None:
        """Ensure the model is resident, run the job, propagate result/exception."""
        # Opportunistic idle sweep between jobs (frees memory pre-emptively).
        self._res.sweep_idle(self._idle_evict_s)

        # Exclusive (borrowed) job: serialise + prioritise execution only; the
        # caller owns the model lifecycle and its own error/health handling.
        if job.model_key is None:
            try:
                job.future.set_result(self._backend.run(job.fn, None))
            except BaseException as exc:  # propagate to caller; owner thread survives
                job.future.set_exception(exc)
            finally:
                with self._lock:
                    self._stats['served'] += 1
            return

        try:
            model = self._ensure_with_fallback(job.model_key)
        except BaseException as exc:  # must reach the caller, never the owner thread
            job.future.set_exception(exc)
            return

        self._res.pin(job.model_key)
        try:
            result = self._backend.run(job.fn, model)
            job.future.set_result(result)
        except BaseException as exc:  # propagate to caller; owner thread survives
            if self._backend.is_oom_error(exc):
                with self._lock:
                    self._stats['oom_events'] += 1
                # A genuine failure during execution — report it (busy != failed).
                self._health.report_failure(job.model_key)
            job.future.set_exception(exc)
        finally:
            self._res.unpin(job.model_key)
            with self._lock:
                self._stats['served'] += 1

        if self._debug and not self._res.invariant_ok():
            with self._lock:
                self._invariant_violation = 'pinned model not resident'

    def _ensure_with_fallback(self, model_key: str) -> Any:
        """Ensure residency; on a hard fit failure, degrade to CPU once.

        A model that will not fit even after evicting every idle model is a
        genuine memory failure: report it (which may flip the device to CPU)
        and retry once.  If it still fails, the exception reaches the caller.
        """
        try:
            return self._res.ensure(model_key)
        except InsufficientMemory:
            self._health.report_failure(model_key)
            with self._lock:
                self._stats['cpu_fallbacks'] += 1
            # Retry once on the (now possibly CPU) device.
            return self._res.ensure(model_key)

    def _drain(self) -> None:
        """Fail all pending jobs explicitly and release all models."""
        with self._cv:
            for q in self._queues.values():
                while q:
                    job = q.popleft()
                    job.future.set_exception(ArbiterShutdown('arbiter stopped before job ran'))
        self._res.evict_all()
