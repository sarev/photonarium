# Feature Spec: Expert-Proxy Image Quality Scoring (Ingest-Time + Group Sorting)

## Objective

Provide a single, general-purpose `quality_score` per image that helps users surface the “best” images within a group (duplicates / near-duplicates / semantic clusters) and move aside or delete the rest.

The score should approximate an experienced photographer’s preference ordering, not just technical fidelity (sharpness, resolution, encoding). It must be:

- Fast enough to compute once at ingest time.
- Stored in the database alongside existing per-image metadata.
- Used at group-sorting time with minimal/no UI complexity (weights can be in config).
- Robust across diverse subject matter by combining two promptless aesthetic proxies:
  - LAION aesthetic score (OpenCLIP embedding head, very cheap).
  - NIMA aesthetic score (CNN-based, optional on CUDA hosts).

Assumption: there is exactly one active scoring configuration at a time for the entire database. If the user changes models or scoring settings, all stored scores are invalidated and recomputed.

## High-Level Approach

Compute and persist two aesthetic scalars per image at ingest:

- `aesthetic_laion`: dot product head applied to the already-normalised OpenCLIP image embedding.
- `aesthetic_nima`: NIMA score computed on a resized unsharpened image (400px thumbnail *before* sharpening).

During “sort a group” operations:

- Convert each raw component to a within-group percentile in `[0..1]`.
- Combine them into a single `quality_score` using fixed default weights (configurable).

## Data Model

### Per-image fields (added)

- `aesthetic_laion` (float)
- `aesthetic_nima` (float, nullable; may be absent if NIMA disabled)
- `quality_score` (float, optional; can be computed on-demand per group instead of stored)

Existing fields assumed available:
- OpenCLIP embedding `clip_emb` (vector, already L2-normalised)
- `laplacian_var` (float)
- `width`, `height` (int), `pixels = width * height` (int)
- `bytes` (int)
- `format` (string)

### Global “scoring metadata” (stored once per database)

Store a single row/document that describes the active scoring setup, e.g. table `scoring_config` with a fixed primary key.

Suggested fields:
- `scoring_version` (int)  
  Increment whenever any model/weights/preprocess changes. Used to detect stale rows.
- `openclip_model` (string) e.g. `ViT-B-32`
- `openclip_pretrained` (string) e.g. `openai`
- `laion_head_id` (string) e.g. checkpoint filename/version
- `nima_impl` (string) e.g. `pyiqa:nima-vgg16-ava`
- `nima_enabled` (bool)
- `weights` (JSON) e.g. `{ "A":0.60, "S":0.20, "P":0.15, "B":0.05 }`
- `alpha` (float) for combining NIMA and LAION into `A`
- `thumb_long_edge_for_nima` (int) fixed at 400
- `thumb_pre_sharpen` (bool) fixed `true` (must be pre-sharpen pixels)

If `scoring_version` changes, all per-image `aesthetic_*` and `quality_score` values are considered invalid and must be recomputed.

## Dependencies (Python Modules)

### Already in use
- `torch`
- `open_clip_torch` (OpenCLIP embeddings)
- `facenet-pytorch` (MTCNN + InceptionResnetV1; not directly used here but already part of ingest)

### New dependencies

#### LAION aesthetic head
- No additional runtime dependency beyond `torch`
- Need to ship or download the LAION regressor checkpoint matching the OpenCLIP backbone.

#### NIMA
Option A (recommended): use `pyiqa`
- `pyiqa` (IQA-PyTorch toolbox) providing NIMA as `nima` / `nima-vgg16-ava`

Option B: standalone NIMA implementation (pinned)
- A specific PyTorch NIMA repo / package, pinned to a commit/tag.

Also required (likely already present):
- image decode/resample stack (PIL / OpenCV / libvips / etc.)
- `torchvision` transforms (or equivalent preprocessing utilities)

## Ingest-Time Computations

### Inputs already produced during ingest
- `clip_emb` (L2-normalised OpenCLIP embedding)
- `laplacian_var`
- `width`, `height`, `pixels`
- `bytes`, `format`
- generation of 200px and 400px thumbnails

### Additional computations

#### 1) LAION aesthetic score (always computed)
Compute from embedding:
- `aesthetic_laion = dot(clip_emb, w) + b`

Where:
- `(w, b)` come from the LAION linear head checkpoint for the configured OpenCLIP model.
- `clip_emb` is already L2-normalised.

Implementation notes:
- Load `(w, b)` once at process startup.
- Assert `len(w) == embedding_dim` at startup and fail fast if mismatch.

#### 2) NIMA aesthetic score (optional)
Compute only if enabled and hardware permits.

Preprocessing requirements:
- Use the 400px thumbnail pixels *before* sharpening is applied.
- Do not run NIMA on a re-encoded thumbnail file; score the in-memory resized pixels prior to encoding to avoid thumbnail codec effects.

Hardware gating:
- If CUDA is available: run NIMA on GPU.
- If CUDA is not available: auto-disable NIMA (skip computing `aesthetic_nima`, store NULL).

Persist:
- `aesthetic_nima` as float when computed, else NULL.

#### 3) (Derived) bits-per-pixel (computed on demand)
- `bpp = 8 * bytes / max(1, pixels)`

No need to store separately if `bytes` and `pixels` are stored.

## Group Sorting

A “group” is a set of images that are:
- exact duplicates / near-duplicates, OR
- a semantic cluster (e.g. OpenCLIP clustering)

Sorting produces a deterministic best-to-worst order.

### Step 1: Compute within-group percentiles in `[0..1]`

For each image in the group, define raw values:

- `L_raw = aesthetic_laion`
- `N_raw = aesthetic_nima` (may be NULL)
- `S_raw = laplacian_var`
- `P_raw = pixels`
- `B_raw = bpp = 8*bytes/max(1,pixels)`

Transforms:
- Use `S_raw = log1p(laplacian_var)` before ranking (recommended).

Convert each to a percentile-rank within the group:
- tie-safe: equal values get equal percentiles (e.g. average-rank method)
- group of size 1 => percentile is `0.5`

Percentiles (higher is better):
- `L = pct_rank(L_raw)`
- `S = pct_rank(log1p(S_raw))`
- `P = pct_rank(P_raw)`
- `B = pct_rank(B_raw)` (see gotchas)

If NIMA is available for the image:
- `N = pct_rank(N_raw)`

### Step 2: Combine aesthetics into a single term `A`

If NIMA is enabled and present:
- `A = alpha * N + (1 - alpha) * L`
- default `alpha = 0.60` (configurable)

If NIMA is absent:
- `A = L`

### Step 3: Final score

Default weights (configurable):
- `quality_score = 0.60*A + 0.20*S + 0.15*P + 0.05*B`

### Step 4: Deterministic tie-breaking

Sort descending by:
1. `quality_score`
2. `pixels`
3. `log1p(laplacian_var)`
4. `bytes`
5. stable image ID

This prevents unstable ordering for ties.

## Invalidating and Recomputing Scores

Trigger a full recompute when any of the following changes:
- OpenCLIP model or `pretrained` variant
- LAION head checkpoint
- NIMA enabled/disabled or NIMA weights/implementation
- thumbnail preprocessing for NIMA (size, colour handling, *pre-sharpen* requirement)
- combination weights or `alpha`

Mechanism:
- increment `scoring_version` in `scoring_config`
- wipe `aesthetic_laion`, `aesthetic_nima`, and `quality_score` (or mark as stale)
- recompute for all images (can be done incrementally, but must be consistent)

## Gotchas / Mitigations

### 1) Changing OpenCLIP model invalidates LAION scores
- LAION head is embedding-space specific.
- If user changes `openclip.model` or `openclip.pretrained`, wipe and recompute all LAION scores (and dependent `quality_score`).

### 2) NIMA should auto-disable without CUDA
- NIMA on CPU may be too slow for ingest throughput.
- Default: if no CUDA, set `nima_enabled=false` in global config and store NULL for `aesthetic_nima`.

### 3) NIMA must run on 400px thumbnail *before* sharpening
- Sharpening biases the aesthetic score.
- Always score the unsharpened resized pixels in-memory, prior to thumbnail sharpening/encoding.

### 4) bpp comparability across formats
- bpp is most meaningful for lossy formats (jpg/webp/avif/heic).
- Default: compute B on all images but keep low weight (0.05), or optionally compute B only for lossy formats and treat lossless as neutral.

### 5) Group-percentile normalisation is required
- Global scaling is unstable across content/cameras/lighting.
- The feature is explicitly designed for ranking within groups, so always use within-group percentiles.

## Acceptance Criteria

- On curated near-duplicate clusters, the top-ranked image is typically:
  - better composed / clearer subject (aesthetics proxy), and
  - not a visibly degraded encode when a better encode exists (guard rails).
- Sorting is deterministic across runs.
- Ingest overhead is acceptable on CUDA hosts; on CPU-only hosts NIMA is skipped automatically.
