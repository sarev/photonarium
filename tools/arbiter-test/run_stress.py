"""Concurrency stress test for the compute arbiter (no GPU, no pytest).

Hammers the arbiter from many threads with a randomized, jittered job mix and
continuously checks the invariants that matter for the "sometimes works /
sometimes hangs / sometimes does nothing" failure modes:

* every submitted job resolves (no lost jobs, no deadlock);
* the memory budget is never exceeded (no unexpected OOM);
* no pinned model is ever evicted (debug invariant);
* no memory is leaked after shutdown.

A faulthandler watchdog dumps all thread stacks and aborts if the run wedges,
so a deadlock surfaces as a diagnosable stack dump rather than a silent hang.

    python tools/arbiter-test/run_stress.py [SEED] [SECONDS]
"""

from __future__ import annotations

import faulthandler
import os
import random
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.normpath(os.path.join(_HERE, '..', '..', 'app'))
sys.path.insert(0, _APP)

from fake import FakeBackend, FakeHealthSink, FakeModel, FakeOOM

from arbiter import ComputeArbiter, Priority

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 1234
DURATION_S = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
N_THREADS = 8
WATCHDOG_S = DURATION_S + 30.0

# Five models; budget fits ~3-4 of them, forcing constant eviction churn.
MODELS = {f'm{i}': 30 + 10 * i for i in range(5)}  # costs 30..70
BUDGET = 200


def main() -> int:
    random.seed(SEED)
    print(f'stress: seed={SEED} duration={DURATION_S}s threads={N_THREADS} models={MODELS} budget={BUDGET}')

    # Watchdog: if the whole run wedges, dump every thread's stack and abort.
    faulthandler.dump_traceback_later(WATCHDOG_S, exit=True)

    backend = FakeBackend(BUDGET)
    health = FakeHealthSink(backend)
    arb = ComputeArbiter(backend, health, idle_evict_s=0.01, bulk_starvation_limit=4, debug=True)
    for key, cost in MODELS.items():
        arb.register(key, (lambda k=key, c=cost: FakeModel(k, c)), cost)

    lock = threading.Lock()
    stats = {'submitted': 0, 'ok': 0, 'expected_exc': 0, 'unexpected_oom': 0, 'other_exc': 0}
    stop = threading.Event()

    def worker(wid: int) -> None:
        rnd = random.Random(SEED * 1000 + wid)
        while not stop.is_set():
            key = rnd.choice(list(MODELS))
            priority = Priority.INTERACTIVE if rnd.random() < 0.5 else Priority.BULK
            should_raise = rnd.random() < 0.1

            def fn(_m, should_raise=should_raise, rnd=rnd):
                # Tiny, sometimes-jittered work; occasionally raise to prove the
                # arbiter survives throwing jobs.
                if rnd.random() < 0.3:
                    time.sleep(rnd.uniform(0.0, 0.002))
                if should_raise:
                    raise ValueError('synthetic job error')
                return _m.calls

            with lock:
                stats['submitted'] += 1
            try:
                arb.run(key, fn, priority)
                with lock:
                    stats['ok'] += 1
            except ValueError:
                with lock:
                    stats['expected_exc'] += 1
            except FakeOOM:
                with lock:
                    stats['unexpected_oom'] += 1  # a budget bug — must stay 0
            except BaseException:
                with lock:
                    stats['other_exc'] += 1

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    time.sleep(DURATION_S)
    stop.set()
    for t in threads:
        t.join(10.0)

    arb.shutdown()
    faulthandler.cancel_dump_traceback_later()

    status = arb.status()
    resolved = stats['ok'] + stats['expected_exc'] + stats['unexpected_oom'] + stats['other_exc']

    print(
        f'submitted={stats["submitted"]} resolved={resolved} '
        f'ok={stats["ok"]} expected_exc={stats["expected_exc"]} '
        f'unexpected_oom={stats["unexpected_oom"]} other_exc={stats["other_exc"]}'
    )
    print(
        f'served={status["served"]} loads={status["loads"]} evictions={status["evictions"]} '
        f'cpu_fallbacks={status["cpu_fallbacks"]} oom_events={status["oom_events"]}'
    )
    print(f'allocated_after_shutdown={backend.allocated} invariant_violation={status["invariant_violation"]}')

    ok = True

    def check(cond: bool, label: str) -> None:
        nonlocal ok
        print(f'  {"OK  " if cond else "FAIL"} {label}')
        ok = ok and cond

    check(resolved == stats['submitted'], 'every submitted job resolved (no lost jobs / deadlock)')
    check(stats['unexpected_oom'] == 0, 'memory budget never exceeded (no unexpected OOM)')
    check(stats['other_exc'] == 0, 'no unexpected exception types')
    check(status['invariant_violation'] is None, 'no pinned model was ever evicted')
    check(backend.allocated == 0, 'no memory leaked after shutdown')

    print('STRESS PASS' if ok else 'STRESS FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
