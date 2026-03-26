"""Shared GPU utility functions for the Photonarium pipeline.

This leaf module contains pure-Python helpers used across GPU-related modules.
It imports only the standard library so it can never introduce circular-import
issues.
"""

from __future__ import annotations


def is_cuda_error(exc: Exception) -> bool:
    """Check if an exception is a CUDA/GPU error (OOM, context lost, driver reset, etc.).

    Used across the codebase to decide whether a RuntimeError should be
    handled as a GPU failure (with recovery logic) or re-raised as a
    genuine programming error.  Matches:

    - ``MemoryError`` (Python-level allocation failure)
    - ``RuntimeError`` containing ``'out of memory'`` (PyTorch OOM)
    - ``RuntimeError`` containing ``'cuda'`` (context corruption, driver
      reset, device lost, kernel errors, etc.)

    Args:
        exc: The caught exception.

    Returns:
        True if the exception is GPU/CUDA-related.
    """
    if isinstance(exc, MemoryError):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return 'out of memory' in msg or 'cuda' in msg
    return False
