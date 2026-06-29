"""Model residency management for the compute arbiter.

Owns which models are loaded in device memory, which are pinned (in use right
now and therefore un-evictable), and the LRU ordering used to choose eviction
victims under memory pressure.

THREADING: this class is **not** thread-safe and is touched only by the
arbiter's single owner thread (the one exception is :meth:`register`, which is
guarded by the arbiter's lock).  Single ownership is what makes load/evict
race-free by construction — the GPU analogue of the single-writer DB design.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .backend import DeviceBackend, InsufficientMemory


class ResidencyManager:
    """Tracks resident models and enforces the memory budget via LRU eviction."""

    def __init__(self, backend: DeviceBackend, *, clock: Callable[[], float]) -> None:
        """Initialise the manager.

        Args:
            backend: Device backend used to load/evict and query free memory.
            clock: Monotonic time source (injectable for deterministic tests).
        """
        self._backend = backend
        self._clock = clock
        self._resident: dict[str, Any] = {}
        self._pinned: set[str] = set()
        self._last_used: dict[str, float] = {}
        self._loaders: dict[str, Callable[[], Any]] = {}
        self._est_cost: dict[str, int] = {}
        # Observability counters.
        self.loads = 0
        self.evictions = 0

    # ------------------------------------------------------------------
    # Registration (called at startup, guarded by the arbiter lock)
    # ------------------------------------------------------------------
    def register(self, key: str, loader: Callable[[], Any], est_cost: int) -> None:
        """Register a model's loader and estimated memory cost."""
        self._loaders[key] = loader
        self._est_cost[key] = int(est_cost)

    def is_registered(self, key: str) -> bool:
        """Whether ``key`` has a registered loader."""
        return key in self._loaders

    # ------------------------------------------------------------------
    # Residency (owner thread only)
    # ------------------------------------------------------------------
    def ensure(self, key: str) -> Any:
        """Return a resident model for ``key``, loading (and evicting to fit) if needed.

        Raises:
            KeyError: if ``key`` was never registered.
            InsufficientMemory: if the model will not fit even after evicting
                every idle (un-pinned) model.
        """
        if key in self._resident:
            return self._resident[key]
        if key not in self._loaders:
            raise KeyError(f'model {key!r} is not registered with the arbiter')

        need = self._est_cost[key]
        # Evict idle models LRU-first until the incoming model fits.  Pinned
        # models (in use this instant) are never candidates.
        while self._backend.free_memory() < need:
            victim = self._lru_idle_victim()
            if victim is None:
                raise InsufficientMemory(
                    f'model {key!r} needs {need} but only {self._backend.free_memory()} '
                    f'is free and no idle models remain to evict',
                )
            self._evict(victim)

        model = self._backend.load(key, self._loaders[key])
        self._resident[key] = model
        self._last_used[key] = self._clock()
        self.loads += 1
        return model

    def pin(self, key: str) -> None:
        """Mark ``key`` as in use — it cannot be evicted until unpinned."""
        self._pinned.add(key)

    def unpin(self, key: str) -> None:
        """Release the pin on ``key`` and stamp it as most-recently used."""
        self._pinned.discard(key)
        self._last_used[key] = self._clock()

    def sweep_idle(self, max_idle_s: float) -> None:
        """Evict resident, un-pinned models untouched for longer than ``max_idle_s``.

        Frees memory proactively so the next load is less likely to need an
        on-demand eviction.  A non-positive threshold disables the sweep.
        """
        if max_idle_s <= 0:
            return
        now = self._clock()
        stale = [
            key
            for key in self._resident
            if key not in self._pinned and (now - self._last_used.get(key, 0.0)) >= max_idle_s
        ]
        for key in stale:
            self._evict(key)

    def evict_all(self) -> None:
        """Evict every resident model (used on shutdown)."""
        for key in list(self._resident):
            self._evict(key)

    # ------------------------------------------------------------------
    # Observability / invariants
    # ------------------------------------------------------------------
    def resident_keys(self) -> list[str]:
        """Snapshot of currently-resident model keys."""
        return list(self._resident)

    def invariant_ok(self) -> bool:
        """Debug-mode check: every pinned model is resident."""
        return self._pinned.issubset(self._resident.keys())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _lru_idle_victim(self) -> str | None:
        """Return the least-recently-used un-pinned resident model, or None."""
        idle = [key for key in self._resident if key not in self._pinned]
        if not idle:
            return None
        return min(idle, key=lambda k: self._last_used.get(k, 0.0))

    def _evict(self, key: str) -> None:
        """Evict a single model and free its memory."""
        model = self._resident.pop(key)
        self._last_used.pop(key, None)
        self._backend.evict(key, model)
        self.evictions += 1
