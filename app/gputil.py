"""Shared GPU utility functions and health tracking for Photonarium.

This leaf module contains pure-Python helpers used across GPU-related modules.
It avoids importing application modules so it can never introduce circular-import
issues.  ``torch`` is imported lazily inside functions that need it.

GPU Error Classification
------------------------
Two helpers classify GPU exceptions:

- ``is_gpu_error(exc)`` — True for *any* GPU/accelerator error (OOM, context
  corruption, driver reset) across all backends (CUDA, MPS, Intel XPU/IPEX).
  Used in ``except`` clauses to decide whether to handle or re-raise.

- ``is_oom_error(exc)`` — True specifically for out-of-memory errors.  OOM is
  transient (GPU works but is full) and handled with single-item fallback.
  Context/driver errors are non-transient and trigger the ``GpuHealth`` state
  machine for CPU fallback.

GPU Health Tracking
-------------------
``GpuHealth`` is a centralised state machine that tracks GPU availability:

    gpu → (first failure + retry) → (second failure) → cpu_fallback → (cpu failure) → disabled

A single instance lives on ``ImageDatabase`` and is shared by all models.
Models call ``gpu_health.device`` for device selection and
``gpu_health.report_failure(feature)`` on context/driver errors.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# =========================================================================
# Error Classification
# =========================================================================

# Keywords that identify GPU/accelerator errors across all backends.
# Matched case-insensitively against RuntimeError messages.
_GPU_ERROR_KEYWORDS = (
    'out of memory',  # All backends (CUDA, MPS, XPU)
    'cuda',  # NVIDIA CUDA errors
    'mps',  # Apple Metal Performance Shaders errors
    'xpu',  # Intel XPU errors
    'ipex',  # Intel Extension for PyTorch errors
    'native api failed',  # Intel XPU driver errors
    'dpcpp',  # Intel oneAPI DPC++ runtime errors
)


def is_gpu_error(exc: Exception) -> bool:
    """Check if an exception is a GPU/accelerator error.

    Covers all supported backends: CUDA (NVIDIA), MPS (Apple Silicon),
    XPU/IPEX (Intel).  Matches by exception class where available and
    by keyword in the error message as a fallback.

    Args:
        exc: The caught exception.

    Returns:
        True if the exception is GPU/accelerator-related.
    """
    if isinstance(exc, MemoryError):
        return True
    # torch.OutOfMemoryError is a RuntimeError subclass — catch by class
    # when available.  Present on CUDA builds; may exist on others.
    try:
        import torch

        if isinstance(exc, torch.OutOfMemoryError):
            return True
    except (ImportError, AttributeError):
        pass
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return any(k in msg for k in _GPU_ERROR_KEYWORDS)
    return False


def is_oom_error(exc: Exception) -> bool:
    """Check if an exception is specifically an out-of-memory error.

    OOM is transient (GPU works but is full) — handled with single-item
    fallback and backend logging only, no user notification.  Context and
    driver errors (``is_gpu_error`` True but ``is_oom_error`` False) mean
    the GPU is broken and trigger the ``GpuHealth`` fallback state machine.

    Args:
        exc: The caught exception.

    Returns:
        True if the exception is an OOM error.
    """
    if isinstance(exc, MemoryError):
        return True
    try:
        import torch

        if isinstance(exc, torch.OutOfMemoryError):
            return True
    except (ImportError, AttributeError):
        pass
    if isinstance(exc, RuntimeError):
        return 'out of memory' in str(exc).lower()
    return False


# =========================================================================
# GPU Health State Machine
# =========================================================================

# Valid states
STATE_GPU = 'gpu'
STATE_CPU_FALLBACK = 'cpu_fallback'
STATE_DISABLED = 'disabled'


class GpuHealth:
    """Centralised GPU availability tracker with automatic CPU fallback.

    State machine::

        gpu ──(first failure)──► gpu (retry once)
             ──(second failure)──► cpu_fallback (emit event, modal dialog)
        cpu_fallback ──(cpu failure)──► disabled (emit event, modal dialog)

    Thread-safe: all mutations go through ``_lock``.

    Args:
        event_callback: Optional callback ``fn(event_type, data_dict)``
            called on state transitions (e.g. to emit backend events).
    """

    def __init__(self, event_callback: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self._state = STATE_GPU
        self._affected_features: set[str] = set()
        self._lock = threading.Lock()
        self._event_callback = event_callback
        # Per-feature retry tracking: features that have had one failure
        # and are allowed one retry on the original device.
        self._retried_features: set[str] = set()

    @property
    def state(self) -> str:
        """Current state: 'gpu', 'cpu_fallback', or 'disabled'."""
        return self._state

    @property
    def device(self) -> str:
        """Get the current best available device.

        Returns ``'cpu'`` if the GPU has failed over.  All backend
        availability checks are guarded with ``hasattr``/``try-except``
        so this works on any torch build (CUDA-only, MPS-only, XPU-only,
        CPU-only).
        """
        if self._state != STATE_GPU:
            return 'cpu'

        import torch

        if torch.cuda.is_available():
            return 'cuda'
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            return 'mps'
        try:
            if hasattr(torch, 'xpu') and torch.xpu.is_available():
                return 'xpu'
        except Exception:
            pass
        return 'cpu'

    def report_failure(self, feature: str) -> str:
        """Report a non-OOM GPU failure for a feature.

        Call this when ``is_gpu_error(exc)`` is True and ``is_oom_error(exc)``
        is False (i.e. a context or driver error, not just out of memory).

        Returns the device the caller should use for retry:
        - Same device on first failure (one retry allowed)
        - ``'cpu'`` after second failure (CPU fallback)
        - ``'cpu'`` if already in cpu_fallback (caller should try CPU)

        If CPU also fails, call ``report_failure`` again — this transitions
        to ``'disabled'`` and the caller should give up.

        Args:
            feature: Human-readable feature name (e.g. ``'search'``,
                ``'embeddings'``, ``'scoring'``, ``'faces'``,
                ``'captions'``, ``'transcription'``).

        Returns:
            Device string to use for retry (``'cuda'``, ``'mps'``,
            ``'xpu'``, or ``'cpu'``).
        """
        with self._lock:
            if self._state == STATE_GPU:
                if feature not in self._retried_features:
                    # First failure — allow one retry on the same device
                    self._retried_features.add(feature)
                    logger.warning(f'GPU error in {feature} — will retry once on {self.device}')
                    return self.device
                # Second failure — fall back to CPU
                self._state = STATE_CPU_FALLBACK
                self._affected_features.add(feature)
                logger.error(f'GPU error in {feature} persists after retry — falling back to CPU')
                self._emit(
                    'gpu_state_changed',
                    {
                        'state': STATE_CPU_FALLBACK,
                        'features': sorted(self._affected_features),
                    },
                )
                return 'cpu'

            if self._state == STATE_CPU_FALLBACK:
                # CPU also failed — disable the feature
                self._state = STATE_DISABLED
                self._affected_features.add(feature)
                logger.error(f'CPU fallback also failed for {feature} — feature disabled (restart server to retry)')
                self._emit(
                    'gpu_state_changed',
                    {
                        'state': STATE_DISABLED,
                        'features': sorted(self._affected_features),
                    },
                )
                return 'cpu'

            # Already disabled
            self._affected_features.add(feature)
            return 'cpu'

    def get_status(self) -> dict[str, Any]:
        """Get the current GPU health status for the ``/api/status`` endpoint.

        Returns:
            Dict with ``'state'`` and ``'affected_features'`` keys.
        """
        return {
            'state': self._state,
            'affected_features': sorted(self._affected_features),
        }

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event via the registered callback (if any)."""
        if self._event_callback is not None:
            try:
                self._event_callback(event_type, data)
            except Exception:
                logger.debug('Failed to emit GPU health event', exc_info=True)
