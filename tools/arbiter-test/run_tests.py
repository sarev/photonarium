"""Deterministic unit tests for the compute arbiter (no GPU, no pytest).

Run with the project venv or any Python 3.10+:

    python tools/arbiter-test/run_tests.py

Exits non-zero if any test fails.  Uses the fake backend (``app/arbiter/fake.py``)
so it needs no torch and no accelerator.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

# Make the sealed package importable as ``arbiter`` (it lives in app/).
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.normpath(os.path.join(_HERE, '..', '..', 'app'))
sys.path.insert(0, _APP)

from arbiter import ArbiterError, ArbiterShutdown, ComputeArbiter, Priority  # noqa: E402
from arbiter.backend import InsufficientMemory  # noqa: E402
from fake import FakeBackend, FakeHealthSink, FakeModel, FakeOOM  # noqa: E402
from arbiter.residency import ResidencyManager  # noqa: E402

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


def make(total=1000, **kw):
    """Build an arbiter with a fake backend. Idle sweep off for determinism."""
    backend = FakeBackend(total)
    health = FakeHealthSink(backend)
    kw.setdefault('idle_evict_s', 0.0)
    kw.setdefault('debug', True)
    arb = ComputeArbiter(backend, health, **kw)
    return arb, backend, health


def loader(key, cost):
    return lambda: FakeModel(key, cost)


def wait_until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.002)
    return False


def assert_eq(a, b, msg=''):
    if a != b:
        raise AssertionError(f'{msg}: {a!r} != {b!r}')


# ---------------------------------------------------------------------------
# Residency / loading
# ---------------------------------------------------------------------------
@test
def test_basic_run_and_no_leak():
    arb, b, _ = make()
    arb.register('m', loader('m', 40), 40)
    assert_eq(arb.run('m', lambda mm: 7), 7, 'result')
    assert_eq(b.load_count['m'], 1, 'loaded once')
    arb.shutdown()
    assert_eq(b.allocated, 0, 'no leak after shutdown')
    assert arb.status()['invariant_violation'] is None


@test
def test_load_once_across_many_runs():
    arb, b, _ = make()
    arb.register('m', loader('m', 40), 40)
    for _ in range(5):
        arb.run('m', lambda mm: None)
    assert_eq(b.load_count['m'], 1, 'single load across 5 runs')
    arb.shutdown()


@test
def test_lru_eviction_and_transparent_reload():
    # Budget fits 2 of 3 equal-cost models.
    arb, b, _ = make(100)
    for k in ('a', 'b', 'c'):
        arb.register(k, loader(k, 40), 40)
    arb.run('a', lambda mm: None)              # resident: a
    arb.run('b', lambda mm: None)              # resident: a, b
    arb.run('c', lambda mm: None)              # evicts LRU idle (a); resident: b, c
    assert_eq(b.load_count, {'a': 1, 'b': 1, 'c': 1}, 'each loaded once so far')
    arb.run('a', lambda mm: 'ok')              # a was evicted -> transparent reload
    assert_eq(b.load_count['a'], 2, 'a reloaded after eviction')
    arb.shutdown()
    assert_eq(b.allocated, 0, 'no leak')


@test
def test_pinned_never_evicted():
    b = FakeBackend(80)
    rm = ResidencyManager(b, clock=lambda: 0.0)
    for k in ('a', 'b', 'c'):
        rm.register(k, loader(k, 40), 40)
    rm.ensure('a'); rm.pin('a')
    rm.ensure('b'); rm.pin('b')                # full: a, b both pinned
    try:
        rm.ensure('c')                          # nothing idle to evict
        raise AssertionError('expected InsufficientMemory')
    except InsufficientMemory:
        pass
    rm.unpin('a')                               # a now idle
    rm.ensure('c')                              # should evict a, not b
    assert_eq(set(rm.resident_keys()), {'b', 'c'}, 'evicted the idle (a), kept pinned (b)')


# ---------------------------------------------------------------------------
# Scheduling: priority + forward progress
# ---------------------------------------------------------------------------
def _occupy_owner(arb):
    """Start a blocking BULK job; return (gate, started) once it is running."""
    gate = threading.Event()
    started = threading.Event()

    def blocker(_m):
        started.set()
        gate.wait()

    threading.Thread(target=lambda: arb.run('x', blocker, Priority.BULK), daemon=True).start()
    assert started.wait(2.0), 'blocker did not start'
    return gate


@test
def test_interactive_preempts_bulk():
    arb, _, _ = make()
    arb.register('x', loader('x', 1), 1)
    results = []

    def job(name):
        return lambda _m: results.append(name)

    gate = _occupy_owner(arb)
    threading.Thread(target=lambda: arb.run('x', job('bulk'), Priority.BULK), daemon=True).start()
    threading.Thread(target=lambda: arb.run('x', job('inter'), Priority.INTERACTIVE), daemon=True).start()
    assert wait_until(lambda: sum(arb.status()['queued'].values()) == 2), 'two jobs queued'
    gate.set()
    assert wait_until(lambda: len(results) == 2), 'both jobs ran'
    assert_eq(results, ['inter', 'bulk'], 'interactive served before bulk')
    arb.shutdown()


@test
def test_bulk_forward_progress():
    # With a starvation limit of 3, a waiting BULK job must run after at most
    # 3 consecutive INTERACTIVE jobs even under a flood of interactive work.
    arb, _, _ = make(bulk_starvation_limit=3)
    arb.register('x', loader('x', 1), 1)
    results = []

    def job(name):
        return lambda _m: results.append(name)

    gate = _occupy_owner(arb)
    threading.Thread(target=lambda: arb.run('x', job('bulk'), Priority.BULK), daemon=True).start()
    for i in range(10):
        threading.Thread(target=lambda i=i: arb.run('x', job(f'i{i}'), Priority.INTERACTIVE), daemon=True).start()
    assert wait_until(lambda: sum(arb.status()['queued'].values()) == 11), 'all queued'
    gate.set()
    assert wait_until(lambda: len(results) == 11), 'all ran'
    assert results.index('bulk') <= 3, f'bulk starved (ran at index {results.index("bulk")})'
    arb.shutdown()


# ---------------------------------------------------------------------------
# Errors / shutdown / health
# ---------------------------------------------------------------------------
@test
def test_exception_propagates_and_arbiter_survives():
    arb, _, _ = make()
    arb.register('m', loader('m', 10), 10)

    def boom(_m):
        raise ValueError('boom')

    try:
        arb.run('m', boom)
        raise AssertionError('expected ValueError')
    except ValueError as e:
        assert_eq(str(e), 'boom', 'exception propagated')
    assert_eq(arb.run('m', lambda mm: 5), 5, 'arbiter survives a throwing job')
    arb.shutdown()


@test
def test_shutdown_fails_pending_and_rejects_new():
    arb, _, _ = make()
    arb.register('x', loader('x', 1), 1)
    captured = {}

    gate = _occupy_owner(arb)

    def pending():
        try:
            arb.run('x', lambda _m: None, Priority.BULK)
        except BaseException as e:  # noqa: BLE001
            captured['err'] = e

    threading.Thread(target=pending, daemon=True).start()
    assert wait_until(lambda: arb.status()['queued']['BULK'] == 1), 'job pending'

    threading.Thread(target=arb.shutdown, daemon=True).start()
    time.sleep(0.05)            # let shutdown set the stopping flag
    gate.set()                  # release the in-flight blocker so the owner can drain
    assert wait_until(lambda: 'err' in captured), 'pending job resolved'
    assert isinstance(captured['err'], ArbiterShutdown), f'got {captured["err"]!r}'

    try:
        arb.run('x', lambda _m: None)
        raise AssertionError('expected ArbiterShutdown for new work')
    except ArbiterShutdown:
        pass


@test
def test_oom_during_run_reports_failure():
    arb, b, h = make()
    arb.register('m', loader('m', 10), 10)
    b.inject_oom_on_run('m', 1)
    try:
        arb.run('m', lambda mm: 1)
        raise AssertionError('expected FakeOOM')
    except FakeOOM:
        pass
    assert_eq(h.failures, ['m'], 'failure reported to health')
    assert_eq(arb.status()['oom_events'], 1, 'oom counted')
    arb.shutdown()


@test
def test_cpu_fallback_when_model_too_big():
    arb, _, h = make(50)                         # budget 50
    arb.register('big', loader('big', 80), 80)   # bigger than the whole budget
    assert_eq(arb.run('big', lambda mm: 'ok'), 'ok', 'ran after CPU fallback')
    assert_eq(h.failures, ['big'], 'reported the fit failure')
    assert_eq(arb.status()['cpu_fallbacks'], 1, 'cpu fallback counted')
    assert_eq(arb.status()['device'], 'cpu', 'flipped to cpu')
    arb.shutdown()


@test
def test_queue_wait_never_reports_failure():
    # 20 concurrent callers all wait their turn; waiting is not failure.
    arb, _, h = make()
    arb.register('m', loader('m', 1), 1)
    threads = [threading.Thread(target=lambda: arb.run('m', lambda mm: None), daemon=True)
               for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3.0)
    assert_eq(h.failures, [], 'no failures from mere waiting')
    assert_eq(arb.status()['served'], 20, 'all served')
    arb.shutdown()


@test
def test_reentrancy_is_rejected_not_deadlocked():
    arb, _, _ = make()
    arb.register('m', loader('m', 1), 1)

    def reenter(_m):
        return arb.run('m', lambda x: 1)         # illegal: on the owner thread

    try:
        arb.run('m', reenter)
        raise AssertionError('expected ArbiterError')
    except ArbiterError as e:
        assert 'reentrant' in str(e).lower(), str(e)
    assert_eq(arb.run('m', lambda mm: 9), 9, 'arbiter still healthy')
    arb.shutdown()


@test
def test_run_exclusive_serialises_and_returns():
    arb, b, h = make()
    # No model registered; exclusive work needs none.
    assert_eq(arb.run_exclusive(lambda: 21 * 2), 42, 'exclusive result')
    assert_eq(arb.status()['served'], 1, 'served counted')
    assert_eq(h.failures, [], 'exclusive does not touch health')
    assert_eq(b.load_count, {}, 'no model loaded for exclusive work')
    arb.shutdown()


@test
def test_run_exclusive_propagates_exception():
    arb, _, _ = make()

    def boom():
        raise ValueError('exclusive boom')

    try:
        arb.run_exclusive(boom)
        raise AssertionError('expected ValueError')
    except ValueError as e:
        assert_eq(str(e), 'exclusive boom', 'propagated')
    assert_eq(arb.run_exclusive(lambda: 'ok'), 'ok', 'arbiter survives')
    arb.shutdown()


@test
def test_exclusive_interactive_preempts_owned_bulk():
    # An exclusive INTERACTIVE job must jump ahead of queued owned BULK jobs.
    arb, _, _ = make()
    arb.register('x', loader('x', 1), 1)
    results = []
    gate = _occupy_owner(arb)
    threading.Thread(
        target=lambda: arb.run('x', lambda _m: results.append('bulk'), Priority.BULK),
        daemon=True,
    ).start()
    threading.Thread(
        target=lambda: arb.run_exclusive(lambda: results.append('exclusive'), Priority.INTERACTIVE),
        daemon=True,
    ).start()
    assert wait_until(lambda: sum(arb.status()['queued'].values()) == 2), 'both queued'
    gate.set()
    assert wait_until(lambda: len(results) == 2), 'both ran'
    assert_eq(results, ['exclusive', 'bulk'], 'exclusive interactive preempted owned bulk')
    arb.shutdown()


def main():
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f'PASS  {fn.__name__}')
        except BaseException as e:  # noqa: BLE001
            failures += 1
            print(f'FAIL  {fn.__name__}: {type(e).__name__}: {e}')
            traceback.print_exc()
    print(f'\n{len(_TESTS) - failures}/{len(_TESTS)} passed')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
