"""Real (torch-backed) device backend for the compute arbiter.

This is Photonarium *integration* code: it bridges the sealed ``arbiter``
package to ``torch`` and the ``GpuHealth`` authority.  It deliberately lives
OUTSIDE ``app/arbiter/`` so the arbiter package keeps its one-way dependency
rule — the sealed core (``arbiter.core``/``residency``/``backend``) never imports
torch or any Photonarium module.  The dependency arrow stays ``Photonarium ->
arbiter``.

``TorchBackend`` satisfies the structural ``arbiter.DeviceBackend`` protocol and
``GpuHealth`` already satisfies ``arbiter.HealthSink`` (it exposes ``device`` and
``report_failure``), so no health adapter is needed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import torch

from gputil import is_oom_error

# Sentinel "effectively unlimited" memory, used for CPU or when free VRAM
# cannot be queried.  Large enough that the residency manager never evicts on
# its account, small enough to stay well clear of overflow.
_UNLIMITED = 1 << 62


class TorchBackend:
    """Device backend over ``torch`` + ``GpuHealth`` for the compute arbiter."""

    def __init__(self, gpu_health: Any) -> None:
        """Initialise the backend.

        Args:
            gpu_health: The shared ``GpuHealth`` instance (also used directly as
                the arbiter's health sink).
        """
        self._health = gpu_health

    @property
    def device(self) -> str:
        """The active device, as decided by the health authority."""
        return self._health.device

    def load(self, key: str, loader: Callable[[], Any]) -> Any:
        """Construct/fetch a model via ``loader``.

        Existing Photonarium model classes load themselves lazily and place
        themselves on the active device, so the loader simply returns the
        (singleton) model instance.
        """
        return loader()

    def run(self, fn: Callable[[Any], Any], model: Any) -> Any:
        """Execute one batch/tile of work against the resident model."""
        return fn(model)

    def evict(self, key: str, model: Any) -> None:
        """Best-effort release of a model and its device cache.

        Full per-model unload arrives with the residency retrofit; today the
        arbiter is configured not to evict resident models, so this is a safety
        net rather than a hot path.  Eviction must never raise.
        """
        with contextlib.suppress(Exception):
            unload = getattr(model, 'unload', None)
            if callable(unload):
                unload()
        self._empty_cache()

    def free_memory(self) -> int:
        """Return free device memory in bytes (or a large sentinel on CPU)."""
        with contextlib.suppress(Exception):
            if torch.cuda.is_available():
                free, _total = torch.cuda.mem_get_info()
                return int(free)
        return _UNLIMITED

    def is_oom_error(self, exc: BaseException) -> bool:
        """Classify ``exc`` as an out-of-memory failure (reuses gputil's helper)."""
        return isinstance(exc, Exception) and is_oom_error(exc)

    @staticmethod
    def _empty_cache() -> None:
        """Release cached CUDA memory, if any (best-effort)."""
        with contextlib.suppress(Exception):
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
