# Feature Spec: Image Quality Scoring

## Objective

Provide a single, general-purpose quality score per image that helps users surface the "best" images within any group — duplicate groups (levels 0-3), custom groups/albums (level 4), or any future grouping context — and move aside or delete the rest.

The score should approximate an experienced photographer's preference ordering, not just technical fidelity (sharpness, resolution, encoding). It must be:

- Fast enough to compute once at ingest time.
- Stored in the database alongside existing per-image metadata.
- Used at group-sorting time with minimal/no UI complexity (weights can be in config).
- Robust across diverse subject matter by combining two promptless aesthetic proxies:
  - LAION aesthetic score (OpenCLIP embedding head, very cheap).
  - NIMA aesthetic score (CNN-based, toggleable via config like face detection).

Assumption: there is exactly one active scoring configuration at a time for the entire database. If the user changes models or scoring settings, all stored scores are invalidated and recomputed.

### Current Problems This Solves

Multiple parts of Photonarium independently rank images by "quality" using ad-hoc multi-column comparisons:

- **Best-image for duplicate groups** (`duplicates.py`): `ORDER BY resolution DESC, lossless DESC, size DESC, laplacian_var DESC, id ASC`
- **Best-image for custom groups** (`duplicates.py`): same ad-hoc ranking
- **Cull weakest** (planned, `plan-cull-weakest.md`): inverse of the above
- **Group opened in Gallery**: auto-selects `best_image.id`

These rankings are inconsistent, duplicated in SQL and JS, and hard to extend. A unified quality score replaces them all.

---

## High-Level Approach

Compute and persist two aesthetic scalars per image at ingest:

- `aesthetic_laion`: dot product head applied to the already-normalised OpenCLIP image embedding.
- `aesthetic_nima`: NIMA score computed on a resized unsharpened image (400px thumbnail *before* sharpening).

During "sort a group" operations (any group viewed in Gallery — duplicate or custom):

- Convert each raw component to a within-group percentile in `[0..1]`.
- Combine them into a single `quality_score` using fixed default weights (configurable).
- Quality sorting is only available in this group context, not as a global Gallery sort option.

---

## Data Model

### Per-image fields (added to `images` table)

| Column | Type | Description |
|--------|------|-------------|
| `aesthetic_laion` | REAL | LAION aesthetic score from OpenCLIP embedding head |
| `aesthetic_nima` | REAL (nullable) | NIMA score; NULL if NIMA disabled in config |

Existing fields used as inputs (no schema change):
- `embedding` (BLOB, already L2-normalised OpenCLIP embedding)
- `laplacian_var` (REAL)
- `width`, `height` (INTEGER); pixels = width * height
- `size` (INTEGER, file bytes)
- `lossless` (INTEGER 0/1) — retained in schema but **no longer used in ranking**; subsumed by quality scoring. Can be dropped in a future cleanup migration.

### Scoring config

Two categories of scoring configuration, with different storage and invalidation behaviour:

**Model identity (no new table needed):**
- **LAION head** is tied 1:1 to the OpenCLIP model. When the OpenCLIP model changes, all embeddings are recomputed, and `aesthetic_laion` is recomputed in the same pass. The OpenCLIP model is already tracked — no additional mechanism required.
- **NIMA model** identity (`nima_impl`) can be stored as a key in the existing `metadata` table. On startup, compare against the current config value; mismatch triggers NIMA recomputation.

**Weighting factors (in `.photonarium.yml` only, not in DB)** — applied at sort time in JS, never baked into stored per-image values:

| Setting | Default | Description |
|---------|---------|-------------|
| `nima_enabled` | true | Toggle NIMA scoring during ingest (like `face_detection_enabled`) |
| `quality_weights` | `{"A":0.60,"S":0.20,"P":0.15,"B":0.05}` | Component weights for the quality formula |
| `quality_alpha` | 0.60 | Blend factor for NIMA vs LAION in the `A` term |

Changing weights/alpha takes effect immediately on the next group sort — no recomputation needed.

---

## Dependencies

### Already in use
- `torch`, `torchvision`
- `open_clip_torch` (OpenCLIP embeddings)

### New dependencies

**LAION aesthetic head:**
- No additional runtime dependency beyond `torch`.
- Need to ship or download the LAION regressor checkpoint matching the OpenCLIP backbone.
- Add to `download_models.py` and `--list-models` output.

**NIMA (optional):**
- Recommended: `pyiqa` (IQA-PyTorch toolbox), providing NIMA as `nima-vgg16-ava`.
- Toggleable via `nima_enabled` config setting (like `face_detection_enabled`). Works on CPU, just slower — same trade-off as face detection and other GPU-accelerated ingest steps.

---

## Ingest-Time Computations

### Inputs already produced during ingest
- `embedding` (L2-normalised OpenCLIP vector) — computed in `EmbeddingThread._process_batch()`
- `laplacian_var` — computed in `extract_image_metadata()`
- `width`, `height`, `size`, `lossless` — computed in `extract_image_metadata()`
- 200px and 400px thumbnails — generated in `_process_image()`

### Additional computations

#### 1) LAION aesthetic score (always computed)

Computed in `EmbeddingThread._process_batch()` immediately after `encode_images_batch()`, since the embedding is already in GPU memory:

```python
aesthetic_laion = float(clip_emb @ laion_w + laion_b)
```

Where `(laion_w, laion_b)` come from the LAION linear head checkpoint, loaded once at `EmbeddingThread` startup. Assert `len(laion_w) == embedding_dim` at startup and fail fast if mismatch.

Store alongside the embedding in the same batch UPDATE:
```sql
UPDATE images SET embedding = ?, aesthetic_laion = ?, updated_at = ? WHERE id = ?
```

#### 2) NIMA aesthetic score (optional, config-toggleable)

Computed only if `nima_enabled` is true in config. Works on both GPU and CPU (slower on CPU, like face detection and other ingest steps).

**Preprocessing:** Our thumbnail pipeline resizes to 400px then applies sharpening (UnsharpMask) before saving to disc. NIMA must score the image *before* that sharpening step, since sharpening biases the aesthetic score. Hook into the thumbnail generation to capture the 400px resized PIL Image in RAM before sharpening is applied, and pass it directly to NIMA batch processing — no intermediate files written or read from disc.

**Timing:** Can run in `EmbeddingThread._process_batch()` or in a dedicated step during `_process_image()` where the thumbnail is generated. The thumbnail generation path is more natural since the 400px pre-sharpen image is available there.

#### 3) bits-per-pixel (derived on demand)

`bpp = 8 * size / max(1, width * height)` — no need to store; computed from existing columns at sort time.

---

## Group Sorting (Within-Group Percentile Ranking)

A "group" is any set of images viewed together — duplicate groups (levels 0-3), custom groups/albums (level 4), or any future grouping context. Sorting produces a deterministic best-to-worst order.

### Step 1: Compute within-group percentiles in `[0..1]`

For each image in the group, raw values:
- `L_raw = aesthetic_laion`
- `N_raw = aesthetic_nima` (may be NULL)
- `S_raw = log1p(laplacian_var)`
- `P_raw = width * height`
- `B_raw = 8 * size / max(1, pixels)`

Convert each to a percentile-rank within the group (tie-safe, average-rank method). Group of size 1 gets percentile 0.5.

### Step 2: Combine aesthetics into `A`

If NIMA present: `A = alpha * N + (1 - alpha) * L` (default `alpha = 0.60`)
If NIMA absent: `A = L`

### Step 3: Final score

Default weights (configurable via `quality_weights` in `.photonarium.yml`):
```
quality_score = 0.60*A + 0.20*S + 0.15*P + 0.05*B
```

### Step 4: Deterministic tie-breaking

Sort descending by: `quality_score`, `pixels`, `log1p(laplacian_var)`, `size`, `id ASC`.

### Where this runs

**Backend (`duplicates.py`):** Replace the multi-column `ROW_NUMBER() OVER (ORDER BY ...)` best-image ranking with `ORDER BY i.aesthetic_laion DESC, i.laplacian_var DESC, i.id ASC` as a fast approximation for best-image selection (the LAION score dominates the quality ranking, and this avoids needing within-group percentile computation in SQL).

**Frontend (`duplicates.js` / `gallery.js`):** When any group (duplicate or custom) is opened in the Gallery, compute the full within-group percentile ranking in JS for display ordering. The `quality_score` drives both the sort order and the auto-selection of the best image.

---

## Invalidating and Recomputing Scores

Each stored score is invalidated only when the model that produced it changes. Changing the weighting factors (`weights`, `alpha`) does NOT require recomputation — those are applied at sort time in JS, not baked into the stored values.

**`aesthetic_laion`** — recompute when:
- OpenCLIP model or `pretrained` variant changes (LAION head is embedding-space specific)
- LAION head checkpoint changes

Note: changing the OpenCLIP model already triggers re-embedding of all images. The LAION score is a trivial dot product on the embedding, so it can be recomputed as part of the same pass — no separate invalidation mechanism needed (see Phase 1).

**`aesthetic_nima`** — recompute when:
- NIMA model/implementation changes (e.g. switching from `nima-vgg16-ava` to a different model)
- NIMA is enabled after previously being disabled (images will have NULL values that need filling)

Mechanism for NIMA recompute:
- Set `aesthetic_nima = NULL` on all images
- Re-queue all images for NIMA scoring (background thread)

---

## Files to Modify

| File | Change |
|------|--------|
| `imagedb.py` | Schema migration (add columns), LAION scoring in `EmbeddingThread._process_batch()`, NIMA scoring in thumbnail pipeline, migration backfill for existing images |
| `duplicates.py` | Replace multi-column best-image `ORDER BY` with `aesthetic_laion DESC` |
| `config.py` | New settings: `nima_enabled`, `quality_weights`, `quality_alpha` |
| `download_models.py` | Download LAION aesthetic head checkpoint; optionally NIMA model |
| `app.py` | Add LAION head to `--list-models` output |
| `static/appstate/images.js` | Add `'quality'` sort option to `_sortImages()` using within-group percentile `quality_score` |
| `static/appstate/view.js` | Add `'quality'` to valid sort values |
| `static/gallery.js` | Auto-sort by quality when viewing any group (duplicate or custom); hide "Quality" sort option otherwise |
| `static/duplicates.js` | Within-group quality ranking for group display; feeds into `navigateToGroup()` |

---

## Gallery Integration

### "Quality" sort — group context only

The "Quality" sort option is available when viewing any group in the Gallery — whether a duplicate group (levels 0-3) or a custom group/album (level 4), i.e. when navigated from the Groups screen via `navigateToGroup()`. This mirrors how "prev/next group" navigation is already scoped to the group context.

**Rationale:** Quality scoring uses within-group percentile ranking, which is only meaningful when comparing images within a group. A raw `aesthetic_laion` global sort would be misleading — an aesthetically "good" heavily-compressed JPEG shouldn't rank above a technically superior RAW file from a completely different shoot.

**Behaviour:**
- When any group is opened in Gallery, auto-set the sort to `'quality'` descending. The "Quality" option appears in the sort dropdown only while in this context.
- The `'quality'` sort in `_sortImages()` computes within-group percentile `quality_score` for the current display list (which *is* the group) and sorts by it.
- The first image in the sorted list is the best; auto-selection picks it.
- When the group filter is cleared (leaving the group view), restore the previous sort and remove the "Quality" option from the dropdown.

### Lightweight image data

Add `aesthetic_laion` to `get_all_images_lightweight()` and `get_images_delta()` in `imagedb.py` so it's available client-side for the within-group quality sort. Also add `laplacian_var` (needed as tiebreaker and for the within-group percentile ranking in JS).

---

## Migration for Existing Images

On first startup after schema migration:

1. Add columns: `ALTER TABLE images ADD COLUMN aesthetic_laion REAL` / `aesthetic_nima REAL`
2. **LAION backfill:** For all images that already have an `embedding` but no `aesthetic_laion`, compute the LAION score from the stored embedding (cheap — just a dot product, no image I/O):
   ```python
   for image in images_with_embeddings:
       emb = np.frombuffer(image['embedding'], dtype=np.float32)
       score = float(emb @ laion_w + laion_b)
       conn.execute('UPDATE images SET aesthetic_laion = ? WHERE id = ?', (score, image['id']))
   ```
3. **NIMA backfill (optional):** If NIMA is enabled, queue all images for NIMA scoring. This is slower (requires reading thumbnails) and can run in the background.

---

## Impact on `plan-cull-weakest.md`

With this plan implemented first:

- **`_findWeakest(group)`** simplifies dramatically: compute within-group `quality_score` for each image, pick the lowest. One ranking instead of four cascading tiebreakers.
- **Backend best-image ranking** is already simplified (`aesthetic_laion DESC`), so frontend and backend agree on "best" and "weakest."
- **No need to add `laplacian_var` to lightweight data just for cull-weakest** — it's added here for the quality ranking.

Update `plan-cull-weakest.md` after this plan is implemented.

---

## Gotchas / Mitigations

1. **Changing OpenCLIP model invalidates LAION scores** — LAION head is embedding-space specific. If user changes `openclip_model` or `openclip_pretrained`, wipe and recompute all LAION scores.

2. **NIMA is config-toggleable** — `nima_enabled` in `.photonarium.yml`, like `face_detection_enabled`. Defaults to true. Works on CPU (slower, same trade-off as face detection). Store NULL for `aesthetic_nima` when disabled.

3. **NIMA must run on 400px thumbnail *before* sharpening** — Sharpening biases the aesthetic score. Always score the unsharpened resized pixels in-memory.

4. **bpp comparability across formats** — bpp is most meaningful for lossy formats. Keep low weight (0.05 default), or optionally treat lossless/RAW as neutral for the B component.

5. **Group-percentile normalisation is required for group sorting** — Global scaling is unstable across content/cameras/lighting. Always use within-group percentiles for group ranking.

---

## Implementation Phases

### Phase 1: LAION aesthetic score (backend only)

**Goal:** Better best-image selection across all group levels (duplicate and custom) with zero frontend changes and no new dependencies.

**Scope:**
- Schema migration: `ALTER TABLE images ADD COLUMN aesthetic_laion REAL`
- Download LAION aesthetic head checkpoint (add to `download_models.py` and `--list-models`)
- Compute `aesthetic_laion` in `EmbeddingThread._process_batch()` immediately after encoding (embedding already in GPU memory — one dot product per image)
- Backfill existing images from stored embeddings on first startup (cheap: dot product, no image I/O)
- Replace the multi-column `ORDER BY resolution DESC, lossless DESC, size DESC, laplacian_var DESC` best-image ranking in `duplicates.py` with `ORDER BY aesthetic_laion DESC, laplacian_var DESC, id ASC`

**Does not include:** NIMA, frontend quality sort, Gallery integration, config settings for weights. The LAION head checkpoint is tied 1:1 to the OpenCLIP model, so invalidation is implicit — if the user changes models, embeddings are recomputed, and the LAION score is recomputed alongside them.

**Files:** `imagedb.py`, `duplicates.py`, `download_models.py`, `app.py` (list-models only)

**Verification:**
1. Verify `aesthetic_laion` is computed on ingestion (check via DB)
2. Verify existing images have scores after migration backfill
3. Open Groups screen → verify best-image selection improves for both duplicate and custom groups (better composed / sharper images preferred over degraded copies)
4. Change OpenCLIP model → verify scores are recomputed with embeddings
5. Delete any legacy code related to the older "best image selection" algorithm - the one that causes the 'best' image to be automatically selected when viewing a group in the Gallery screen

### Phase 2: Frontend quality sort

**Goal:** Let users see quality-ranked ordering when viewing any group (duplicate or custom) in the Gallery.

**Depends on:** Phase 1 (needs `aesthetic_laion` in the database)

**Scope:**
- Add `aesthetic_laion` and `laplacian_var` to `get_all_images_lightweight()` and `get_images_delta()` in `imagedb.py` so they're available client-side
- Implement within-group percentile ranking in JS (using `aesthetic_laion`, `laplacian_var`, resolution, bpp — the four-component formula from the spec, minus the NIMA term)
- Add context-sensitive `'quality'` sort option in Gallery sort toolbar section — visible only when viewing a group (entered via `navigateToGroup()` from any level including custom)
- Auto-sort by quality when opening any group; auto-select the best image
- Remove the "Quality" option and restore previous sort when leaving the group view

**Does not include:** NIMA. Weighting config (`quality_weights`, `quality_alpha`) can be added to `.photonarium.yml` here or deferred until Phase 3 — the defaults are hardcoded in JS until then.

**Files:** `imagedb.py`, `static/appstate/images.js`, `static/appstate/view.js`, `static/gallery.js`, `static/duplicates.js`

**Verification:**
1. Open a duplicate group in Gallery → auto-sorts by quality descending, best image selected
2. Open a custom group in Gallery → same behaviour
3. "Quality" sort option appears in dropdown only while in group view
4. Leave group view → sort reverts, "Quality" option disappears
5. Within a group of near-duplicates, the top-ranked image should be the best composed / sharpest / highest resolution

### Phase 3: NIMA aesthetic score (optional, config-toggleable)

**Goal:** Improve aesthetic ranking accuracy by blending a second opinion (CNN-based NIMA) with the LAION score.

**Depends on:** Phase 1. Independent of Phase 2 (backend-only, but Phase 2 benefits from it).

**Scope:**
- New dependency: `pyiqa` (IQA-PyTorch toolbox) — requires strong justification per project rules
- Schema migration: `ALTER TABLE images ADD COLUMN aesthetic_nima REAL`
- Store `nima_impl` in the existing `metadata` table to detect model changes on startup
- Config toggle: `nima_enabled` in `.photonarium.yml` (like `face_detection_enabled`), defaults to true. Works on both GPU and CPU — slower on CPU, same trade-off as face detection and other GPU-accelerated ingest steps. Store NULL for `aesthetic_nima` when disabled.
- Hook into thumbnail generation to capture the 400px resized PIL Image *before* sharpening is applied (our thumbnails are sharpened before saving, which would bias the score). Pass these pre-sharpen images in RAM to NIMA batch processing — no intermediate files on disc.
- Config settings in `config.py`: `nima_enabled`, `quality_weights`, `quality_alpha`
- Update Phase 2's JS percentile ranking to incorporate NIMA term when present
- NIMA backfill for existing images (slower — requires reading thumbnails, background queue)
- Invalidate/recompute `aesthetic_nima` only when the NIMA model itself changes (not when weighting factors change — weights are applied at sort time in JS, not stored per-image)
- Add NIMA model to `download_models.py` and `--list-models` output

**Open design questions — must be resolved before implementation:**

1. **Own phase vs integrated into thumbnail generation?**
   - *Integrated:* NIMA runs inline during `_process_image()` where thumbnails are generated. Simpler hand-off (the pre-sharpen PIL Image is right there in scope). But thumbnail generation is multi-threaded via `indexing_threads` and NIMA wants batched GPU inference — single-image scoring per thread would underutilise the GPU.
   - *Own phase:* A dedicated `NimaThread` (like `EmbeddingThread`) that runs after thumbnail generation, consuming batches of images. Cleaner separation, natural batching, but requires a mechanism to hand over the pre-sharpen pixel data without disc I/O or RAM explosion.

2. **In-memory hand-off without RAM explosion.**
   If NIMA runs as a separate phase, the thumbnail generation phase produces pre-sharpen 400px images that NIMA needs later. Options:
   - *Bounded queue:* Thumbnail threads push pre-sharpen PIL Images into a bounded `queue.Queue(maxsize=N)`. NIMA thread pulls batches. Backpressure from the queue naturally limits how many images are buffered in RAM at once. A 400px RGB image is ~0.5 MB, so a queue of 64 images ≈ 32 MB — manageable.
   - *Re-read from disc:* NIMA reads the saved (sharpened) 400px thumbnail from disc and skips the sharpening concern by loading the raw pixels before sharpen. But this defeats the point — the saved thumbnail IS sharpened. Would need to save a second unsharpened copy, which is wasteful.
   - *Re-generate:* NIMA reopens the original image file and resizes to 400px itself. Correct (no sharpening) but expensive (full image decode repeated).

   The bounded queue approach is strongly preferred — minimal RAM overhead, no disc I/O, no redundant image decoding.

3. **Batching and parallelisation.**
   NIMA scoring should batch images for GPU efficiency (like `EmbeddingThread` does). Config setting `nima_batch_size` (default e.g. 16-32). The NIMA thread consumes from the bounded queue, accumulates a batch, runs inference, writes scores to DB.

4. **Graceful shutdown integration.**
   The NIMA thread must integrate with the existing graceful shutdown pattern — check the stop event between batches, drain cleanly, don't leave orphaned work. Follow the same pattern as `EmbeddingThread` and face detection.

5. **Frontend status polling.**
   If NIMA is a separate ingest phase, the frontend's database status panel needs to show its progress (like it does for embedding computation and face detection). This means:
   - Backend must track NIMA phase progress (images scored / total pending)
   - The `/api/status` response needs a NIMA section
   - `static/database.js` needs to render the new phase in the progress UI

6. **Backfill strategy.**
   For existing images that have thumbnails on disc but no `aesthetic_nima`: we can't get the pre-sharpen pixels from the saved thumbnails (they're already sharpened). Options:
   - Re-open original image files and resize to 400px (correct but slow — full image decode)
   - Score the sharpened thumbnails and accept the slight bias (pragmatic — the sharpening effect on NIMA is likely small)
   - The bounded-queue approach doesn't apply to backfill since there's no concurrent thumbnail generation. Backfill would use the re-open-and-resize path or the accept-sharpened-thumbnails path.

**Risk:** Adds a new dependency and increases ingest time. The new ingest phase (thread, queue, status, shutdown) adds significant complexity. Can be deferred — Phase 1+2 deliver the core value without it.

**Files:** `imagedb.py`, `duplicates.py`, `config.py`, `download_models.py`, `app.py`, `static/appstate/images.js`, `static/appstate/status.js`, `static/database.js`

**Verification:**
1. Verify `aesthetic_nima` is computed when enabled (GPU and CPU hosts)
2. Verify `aesthetic_nima` is NULL when `nima_enabled` is false
3. Verify NIMA scores are computed from pre-sharpen 400px thumbnails
4. Change scoring config → verify all scores invalidated and recomputed
5. Compare quality rankings with and without NIMA on curated test sets
6. Verify ingest throughput is acceptable with NIMA enabled on CPU

---

## Verification (end-to-end, all phases)

1. Add a folder with mixed images (RAW, PNG, JPEG at various qualities/compositions)
2. Verify `aesthetic_laion` is computed on ingestion (check via API or DB)
3. Verify `aesthetic_nima` is computed when enabled, NULL when disabled
4. Modify a JPEG (recompress at lower quality), rescan → verify scores recomputed
5. Open Groups screen → verify best-image selection correlates with quality for both duplicate and custom groups
6. Open a duplicate group in Gallery → auto-sorts by quality, auto-selects best
7. Open a custom group in Gallery → same behaviour
8. Verify "Quality" sort option only appears in the sort dropdown when viewing a group, and disappears when leaving
9. Verify existing images have scores after migration (LAION from stored embeddings)
10. Change OpenCLIP model in config → verify scores are invalidated and recomputed
11. On curated near-duplicate clusters, the top-ranked image should typically be better composed / clearer / not a visibly degraded encode
