"""In-memory fakes for hardening the arbiter without a GPU.

These model the device backend and health authority with deterministic,
injectable behaviour (settable memory budget, on-demand OOM / load failures,
device flips).  They are *bring-up scaffolding* — once the arbiter is integrated,
the permanent no-mock test vehicle is CPU mode running the real backend, and
this shrinks to a thin fault hook.

NOTE: methods are invoked only on the arbiter's single owner thread, so no
locking is required here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeOOM(RuntimeError):
    """Simulated out-of-memory / device-memory failure."""


class FakeModel:
    """A stand-in model carrying a memory ``cost`` and a call counter."""

    def __init__(self, key: str, cost: int) -> None:
        self.key = key
        self.cost = cost
        self.calls = 0


class FakeBackend:
    """A device backend backed by a settable integer memory budget."""

    def __init__(self, total_memory: int, *, device: str = 'cuda', cpu_memory: int = 10**15) -> None:
        self._total = int(total_memory)
        self._cpu_memory = int(cpu_memory)
        self._allocated = 0
        self._device = device
        # Observability / assertions.
        self.load_count: dict[str, int] = {}
        # Fault injection: pending OOMs to raise on load/run per key.
        self._oom_on_load: dict[str, int] = {}
        self._oom_on_run: dict[str, int] = {}

    # --- DeviceBackend protocol -------------------------------------------
    @property
    def device(self) -> str:
        return self._device

    def load(self, key: str, loader: Callable[[], Any]) -> Any:
        if self._oom_on_load.get(key, 0) > 0:
            self._oom_on_load[key] -= 1
            raise FakeOOM(f'injected OOM loading {key!r}')
        model = loader()
        # Model the hard memory cliff: a correct ResidencyManager will have
        # evicted enough first, so tripping this signals an arbiter bug.
        if self._allocated + model.cost > self._total:
            raise FakeOOM(
                f'OOM loading {key!r}: alloc {model.cost} + {self._allocated} > budget {self._total}',
            )
        self._allocated += model.cost
        self.load_count[key] = self.load_count.get(key, 0) + 1
        return model

    def run(self, fn: Callable[[Any], Any], model: Any) -> Any:
        # model is None for exclusive (borrowed) jobs that own no registered model.
        if model is not None:
            model.calls += 1
            if self._oom_on_run.get(model.key, 0) > 0:
                self._oom_on_run[model.key] -= 1
                raise FakeOOM(f'injected OOM running {model.key!r}')
        return fn(model)

    def evict(self, key: str, model: Any) -> None:
        self._allocated -= model.cost

    def free_memory(self) -> int:
        return self._total - self._allocated

    def is_oom_error(self, exc: BaseException) -> bool:
        return isinstance(exc, FakeOOM)

    # --- test controls ----------------------------------------------------
    @property
    def allocated(self) -> int:
        """Currently-allocated memory (0 after a clean shutdown == no leak)."""
        return self._allocated

    def set_device(self, device: str) -> None:
        """Flip the active device; CPU is treated as effectively unlimited."""
        self._device = device
        if device == 'cpu':
            self._total = self._cpu_memory

    def inject_oom_on_load(self, key: str, times: int = 1) -> None:
        self._oom_on_load[key] = times

    def inject_oom_on_run(self, key: str, times: int = 1) -> None:
        self._oom_on_run[key] = times


class FakeHealthSink:
    """A health authority that flips to CPU on the first reported failure."""

    def __init__(self, backend: FakeBackend) -> None:
        self._backend = backend
        self.failures: list[str] = []

    @property
    def device(self) -> str:
        return self._backend.device

    def report_failure(self, feature: str) -> str:
        self.failures.append(feature)
        self._backend.set_device('cpu')
        return 'cpu'
