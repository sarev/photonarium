# Audit 18 — Handle Low-Memory/OOM Gracefully

## Principle

> Handle low-memory/OOM gracefully — model loads, batch inference, and large allocations must catch `MemoryError`/`RuntimeError` and degrade (retry smaller, skip, or disable feature) rather than crash

## Scope

- All model loading code (`torch.load`, `from_pretrained`, `create_model`)
- All batch inference (`torch.stack().to(device)`)
- All large numpy allocations (`np.vstack`, `np.concatenate`)
- `_load_failed` flag pattern compliance
- `torch.cuda.empty_cache()` usage
- `app/imagedb.py`, `app/faces.py`, `app/caption.py`, `app/nima.py`, `app/stt.py`, `app/video.py`

## Findings

### Model Loading with `_load_failed` Pattern

| Model | Location | OOM Catch | `_load_failed` Flag | Cache Cleanup | Feature Disable |
|-------|----------|-----------|---------------------|---------------|-----------------|
| OpenCLIP | `imagedb.py:2546-2602` | ✓ (2584-2596) | ✓ (2587) | ✓ (2591) | ✓ (2592-2596) |
| BLIP/BLIP-2 | `caption.py:190-264` | ✓ (248-259) | ✓ (251) | ✓ (255) | ✓ (256-259) |
| MTCNN | `faces.py:304-310` | ✓ (304) | ✓ `_mtcnn_failed` (307) | ✓ (309) | ✓ (implicit) |
| InceptionResnetV1 | `faces.py:326-331` | ✓ (326) | ✓ `_resnet_failed` (329) | ✓ (331) | ✓ (implicit) |
| faster-whisper | `stt.py:124-131` | ✓ (124-131) | ✓ (97) | ✓ (131) | ✓ (implicit) |

All 5 model loading sites follow the documented pattern:
1. Fast-path check for `_load_failed` before acquiring lock
2. Double-checked locking for thread safety
3. try/except catches `MemoryError` and `RuntimeError` (covers `torch.cuda.OutOfMemoryError`)
4. On failure: set `_load_failed`, release GPU cache, log error, disable feature

### Batch Inference with Two-Tier Fallback

| Batch Site | Location | Tier 1 (Batch) | Tier 2 (Single-item) | Cache Cleanup |
|------------|----------|----------------|----------------------|---------------|
| OpenCLIP encoding | `imagedb.py:2688-2731` | ✓ `torch.stack().to()` (2690) | ✓ per-item loop (2714-2731) | ✓ (2712, 2730) |
| Face embeddings | `faces.py:716-759` | ✓ `torch.stack().to()` (717) | ✓ per-face loop (738-752) | ✓ (726, 750) |
| Video keyframe | `imagedb.py:4307-4313` | ✓ single frame encode | N/A (already single) | ✓ (4310) |

Pattern for each:
1. Try batch: `torch.stack(tensors).to(device)`
2. Catch `(MemoryError, RuntimeError)` with OOM string check
3. Fall back to single-item processing
4. If single-item also fails: skip and log
5. `torch.cuda.empty_cache()` after every catch

### NIMA Batch Inference — Gap Identified

**`nima.py:228-244`**: `batch = torch.stack(tensors).to(device)` — **no try/except at this layer**. However, the calling code in `imagedb.py` (`NimaThread`) wraps the batch call with OOM handling at `imagedb.py:3936-3967`, providing the two-tier fallback externally. The protection exists but is split across files.

### Large Numpy Allocations

| Allocation | Location | Chunked | OOM Protected |
|------------|----------|---------|---------------|
| Duplicate similarity | `duplicates.py:853-939` | ✓ chunk_size=1000 | Implicit (bounded) |
| Incremental duplicates | `duplicates.py:1137-1188` | ✓ chunk_size=5000 | ✓ explicit cleanup |
| Image search | `duplicates.py:1271-1280` | ✓ chunk_size=10000 | Implicit (bounded) |
| Face grouping | `faces.py:2649` | ✗ loads all at once | ✗ no protection |
| Video scene search | `imagedb.py:4701-4711` | ✓ chunk_size=10000 | ✓ pre-allocated |

**Gap**: `faces.py:2649` (`embedding_matrix = np.vstack(embeddings)`) loads ALL unknown face embeddings at once. For 100K+ faces × 512 × float32 ≈ 200MB, this could be problematic on low-memory systems.

### PIL Image Opening

Most uses of `Image.open()` use context managers ensuring cleanup:
- `imagedb.py:1729`, `thumbnails.py:174`, etc.
No explicit size limits checked before opening, but the subsequent `resize()` calls bound memory usage.

## Status

**Compliant** (after fix)

## Actions

- ~~**P2**: Add OOM guard for face grouping vstack~~ — **FIXED**: `faces.py` `np.vstack(embeddings)` wrapped in try/except, logs error and returns 0 on OOM
- ~~**P3**: Add OOM protection in `nima.py`~~ — **FIXED**: `score_images()` now has full two-tier OOM fallback (batch → single-item) with `torch.cuda.empty_cache()`, matching the pattern in `imagedb.py` and `faces.py`
