# Audit 07 — Extend/Adapt Existing Code Over Re-inventing

## Principle

> Extend/adapt existing code over re-inventing or duplicating

## Scope

- `app/video.py` vs `app/thumbnails.py` — thumbnail/sharpening duplication
- `app/imagedb.py` vs `app/nima.py` vs `app/faces.py` — OOM pattern duplication
- `app/video.py` vs `app/rawimage.py` — image processing duplication
- `app/faces.py` vs `app/video.py` — background blur/padding duplication
- Database query patterns across `app/app.py`, `app/faces.py`, `app/imagedb.py`

## Findings

### 1. Duplicated Thumbnail Sharpening Constants (3 locations, inconsistent values)

UnsharpMask sharpening is applied independently in three places with **different parameters**:

| Location | radius | percent | threshold |
|----------|--------|---------|-----------|
| `thumbnails.py:64-67` (constants) + `thumbnails.py:217` (apply) | 1.0 | **60** | 3 |
| `video.py:307` (`extract_keyframe_thumbnail()`) | 1.0 | **40** | 3 |
| `video.py:658` (`generate_scene_thumbnails()`) | 1.0 | **40** | 3 |

The video code duplicates the entire thumbnail generation pipeline (resize, sharpen, save) without reusing `thumbnails.py`. The different `percent` value (40 vs 60) may be intentional for video frames but isn't documented as such.

### 2. Duplicated OOM Fallback Pattern (batch → single-item)

The two-tier OOM fallback pattern is hand-copied across batch processors:
- `imagedb.py:2688-2731` — OpenCLIP batch encoding
- `imagedb.py:3936-3967` — NIMA batch scoring
- `faces.py:716-759` — face embedding batch

All three implement identical logic: try `torch.stack().to(device)` → catch `MemoryError`/`RuntimeError` → fall back to single-item loop → `torch.cuda.empty_cache()`. No shared utility function exists.

### 3. Video Frame Padding Duplicates Face Thumbnail Blurring

- `video.py:576-621` (`_fit_frame_to_16_9()`) — pillarbox with blurred background
- `faces.py:1785+` — face thumbnail with blurred background

Both implement "blur original image → paste sharp crop on top" independently.

### 4. Database Query Pattern Repetition

Face person updates appear in multiple locations without a centralised query builder:
- `faces.py:1060` — `INSERT INTO people`
- `faces.py:1177` — `UPDATE people`
- `faces.py:1269` — `UPDATE people SET preferred_face_id`
- `app.py:4346` — `UPDATE faces SET manually_tagged`

Each site constructs SQL independently. This is a common pattern in Flask/SQLite apps and the duplication is moderate — not a structural concern but increases maintenance burden.

## Status

**Mostly Compliant** (after fix)

## Actions

- ~~**P2**: Extract shared thumbnail sharpening~~ — **FIXED**: Added `sharpen_thumbnail(img, *, video=False)` in `thumbnails.py` with `VIDEO_SHARPEN_PERCENT = 40` (documented: lighter sharpening avoids amplifying video compression artefacts). Both `video.py` call sites updated to use the shared function
- **P3**: Consider extracting OOM batch fallback into a shared utility — low priority since the pattern is stable and infrequently modified
- **P3**: Consider extracting blurred-background padding into a shared utility — low priority as the implementations serve different aspect ratio targets
