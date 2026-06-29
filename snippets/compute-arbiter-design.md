# Compute Arbiter — Design & Build Specification

## Overview

Photonarium has **no central arbiter for model/compute access**. Multiple
independent code paths load models into and run inference on the same
(often limited, sometimes absent) accelerator with no coordination of either
*execution* or *VRAM residency*. With a single user this mostly hides; with
multiple clients — or once a large, long-running model (AI image enhancement)
is added — it surfaces as VRAM exhaustion, model thrash, latency spikes, and,
worst case, the CUDA context corruption already tracked as a known
GPU-resilience bug.

This document specifies a **compute arbiter**: a single coordination point that
all model work flows through. It is the compute analogue of the single-writer
`SafeConnection` design (`08b5329`) that eliminated `SQLITE_BUSY` by
construction — one owner, one queue, contention turned into orderly waiting.

> **Device-aware, not GPU-only.** GPU is the *hardest* case (hard VRAM cliff,
> context corruption) and drives most of the design, but **CPU-only is a
> first-class supported runtime**. The arbiter is device-aware: same scheduler,
> device-specific *policy* (§Device-Aware Policy). CPU mode is also the
> permanent, no-mock concurrency **test vehicle** (§Testing).

This is **Phase 0**: it stands alone, fixes present-day multi-client contention,
and is a hard prerequisite for the AI image processing feature (see
[`ai-image-processing-design.md`](ai-image-processing-design.md)).

The guiding discipline for *building* it: **harden it in a sealed box, then graft
it in through a narrow seam** (§Build & Integration Strategy). The hard
concurrency lives inside the box and never leaks into Photonarium's codebase.

---

## Problem Statement

### Evidence this is real, not hypothetical

- **`/api/faces/detect-preview` is deliberately demoted to CPU.** Its docstring
  (`app.py:3784`): *"Detection runs on CPU to avoid contention with the
  pipeline's GPU models."* Originally evidence of the contention problem — but
  on review this is a **deliberate keep**: it builds a fresh CPU MTCNN per call
  with the caller's `min_face_size`, is fast and isolated, and stays CPU-only.
- **`/api/search` runs OpenCLIP inline on the Waitress request thread**
  (`app.py:2831` → `search_images()` → `encode_semantic_query`). N concurrent
  clients searching = N uncoordinated encodes.
- **OOM → context corruption.** A VRAM collision is precisely the trigger for the
  documented GPU-resilience failure (CUDA context corruption that disables all
  GPU features until restart).

### The contention surface

| Class | Consumers | Concurrency |
|-------|-----------|-------------|
| **Background (single stream)** | Pipeline Stages 3 (OpenCLIP), 4 (NIMA/LAION), 5 (MTCNN + InceptionResnetV1), 7 (faster-whisper); the `reassess` thread | One shared stream per instance. Client-*triggered* (`/api/rescan`, `/api/faces/reassess`), never parallel copies. |
| **On-demand interactive (many in flight)** | `/api/search` (OpenCLIP), `/api/faces/detect-preview` (MTCNN), `/api/images/<id>/generate-caption` (BLIP) | One per Waitress request thread; multiple clients ⇒ multiple concurrent. **The real multi-user contention.** |
| **Future** | AI enhancement (SwinIR / Real-ESRGAN) | Long-running bulk client; large resident model. |

Today nothing serialises the interactive ops against each other or against the
background stream, nor caps total VRAM residency across them.

---

## Goals

1. **Correctness** — never exceed the device memory budget; OOM degrades via the
   health sink rather than corrupting the CUDA context.
2. **Fairness** — interactive work is never starved by the background stream
   (bounded wait); the background stream is never starved indefinitely by
   interactive work (forward-progress guarantee).
3. **Determinism of behaviour** — no "sometimes works / sometimes does nothing /
   sometimes takes ages". See §Non-Determinism — designed out, not hoped away.
4. **Liveness** — integrates with graceful shutdown; no job wedges the server.
5. **Degradation** — honours health states (GPU / CPU-fallback / disabled); fully
   functional on **CPU-only** systems.
6. **Observability** — resident model set, queue depth, current job, grants,
   yields and evict counts exposed via `/api/status`.
7. **Lean footprint** — adds a single sealed module behind a ~3-method API, not a
   framework woven through the codebase (§Build & Integration Strategy).

### Non-Goals

- Maximising raw utilisation (correctness-first; one job at a time is fine for
  Phase 0 — co-resident concurrent execution can come later).
- Multi-GPU scheduling (single device assumed; design must not preclude it).
- Replacing the health state machine — the arbiter *uses* it.

---

## Architecture

### A single arbiter thread

All model work is submitted to **one owner thread** that holds the device and
owns the resident model set. Consumers submit a *job* and block on its result.
One job runs at a time, so execution collisions are impossible by construction.

```
            submit(job, priority, model_key)            (blocks caller)
 consumers ───────────────────────────────────▶ ┌───────────────────────┐
 (request threads,                               │   arbiter thread      │
  pipeline, reassess)  ◀───────── result ─────── │  priority queue       │
                                                 │  residency manager    │
                                                 │  device backend (seam)│
                                                 │  health sink (seam)   │
                                                 └───────────────────────┘
```

This mirrors `SafeConnection`'s single-writer queue: one owner ⇒ no races on
model load/evict, no double-loads, one place for OOM policy. Crucially, **the
scheduler is deterministic**: given a fixed *arrival order* of submissions, its
decisions are fully determined. Non-determinism enters only via real-thread
arrival jitter at the edges — which makes the core deterministically testable.

### Two decoupled concepts: *claim* vs *residency*

The cardinal rule:

- **Claim** = whose code runs kernels *right now* (an execution turn).
- **Residency** = which models occupy memory. Governed *separately* by the
  residency manager on an idle-timeout / memory-pressure basis — **not** per
  claim.

Releasing a claim does **not** evict a model. A model loads **once** and stays
warm across many claims (prevents the "load → 1 image → unload × 200"
catastrophe).

### The two seams (what makes the box sealed)

The arbiter depends on **interfaces it is handed**, never on Photonarium:

- **Device backend** — `load(key)`, `run(fn, model)`, `free_memory()`,
  `evict(key)`. Real impl wraps `torch`; standalone/test impl is a fake.
- **Health sink** — `report_failure(feature) -> device`, state queries. The real
  `GpuHealth` (`gputil.py:122`) satisfies this protocol; standalone uses a stub.

Dependency arrow points **one way forever: Photonarium → arbiter, never
arbiter → Photonarium.**

---

## The Consumer Contract

### Acquisition: name the model, get handed a ready one

Consumers **never** hold a raw model reference across calls and **never** decide
whether to (re)load. They submit work *naming* the model they need:

```python
result = arbiter.run(model_key, fn, *, priority=BULK | INTERACTIVE)
#   fn(model) -> result   # fn is handed a guaranteed-resident model
```

Arbiter logic (on its owner thread):

```python
def serve(model_key, fn, priority):
    ensure_resident(model_key)        # cheap no-op if already loaded
    pin(model_key)                    # cannot be evicted while running
    try:
        return fn(resident[model_key])
    finally:
        unpin(model_key)
        touch_lru(model_key)

def ensure_resident(model_key):
    if model_key not in resident:
        while free_memory() < est_cost[model_key] and idle_models_exist():
            evict(least_recently_used_idle())     # del model; empty_cache()
        resident[model_key] = loaders[model_key]()   # the actual load
```

Whether a call involved a reload or hit a warm model is **invisible** to the
consumer. When resident (the common case) `run` is a dict lookup — no reload. A
reload happens only across an actual eviction, and the consumer's code is
identical either way:

```python
for batch in batches:                 # 200 imgs / 32 ≈ 7 batches
    arbiter.run('openclip', lambda m: embed(m, batch), priority=BULK)
# OpenCLIP loaded once; processed continuously; no per-image reload.
```

### Model registration

```python
arbiter.register('openclip', loader=lambda: create_openclip(...), est_cost=...)
arbiter.register('mtcnn',    loader=lambda: MTCNN(...),            est_cost=...)
```

The per-consumer `_load_failed` flag + 60s cooldown and `empty_cache()`
bookkeeping **centralise into the arbiter** (one place instead of eight). The
loader fn is essentially today's `_load_model` body.

---

## Scheduling: priority, yielding, forward progress

- **Two priority levels:** `INTERACTIVE` > `BULK` (room to add more).
- **Cooperative yield between batches.** Bulk work submits per-batch jobs;
  between them the arbiter services any queued higher-priority job first. An
  interactive request waits **at most one bulk batch** (hundreds of ms). The
  model stays resident across the yield.
- **Forward-progress guarantee for bulk.** Bulk must not be starved indefinitely
  by a stream of interactive ops: guarantee bulk a turn at least every *N*
  interactive jobs (or after a max wait). Made explicit; tuned via the harness.

---

## Residency & Eviction Policy

1. **Pinned models are never evicted.** A model in use by the running `fn` is
   pinned for that batch. Eviction happens only **between** jobs (yield points)
   or while a model is idle — **never mid-kernel**.
2. **Evict idle models LRU** until the incoming model fits. Free memory read live
   (GPU: `torch.cuda.mem_get_info()`).
3. **If it still won't fit** after evicting all idle models → run that op on
   **CPU** for this call (graceful degradation). If even CPU fails →
   `report_failure()`.
4. **Idle timeout** — models idle beyond a threshold are evicted proactively
   (extends existing "release models when idle", `1aa459e` / `4a62086`).

### Eviction-during-yield (worked example)

Indexer yields between batches → a caption job needs BLIP → arbiter evicts idle
OpenCLIP to fit → indexer re-claims for its next batch via `run('openclip', …)`
→ arbiter sees it's gone and **transparently reloads** it. The indexer never
knew. Churn is **bounded** (only under genuine memory pressure *with*
contention); on a roomy device both stay resident, nothing reloads. The arbiter
turns a would-be OOM into orderly evict-and-reload.

---

## Device-Aware Policy

Same scheduler, device-specific policy through the backend seam:

| | **GPU** | **CPU-only** |
|--|---------|--------------|
| Memory budget | Hard VRAM cliff via `mem_get_info()` | Soft, RAM-aware at most (no hard cliff; OOM recoverable) |
| Eviction | LRU under budget pressure | Largely unnecessary; optional RAM-pressure trim |
| Failure mode | OOM → context corruption (severe) | RAM OOM (catchable) |
| **Still needed for** | residency + serialisation + fairness | **serialisation + fairness** — concurrent CPU inferences thrash cores/caches; a search must not queue behind a bulk CPU embed |

The arbiter is therefore valuable **even with no GPU**, and must be correct
GPU-less — which is also the no-mock test bed (§Testing).

---

## Busy vs Failed — the back-pressure contract

The single most important UX rule: **"busy" is never surfaced.** Contention
manifests as *latency*, not errors or toasts.

- **Busy = queue, not reject.** No consumer is ever told "busy, try again". It
  submits and blocks at its priority. Nothing to toast; the toast-spam
  anti-pattern (clients polling "is it free?" and retrying) cannot arise in a
  submit-and-block model.
- **Busy ≠ failed — keep strictly separate.** A *queue wait* must **never** route
  through `report_failure()`; only an actual execution exception does.
  Conflating the two would wrongly trip CPU-fallback and produce spurious health
  modals.
- **The only user-facing GPU message remains genuine failure** — health sink →
  `gpu_state_changed` → the existing GPU-health modal (`df349f4`).

## Client Execution Contract (threading)

Generalises the threading model already in use; only long ops add a new pattern.

- **Short ops (search, detect-preview): block the request thread on the future.**
  Matches today — search runs inline; caption blocks on a daemon-thread future
  (`app.py:739`) with shutdown-aware polling (a ready template for awaiting
  `run`). Wait bounded by priority + per-batch yield + CPU soft-deadline. Covered
  by the existing spinner — no new UI.
- **Long ops (enhancement): submit-and-return.** Must **not** hold a Waitress
  thread for minutes. Endpoint returns a job id immediately; progress/completion
  ride the **existing 2s `/api/events` poll** (no new polling, no busy-wait). The
  one genuinely new pattern, justified by duration.
- **Background ops (pipeline, reassess): patient, low priority, submit-and-block
  on their own threads.** Slow gracefully under interactive load and resume;
  nothing silently dropped (stages are idempotent / queue-based). Already report
  via `/api/status` + events.

---

## Non-Determinism — designed out, not hoped away

"Sometimes works / sometimes does nothing / sometimes takes ages" has three root
causes; the first defence is removing the *sources*:

- **"Does nothing" = a silently dropped job or swallowed exception.** *By
  construction:* every submission returns a future that **must** resolve —
  success, exception, or explicit timeout — never fire-and-forget for anything
  that matters. A job that cannot be served returns an **explicit error**, never
  silent nothing. Exceptions always propagate to the submitter. No "retry later"
  path that might not happen.
- **"Takes ages" = an unbounded wait.** *By design:* forward-progress guarantee +
  interactive CPU soft-deadline bound latency. Then **instrument wait times** so
  a long tail shows as a metric, not a complaint. **No timeout-as-control-flow**
  in the core — the classic "sometimes slow" generator.
- **Deterministic core.** Single owner + sequential decisions ⇒ fixed arrival
  order yields fixed behaviour. Only edge arrival-jitter is non-deterministic,
  and that is what the stress layer targets.

---

## Testing & Verification

### Kill-modes → guard map

| How it could kill Photonarium | Primary guard |
|---|---|
| **Deadlock** — arbiter wedges, all GPU work hangs, Waitress threads exhaust → whole app hangs | **Deadlock watchdog**: any future unresolved in T dumps *all* thread stacks and fails. Reentrancy test. Strict lock ordering. |
| **Arbiter thread dies** on an uncaught job exception → work hangs forever | Every job wrapped try/except; exceptions **propagate to submitter**, never to the owner thread. "Survives N throwing jobs" test. |
| **Memory budget exceeded → OOM → context corruption** | Budget **invariant baked in** (assert resident ≤ budget at every transition); fault-injected OOM; real-GPU low-VRAM soak. |
| **Evict a pinned / in-use model** → crash or garbage mid-inference | "pinned-never-evicted" invariant assertion + stress test. |
| **Model leak** — evicted models not freed → memory creep → OOM | Memory-accounting test (fake); real-GPU soak sampling `nvidia-smi`. |
| **Starvation (bulk)** — indexing never completes | Forward-progress test under sustained interactive load. |
| **Starvation (interactive)** — searches hang | Per-batch-yield latency-bound test (p99 under threshold during bulk run). |
| **Busy mis-routed as failure** → spurious health disable + modals | Unit test: a queue wait **never** calls `report_failure()`. |
| **Shutdown wedge** — server won't exit | Shutdown-under-load test; daemon threads + stop-event; cancel/drain on close. |
| **Lost job** — a future never resolves | Stress invariant: *every* submitted job resolves within a timeout. |
| **Reload race / double-load** | Single-owner-thread design + stress test. |

### Test pyramid

1. **Deterministic unit (fake backend)** — eviction matrix, priority, yield,
   forward-progress, OOM→CPU→disabled transitions, load-failure→cooldown→retry,
   error propagation, reentrancy. Injectable clock + barriers for repeatability.
2. **Concurrency stress / property (fake backend)** — N threads submitting
   **seeded randomized schedules** (mixed priority/duration/model, some throwing,
   some slow), **continuously asserting invariants**. **Jitter injection** (random
   sleeps at yield/lock boundaries) widens race windows beyond production; run at
   **volume** (thousands of seeded iterations) so a 1-in-10k interleaving bug
   surfaces with a replayable seed.
3. **Fault injection** — OOM / load-failure / context-loss / cancellation at
   random points; assert graceful degrade, memory reclaimed, correct health
   transitions, no wedge. Includes simulating context-corruption to prove it now
   degrades instead of bricking.
4. **Soak/endurance** — hours-long run; **latency-distribution assertions**
   (p50/p99/max) so "sometimes takes ages" is a visible, assertable tail; leaks
   accumulate into visibility.
5. **CPU-mode integration (real, no mock)** — see below.
6. **Real-GPU smoke + soak (gated/manual)** — multi-client harness against real
   models; **low-VRAM config** (`set_per_process_memory_fraction`) to *force*
   eviction/OOM paths.

### Cross-cutting safety nets

- **Invariant assertions inside the arbiter (debug mode)** — budget-not-exceeded,
  pinned-not-evicted, checked at every transition; a violation surfaces in *any*
  test, not just dedicated ones.
- **Differential testing (test-only)** — for each model, compare the
  arbiter-routed result against a **direct** call to the same registered loader +
  `fn`, constructed inside the test (no shipped bypass flag needed). Identical
  outcomes (embeddings, detections) prove the seam changes only *scheduling*,
  never *results*.
- **Structured decision logging + `/api/status` metrics** — load / evict / yield /
  grant decisions reconstructable; a failed soak is diagnosable, not a mystery
  hang.

### CPU-mode is the permanent, no-mock test vehicle

CPU-only is a **shipping runtime**, so running the **real arbiter with real
(small) models and real threads on CPU** exercises *all* scheduling / fairness /
serialisation / forward-progress logic — on any CI box, no GPU, no mock. The
fake backend is needed *only* to inject GPU-*specific* failures CPU can't produce
(VRAM OOM, context loss), which shrinks to a **one-method fault hook** in
`tests/`. This is why the fake is bring-up scaffolding, not permanent weight.

---

## Build & Integration Strategy

**Harden it in a sealed box, then graft it in through a narrow seam.** The hard
concurrency lives inside the box; Photonarium proper only ever sees ~3 methods
(`register`, `run`, `status`).

### Artifact
A self-contained package (e.g. `app/arbiter/`) with **zero inbound imports from
the rest of Photonarium**, depending only on `torch` + the two seam interfaces
(device backend, health sink). Standalone uses fakes/stubs; integration wires the
real `torch` backend and `GpuHealth`.

### Phase A — Build & harden in the box
Full scaffolding lives *here only*: fake backend, fault injection, jitter-stress,
watchdog, soak. Runs as its own fast, isolated CI suite, fuzzable independently
of the slow app tests. This is "test the hell out of it."

### Phase B — Strip the mess (keep the guard rails)
**"Remove the mess" = discard bring-up *scaffolding*, not *regression
protection*.**
- *Discarded/retired:* the elaborate fake-GPU simulator, one-off confidence
  experiments, heavy harness.
- *Kept, lean:* a small durable regression suite (highest-value scheduler/eviction
  tests + invariant assertions). The mock shrinks to a **one-method fault hook**.
Net carried into the repo: one sealed, documented module + a lean test file — not
a framework.

### Phase C — Integrate leanly
Two integration shapes:

- **Owned** (`arbiter.run('key', fn, …)`) — for clean long-lived singletons the
  arbiter loads and (eventually) evicts. Used for **OpenCLIP** (search +
  embedding stages + transcription text embeddings).
- **Exclusive** (`arbiter.run_exclusive(fn, …)`) — serialises + prioritises a
  GPU callable whose model lifecycle is owned by the caller, *without* arbiter
  residency. Because `fn` runs on the owner thread, lazy loads inside it are
  serialised too (preventing concurrent loads). Used for **NIMA, faces (Stage
  5), Whisper/STT, caption** — whose models have bespoke per-run/lazy lifecycles.
  Converting these to owned models is the eviction-era follow-up.

Retrofit status (lowest-risk first):

1. ✅ `/api/search` + `/api/search/videos` — OpenCLIP, INTERACTIVE (proving slice)
2. ✅ Pipeline Stage 3a/3b embeddings — OpenCLIP, BULK
3. ✅ Pipeline Stage 4 (NIMA), Stage 5 (faces), Stage 7 (Whisper + text embed) — BULK
4. ✅ `generate-caption` — BLIP, INTERACTIVE (kept in its shutdown-abandon executor)
5. `detect-preview` — **stays CPU** (deliberate; isolated, parameterised per call)
6. `reassess` — **no arbiter needed** (CPU cosine similarity over stored embeddings)
7. enhancement, later, as just another owned bulk client (with real `est_cost`)

All GPU work now funnels through the arbiter. Residency/eviction stays disabled
(`est_cost=0`, `idle_evict_s=0`) until measured — the current win is
serialisation + priority + serialised loads, which is the multi-user fix.

### Phase D — Test the integration (small, because the unit is pre-proven)
Only prove **wiring + equivalence**:
1. **Differential** — arbiter-routed vs a direct loader+`fn` call (test-only) →
   identical outcomes.
2. **CPU-mode end-to-end** — real consumers, small models, real threads, no GPU,
   on any CI: multi-client harness (search + detect-preview + scan + face-name)
   asserting no lost jobs, bounded latency, index completes.
3. **Real-GPU soak** — gated/manual, low-VRAM config to force eviction paths.

End state: **one lean, sealed, pre-hardened module behind a ~3-method API + a
lean regression test**, and Photonarium proper just gains `arbiter.run(...)`
calls where it used to poke models directly.

---

## Integration Points (existing code)

| Area | File | Change |
|------|------|--------|
| Device / health authority | `gputil.py:122` (`GpuHealth`) | Satisfies the health-sink seam; arbiter reads `.device`, calls `report_failure()` on execution errors only. |
| OpenCLIP | `imagedb.py` (`_get_clip_model`, `encode_*`) | `_load_model` body → registered loader; call sites → `arbiter.run('openclip', …)`. |
| NIMA / LAION | `nima.py`, `pipeline.py` Stage 4 | As above. |
| Faces | `faces.py` (MTCNN, InceptionResnetV1) | As above; Stage 5 + reassess share registered models. |
| Caption | `caption.py` | As above; reuse its shutdown-aware future pattern for awaiting `run`. |
| STT | `stt.py` (faster-whisper) | As above. |
| Search endpoint | `app.py:2801` | Route encode through the arbiter. |

## Risks & Open Questions

- **One-job-at-a-time may under-utilise large GPUs.** Acceptable for Phase 0;
  revisit co-resident concurrent execution (within budget) later.
- **`est_cost` accuracy.** Start conservative; measure-and-cache actual footprint
  after first load to refine the fit check.
- **Forward-progress tuning.** The interactive:bulk ratio / max-bulk-wait needs
  empirical tuning via the harness.
- **GIL note.** GPU kernels release the GIL; CPU-side pre/post-processing does
  not — another reason serialised submission is fine for Phase 0.
