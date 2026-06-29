"""Seam interfaces and shared types for the compute arbiter.

The arbiter depends ONLY on the protocols defined here — never on Photonarium
modules or on ``torch`` directly.  The dependency arrow points one way:
``Photonarium -> arbiter``.  This is what keeps the arbiter a sealed box that
can be hardened standalone and grafted in through a narrow seam.

Two seams:

* :class:`DeviceBackend` — loads/evicts models and runs inference on whatever
  accelerator is in use.  The real implementation wraps ``torch``; tests use a
  fake (see ``fake.py``).
* :class:`HealthSink` — the failure/health authority.  Photonarium's
  ``GpuHealth`` (``gputil.py``) satisfies this protocol; tests use a stub.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


class Priority(enum.IntEnum):
    """Scheduling priority.  Lower value is served first.

    INTERACTIVE work (search, on-demand detect/caption) must never wait behind
    more than one BULK batch; BULK work (pipeline stages, enhancement) yields by
    being submitted in small per-batch jobs.  A starvation guard in the arbiter
    still gives BULK guaranteed forward progress under sustained interactive
    load.
    """

    INTERACTIVE = 0
    BULK = 1


@runtime_checkable
class DeviceBackend(Protocol):
    """Loads/evicts models and executes inference on the active device.

    All methods are invoked **only** on the arbiter's single owner thread, so
    implementations need not be thread-safe with respect to each other.
    """

    @property
    def device(self) -> str:
        """The active device string (e.g. ``'cuda'`` or ``'cpu'``)."""
        ...

    def load(self, key: str, loader: Callable[[], Any]) -> Any:
        """Construct a model via ``loader`` and place it on the active device.

        May raise on out-of-memory; the arbiter classifies via
        :meth:`is_oom_error`.
        """
        ...

    def run(self, fn: Callable[[Any], Any], model: Any) -> Any:
        """Execute ``fn(model)`` (one batch/tile of work) and return its result."""
        ...

    def evict(self, key: str, model: Any) -> None:
        """Release a model and free its device memory."""
        ...

    def free_memory(self) -> int:
        """Return free device memory in arbitrary consistent units (e.g. bytes).

        On CPU-only backends this may be a large constant — there is no hard
        memory cliff, so eviction is largely unnecessary.
        """
        ...

    def is_oom_error(self, exc: BaseException) -> bool:
        """Classify ``exc`` as an out-of-memory / device-memory failure."""
        ...


@runtime_checkable
class HealthSink(Protocol):
    """The device-health authority (satisfied by Photonarium's ``GpuHealth``)."""

    @property
    def device(self) -> str:
        """The currently-recommended device."""
        ...

    def report_failure(self, feature: str) -> str:
        """Report a genuine device failure for ``feature``; return the new device.

        IMPORTANT: a *queue wait* is never a failure — only an exception raised
        during model load or inference is reported here.  Conflating "waited a
        while" with "failed" would wrongly trip CPU-fallback.
        """
        ...


class ArbiterError(Exception):
    """Base class for arbiter errors."""


class ArbiterShutdown(ArbiterError):
    """Raised to callers whose work was submitted to (or pending in) a stopping
    arbiter.  Surfaces explicitly — work is never silently dropped."""


class InsufficientMemory(ArbiterError):
    """A single model does not fit even after evicting every idle model.

    The arbiter catches this internally to attempt CPU fallback; it should not
    normally reach a caller.
    """
