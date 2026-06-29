"""Compute arbiter — a single coordination point for all model/GPU work.

A sealed, self-contained component with **no inbound imports from the rest of
Photonarium**.  It serialises and prioritises model inference on one owner
thread, manages model residency within a memory budget, and turns contention
into orderly waiting (latency) rather than errors.

Public API (the narrow seam Photonarium consumes):

    from arbiter import ComputeArbiter, Priority

    arbiter = ComputeArbiter(backend, health)
    arbiter.register('openclip', loader=make_openclip, est_cost=...)
    result = arbiter.run('openclip', lambda m: m.encode(x), priority=Priority.INTERACTIVE)

See ``snippets/compute-arbiter-design.md`` for the full specification.
"""

from __future__ import annotations

from .backend import (
    ArbiterError,
    ArbiterShutdown,
    DeviceBackend,
    HealthSink,
    InsufficientMemory,
    Priority,
)
from .core import ComputeArbiter

__all__ = [
    'ArbiterError',
    'ArbiterShutdown',
    'ComputeArbiter',
    'DeviceBackend',
    'HealthSink',
    'InsufficientMemory',
    'Priority',
]
