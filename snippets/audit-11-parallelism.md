# Audit 11 — Parallelise Intensive Tasks; Avoid O(n²) and Memory Explosions

## Principle

> Parallelise intensive tasks; avoid O(n²) patterns and memory explosions

## Scope

- `app/duplicates.py` — similarity computation chunking
- `app/faces.py` — face grouping, person similarity
- `app/imagedb.py` — embedding computation, video scene search
- All `np.vstack`, `np.concatenate`, `torch.stack` usage
- Nested loop patterns over large collections
- Thread pool architecture and coordination

## Findings

### Well-Implemented Chunking Patterns

1. **Duplicate similarity** (`duplicates.py:853-939`): Chunked matrix multiplication with `chunk_size=1000`. Computes `chunk_embeddings @ embeddings.T` (shape: chunk_len × n). Memory complexity O(chunk_size × n) instead of O(n²). Upper-triangle iteration avoids redundant pairs.

2. **Incremental duplicates level 2** (`duplicates.py:1073-1188`): Processes in chunks of 5000 with dirty image matrix. Explicit cleanup: `del chunk_matrix, chunk_embeddings, chunk_rows`.

3. **Image search** (`duplicates.py:1271-1280`): 10,000-item chunks. Comment: "Each embedding is ~2KB (512 × float32), so 10k embeddings ≈ 20MB per chunk."

4. **Video scene search** (`imagedb.py:4700-4711`): Chunked processing with `chunk_size=10000`. Pre-allocated numpy arrays.

5. **Semantic text search** (`imagedb.py:4565-4577`): Vectorised dot product `img_matrix @ query_embedding`. Threshold filtering before sorting.

### Thread Pool Architecture

7 background threads with coordinated queues (`imagedb.py:129-138`):
- IngestionThread → EmbeddingThread (producer/consumer)
- FaceDetectionThread (waits for EmbeddingThread completion flag)
- NimaThread (waits for IngestionThread idle)
- VideoProcessingThread (runs parallel with EmbeddingThread)
- TrashWorker, ImportWorker (independent)

Bounded EventQueue ring buffer (`imagedb.py:4913-4936`): `MAX_EVENTS = 200`, auto-trims oldest.

### O(n²) Issues Found

1. **CRITICAL: Face unknown grouping** (`faces.py:2671-2679`):
   The matrix multiplication is properly chunked (`chunk @ embedding_matrix.T`), but the inner loop that processes similarities is O(n²):
   ```python
   for local_idx in range(chunk_end - i):          # chunk_size iterations
       global_idx = i + local_idx
       for j in range(global_idx + 1, n_faces):    # up to n_faces iterations
           if similarities[local_idx, j] >= threshold:
               uf.union_ids(face_id_i, face_id_j)
   ```
   For 100K unknown faces: ~10 billion Python loop iterations despite the chunked matrix computation. The `duplicates.py:911` implementation uses vectorised `np.where()` for the same task — this should follow that pattern.

2. **MEDIUM: Person face similarity** (`faces.py:1220-1235`):
   Computes full pairwise similarity: `similarities = embeddings @ embeddings.T`. O(n²) memory for all faces in a person. No chunking protection. Mitigated by practical limits (persons typically <100 faces) but no explicit bounds check.

### Unbounded Collection Growth

- **Face grouping** (`faces.py:2649`): `embedding_matrix = np.vstack(embeddings)` loads ALL unknown face embeddings at once. For massive datasets (100K+ unknown faces × 512 × float32 = ~200MB), this could be problematic on low-memory systems.

## Status

**Compliant** (after fix)

## Actions

- ~~**P1**: Refactor `faces.py:2671-2679` to use vectorised `np.where()` comparison~~ — **FIXED**: inner loop now slices `similarities[local_idx, global_idx + 1:]` and uses `np.where(row_sims >= threshold)` matching the `duplicates.py` pattern
- ~~**P2**: Add bounds check in `faces.py:1220-1235` (`eject_low_quality_faces()`)~~ — **FIXED**: chunked similarity computation for persons with >500 faces, with OOM guard on `np.vstack`
- **P3**: Consider chunked loading of unknown face embeddings in `faces.py:2649` for extreme-scale datasets (OOM guard added; full chunking deferred)
