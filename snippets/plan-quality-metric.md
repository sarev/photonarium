# Feature Spec: Image Quality Scoring

## Objective

Provide a single, general-purpose quality score per image that helps users surface the "best" images within a group (duplicates / near-duplicates / semantic clusters) and move aside or delete the rest.

The score should approximate an experienced photographer's preference ordering, not just technical fidelity (sharpness, resolution, encoding). It must be:

- Fast enough to compute once at ingest time.
- Stored in the database alongside existing per-image metadata.
- Used at group-sorting time with minimal/no UI complexity (weights can be in config).
- Robust across diverse subject matter by combining two promptless aesthetic proxies:
  - LAION aesthetic score (OpenCLIP embedding head, very cheap).
  - NIMA aesthetic score (CNN-based, optional on CUDA hosts).

Assumption: there is exactly one active scoring configuration at a time for the entire database. If the user changes models or scoring settings, all stored scores are invalidated and recomputed.

### Current Problems This Solves

Multiple parts of Imaginary independently rank images by "quality" using ad-hoc multi-column comparisons:

- **Duplicate best-image** (`duplicates.py`): `ORDER BY resolution DESC, lossless DESC, size DESC, laplacian_var DESC, id ASC`
- **Cull weakest** (planned, `plan-cull-weakest.md`): inverse of the above
- **Duplicate group in Gallery**: auto-selects `best_image.id`

These rankings are inconsistent, duplicated in SQL and JS, and hard to extend. A unified quality score replaces them all.

---

## High-Level Approach

Compute and persist two aesthetic scalars per image at ingest:

- `aesthetic_laion`: dot product head applied to the already-normalised OpenCLIP image embedding.
- `aesthetic_nima`: NIMA score computed on a resized unsharpened image (400px thumbnail *before* sharpening).

During "sort a group" operations (duplicate group viewed in Gallery):

- Convert each raw component to a within-group percentile in `[0..1]`.
- Combine them into a single `quality_score` using fixed default weights (configurable).
- Quality sorting is only available in this duplicate-group context, not as a global Gallery sort option.

---

## Data Model

### Per-image fields (added to `images` table)

| Column | Type | Description |
|--------|------|-------------|
| `aesthetic_laion` | REAL | LAION aesthetic score from OpenCLIP embedding head |
| `aesthetic_nima` | REAL (nullable) | NIMA score; NULL if NIMA disabled or CPU-only |

Existing fields used as inputs (no schema change):
- `embedding` (BLOB, already L2-normalised OpenCLIP embedding)
- `laplacian_var` (REAL)
- `width`, `height` (INTEGER); pixels = width * height
- `size` (INTEGER, file bytes)
- `lossless` (INTEGER 0/1) — retained in schema but **no longer used in ranking**; subsumed by quality scoring. Can be dropped in a future cleanup migration.

### Scoring config (stored once per database)

New table `scoring_config` with a single row:

| Column | Type | Description |
|--------|------|-------------|
| `scoring_version` | INTEGER | Increment on any model/weights/preprocess change |
| `openclip_model` | TEXT | e.g. `ViT-B-32` |
| `openclip_pretrained` | TEXT | e.g. `openai` |
| `laion_head_id` | TEXT | Checkpoint filename/version for the LAION linear head |
| `nima_enabled` | INTEGER | 0/1; auto-set to 0 if no CUDA |
| `nima_impl` | TEXT | e.g. `pyiqa:nima-vgg16-ava` |
| `weights` | TEXT (JSON) | e.g. `{"A":0.60,"S":0.20,"P":0.15,"B":0.05}` |
| `alpha` | REAL | Blend factor for NIMA vs LAION in the `A` term |

If `scoring_version` changes (or OpenCLIP model changes), all per-image `aesthetic_*` values are considered stale and must be recomputed.

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
- Auto-disabled on CPU-only hosts (too slow for ingest throughput).

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

#### 2) NIMA aesthetic score (optional, CUDA-only)

Computed only if enabled and CUDA is available.

**Preprocessing:** Use the 400px thumbnail pixels *before* sharpening is applied. Score the in-memory resized pixels prior to encoding to avoid thumbnail codec effects. This requires a small change to the thumbnail generation pipeline to capture the pre-sharpen PIL Image.

**Hardware gating:** If CUDA is not available, auto-set `nima_enabled=false` in config and store NULL for `aesthetic_nima`.

**Timing:** Can run in `EmbeddingThread._process_batch()` or in a dedicated step during `_process_image()` where the thumbnail is generated. The thumbnail generation path is more natural since the 400px pre-sharpen image is available there.

#### 3) bits-per-pixel (derived on demand)

`bpp = 8 * size / max(1, width * height)` — no need to store; computed from existing columns at sort time.

---

## Group Sorting (Within-Group Percentile Ranking)

A "group" is a set of images that are exact/near duplicates or a semantic cluster. Sorting produces a deterministic best-to-worst order.

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

Default weights (configurable via `scoring_config.weights`):
```
quality_score = 0.60*A + 0.20*S + 0.15*P + 0.05*B
```

### Step 4: Deterministic tie-breaking

Sort descending by: `quality_score`, `pixels`, `log1p(laplacian_var)`, `size`, `id ASC`.

### Where this runs

**Backend (`duplicates.py`):** Replace the multi-column `ROW_NUMBER() OVER (ORDER BY ...)` best-image ranking with `ORDER BY i.aesthetic_laion DESC, i.laplacian_var DESC, i.id ASC` as a fast approximation for best-image selection (the LAION score dominates the quality ranking, and this avoids needing within-group percentile computation in SQL).

**Frontend (`duplicates.js` / `gallery.js`):** When a duplicate group is opened in the Gallery, compute the full within-group percentile ranking in JS for display ordering. The `quality_score` drives both the sort order and the auto-selection of the best image.

---

## Invalidating and Recomputing Scores

Trigger a full recompute when any of the following changes:
- OpenCLIP model or `pretrained` variant (LAION head is embedding-space specific)
- LAION head checkpoint
- NIMA enabled/disabled or NIMA weights/implementation

Mechanism:
- Increment `scoring_version` in `scoring_config`
- Set `aesthetic_laion = NULL`, `aesthetic_nima = NULL` on all images
- Recompute for all images (via embedding thread re-queue or a CLI command)

---

## Files to Modify

| File | Change |
|------|--------|
| `imagedb.py` | Schema migration (add columns + `scoring_config` table), LAION scoring in `EmbeddingThread._process_batch()`, NIMA scoring in thumbnail pipeline, migration backfill for existing images |
| `duplicates.py` | Replace multi-column best-image `ORDER BY` with `aesthetic_laion DESC` |
| `config.py` | New settings: `nima_enabled`, `quality_weights`, `quality_alpha` |
| `download_models.py` | Download LAION aesthetic head checkpoint; optionally NIMA model |
| `app.py` | Add LAION head to `--list-models` output |
| `static/appstate/images.js` | Add `'quality'` sort option to `_sortImages()` using within-group percentile `quality_score` |
| `static/appstate/view.js` | Add `'quality'` to valid sort values |
| `static/gallery.js` | Auto-sort by quality when viewing duplicate groups; hide "Quality" sort option otherwise |
| `static/duplicates.js` | Within-group quality ranking for group display; feeds into `navigateToGroup()` |

---

## Gallery Integration

### "Quality" sort — duplicate groups only

The "Quality" sort option is only available when viewing a duplicate group in the Gallery (i.e. when navigated from the Duplicates screen via `navigateToGroup()`). This mirrors how "prev/next group" navigation is already scoped to the duplicate-group context.

**Rationale:** Quality scoring uses within-group percentile ranking, which is only meaningful when comparing images within a group. A raw `aesthetic_laion` global sort would be misleading — an aesthetically "good" heavily-compressed JPEG shouldn't rank above a technically superior RAW file from a completely different shoot.

**Behaviour:**
- When a duplicate group is opened in Gallery, auto-set the sort to `'quality'` descending. The "Quality" option appears in the sort dropdown only while in this context.
- The `'quality'` sort in `_sortImages()` computes within-group percentile `quality_score` for the current display list (which *is* the group) and sorts by it.
- The first image in the sorted list is the best; auto-selection picks it.
- When the duplicate filter is cleared (leaving the group view), restore the previous sort and remove the "Quality" option from the dropdown.

### Lightweight image data

Add `aesthetic_laion` to `get_all_images_lightweight()` and `get_images_delta()` in `imagedb.py` so it's available client-side for the within-group quality sort. Also add `laplacian_var` (needed as tiebreaker and for the within-group percentile ranking in JS).

---

## Migration for Existing Images

On first startup after schema migration:

1. Add columns: `ALTER TABLE images ADD COLUMN aesthetic_laion REAL` / `aesthetic_nima REAL`
2. Create `scoring_config` table with initial row
3. **LAION backfill:** For all images that already have an `embedding` but no `aesthetic_laion`, compute the LAION score from the stored embedding (cheap — just a dot product, no image I/O):
   ```python
   for image in images_with_embeddings:
       emb = np.frombuffer(image['embedding'], dtype=np.float32)
       score = float(emb @ laion_w + laion_b)
       conn.execute('UPDATE images SET aesthetic_laion = ? WHERE id = ?', (score, image['id']))
   ```
4. **NIMA backfill (optional):** If NIMA is enabled and CUDA available, queue all images for NIMA scoring. This is slower (requires reading thumbnails) and can run in the background.

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

2. **NIMA should auto-disable without CUDA** — NIMA on CPU may be too slow for ingest throughput. Default: if no CUDA, set `nima_enabled=false` and store NULL.

3. **NIMA must run on 400px thumbnail *before* sharpening** — Sharpening biases the aesthetic score. Always score the unsharpened resized pixels in-memory.

4. **bpp comparability across formats** — bpp is most meaningful for lossy formats. Keep low weight (0.05 default), or optionally treat lossless/RAW as neutral for the B component.

5. **Group-percentile normalisation is required for group sorting** — Global scaling is unstable across content/cameras/lighting. Always use within-group percentiles for group ranking.

---

## Verification

1. Add a folder with mixed images (RAW, PNG, JPEG at various qualities/compositions)
2. Verify `aesthetic_laion` is computed on ingestion (check via API or DB)
3. Verify `aesthetic_nima` is computed on CUDA hosts, NULL on CPU-only
4. Modify a JPEG (recompress at lower quality), rescan → verify scores recomputed
5. Open Duplicates screen → verify best-image selection correlates with quality
6. Open a duplicate group in Gallery → auto-sorts by quality, auto-selects best
7. Verify "Quality" sort option only appears in the sort dropdown when viewing a duplicate group, and disappears when leaving
8. Verify existing images have scores after migration (LAION from stored embeddings)
9. Change OpenCLIP model in config → verify scores are invalidated and recomputed
10. On curated near-duplicate clusters, the top-ranked image should typically be better composed / clearer / not a visibly degraded encode
