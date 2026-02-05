# Plan: Fix Intermittent Scroll/Zoom Lag

## Scale Assumptions

- Up to **250,000 images** and **500,000+ faces**
- Unknown faces (candidates for reassessment/grouping) could be in the tens of
  thousands at any point
- **100-500 people**, most with custom matching thresholds
- Highly skewed face distribution: a handful of family/close friends with
  10,000+ faces each, a long tail of acquaintances with few faces, and a
  large "ignore" person that may accumulate the most faces of all

## Problem

User occasionally experiences periods where gallery scrolling and fullscreen
zoom/pan become laggy and difficult to control. The lag is **intermittent**,
suggesting a background trigger rather than a constant cost.

## Root Cause Analysis

Two independent sources compound:

1. **Backend GIL contention** (intermittent trigger) — Background processing
   threads hold the Python GIL during pure-Python loops that process similarity
   results. While numpy matrix multiplications release the GIL, the Python
   `for`/`while` loops that iterate results don't. During these loops, Flask
   cannot serve thumbnail or API requests, causing the browser to stall on
   pending fetches.

2. **Frontend forced reflows** (constant amplifier) — `getBoundingClientRect()`
   is called on every wheel, mousemove, and scroll event during zoom/pan,
   forcing layout recalculation. This is always present but becomes noticeable
   when combined with stalled network requests.

---

## Backend Fixes

### B1. Vectorize face reassessment matching loop

**File:** `faces.py:3046-3057`

**Current code** — pure Python loop holding GIL:
```python
matched = []
for i, candidate_face_id in enumerate(candidate_ids):
    best_idx = np.argmax(similarities[i])
    best_similarity = similarities[i, best_idx]
    _, matched_person_id = known_ids[best_idx]
    person_threshold = person_thresholds.get(matched_person_id)
    effective_threshold = person_threshold if person_threshold is not None else threshold
    if best_similarity >= effective_threshold:
        matched.append((candidate_face_id, matched_person_id, float(best_similarity)))
```

With N candidates and M known faces, this is N iterations of Python, each doing
`np.argmax` on a row (which releases GIL briefly but the loop overhead doesn't).
At scale: 50,000 unknown candidates × 200,000 known faces = 50,000 Python loop
iterations, each scanning a 200K-element row. The numpy calls release the GIL
per-row, but the loop re-acquires it 50,000 times.

**Fix:** Vectorize with numpy. The complication is per-person thresholds — most
people will have a custom threshold, so we need to build a threshold vector
indexed by known-face position.

```python
# Vectorized: find best match for every candidate in one operation
best_indices = np.argmax(similarities, axis=1)          # shape: (N,)
best_scores = similarities[np.arange(len(candidate_ids)), best_indices]  # shape: (N,)

# Build threshold vector for the known faces (one per column in similarities)
# This is indexed by known-face position, so we can look up the effective
# threshold for each candidate's best match in bulk.
known_thresholds = np.array([
    person_thresholds.get(pid, threshold)
    for _, pid in known_ids
], dtype=np.float32)  # shape: (M,) — built once, 500 people = trivial

# Look up the effective threshold for each candidate's best match
effective_thresholds = known_thresholds[best_indices]  # shape: (N,) — numpy fancy indexing

# Filter in bulk
above = best_scores >= effective_thresholds
matched = [
    (candidate_ids[i], known_ids[best_indices[i]][1], float(best_scores[i]))
    for i in np.where(above)[0]
]
```

The `np.argmax(similarities, axis=1)` call processes all candidates in one
GIL-releasing numpy operation. The `known_thresholds` array is built once from
the known-faces list (at most ~500K entries, but the list comprehension over
`known_ids` is simple indexing). The fancy-index lookup
`known_thresholds[best_indices]` is a single numpy operation.

**Risk:** Low. Same algorithm, same results, just vectorized. The
`known_thresholds` array is built once per reassessment cycle and reused for
all candidates.

---

### B2. Vectorize face grouping nested loop

**File:** `faces.py:2673-2681`

**Current code** — O(n²) pure Python nested loop:
```python
for local_idx in range(chunk_end - i):
    global_idx = i + local_idx
    face_id_i = face_ids[global_idx]
    for j in range(global_idx + 1, n_faces):
        if similarities[local_idx, j] >= threshold:
            face_id_j = face_ids[j]
            uf.union_ids(face_id_i, face_id_j)
```

With chunk_size=1000 and n_faces=500,000, each chunk does up to 500M Python
iterations in the inner loop. With 500 chunks total, that's 125 billion
iterations of pure Python — potentially minutes of continuous GIL holding.
This is by far the largest source of GIL contention.

**Fix:** Replace the nested loop with numpy bulk extraction:

```python
for local_idx in range(chunk_end - i):
    global_idx = i + local_idx
    # Find all j > global_idx where similarity >= threshold (vectorized)
    row = similarities[local_idx, global_idx + 1:]
    matches = np.where(row >= threshold)[0] + (global_idx + 1)
    face_id_i = face_ids[global_idx]
    for j_idx in matches:
        uf.union_ids(face_id_i, face_ids[j_idx])
```

The inner loop is now over only the matches (typically few) rather than all
faces. The `np.where` call releases the GIL during the bulk comparison.

At 500K faces this per-row approach still has 500K outer iterations of Python
(one per row in the chunk). Better: extract all above-threshold pairs from the
entire chunk at once, eliminating the outer Python loop too:

```python
# Zero out the lower triangle and diagonal to avoid duplicate pairs
chunk_sims = similarities.copy()
for local_idx in range(chunk_end - i):
    global_idx = i + local_idx
    chunk_sims[local_idx, :global_idx + 1] = 0.0

# Find all pairs above threshold in one numpy call
local_rows, cols = np.where(chunk_sims >= threshold)
for local_idx, j in zip(local_rows, cols):
    global_idx = i + local_idx
    uf.union_ids(face_ids[global_idx], face_ids[j])
```

This replaces the entire nested loop with a single `np.where` on the chunk
matrix — one GIL-releasing numpy call per chunk instead of chunk_size × n_faces
Python iterations. The remaining loop is only over actual matches (typically
sparse at reasonable thresholds).

The triangle masking loop (`chunk_sims[local_idx, :global_idx + 1] = 0.0`) is
at most chunk_size iterations (1000) per chunk, which is negligible. It could
also be replaced with `np.triu` if the chunk aligns with the diagonal, but the
offset arithmetic makes the explicit loop clearer and still fast enough.

**Risk:** Low-medium. Need to verify the triangle masking is correct. The
`copy()` adds memory but chunk_sims is already allocated (chunk_size × n_faces
floats — at 1000 × 500K that's ~2GB per chunk, which may need the chunk_size
reduced or a sparse approach for very large face counts). Test with known
grouping results to confirm equivalence.

**Note on memory at scale:** With 500K faces, each chunk's similarity matrix is
1000 × 500,000 × 4 bytes = ~2GB. The `copy()` doubles this. If memory is
tight, consider processing the `np.where` directly on the original matrix and
filtering out the lower-triangle pairs in the result loop instead of zeroing
them beforehand:

```python
local_rows, cols = np.where(similarities >= threshold)
for local_idx, j in zip(local_rows, cols):
    global_idx = i + local_idx
    if j > global_idx:  # Skip lower triangle and diagonal
        uf.union_ids(face_ids[global_idx], face_ids[j])
```

This avoids the copy entirely — same numpy call, just a cheap conditional in
the (sparse) result loop.

---

### B3. Add yields between embedding/face-detection batches

**File:** `imagedb.py` — EmbeddingThread.run() (~line 2311) and
FaceDetectionThread.run()

**Current:** After processing a batch, immediately loops back to collect the
next batch with no pause.

**Fix:** Add a brief `time.sleep(0.01)` (10ms) between batches. This is enough
to let Flask threads acquire the GIL and serve a few requests, without
meaningfully slowing batch processing (10ms per batch of 32 images is
negligible vs the ~500ms+ per batch of actual work).

```python
if batch_ids:
    self._process_batch(batch_ids, batch_paths)
    time.sleep(0.01)  # Yield GIL briefly for Flask request handling
```

Same pattern for FaceDetectionThread after its batch processing completes.

**Risk:** Very low. 10ms delay is imperceptible in a minutes-long indexing run.

---

## Frontend Fixes

### F1. Cache container rect in fullscreen zoom/pan

**File:** `fullscreen.js`

**Problem:** `getBoundingClientRect()` is called in three places during a single
wheel or mousemove event:

1. `_zoomAtPoint()` line 585 — gets container rect
2. `_constrainPan()` line 730 — gets container rect again
3. Both are called from `_handleWheel()` and `_handleMouseMove()`

Each `getBoundingClientRect()` forces the browser to calculate layout. During
rapid interaction (60+ events/sec), this causes measurable jank.

**Fix:** Cache the container rect and invalidate on resize. The container
dimensions don't change during zoom/pan — only on window resize.

```javascript
// Add to Fullscreen state:
_cachedContainerRect: null,

_getContainerRect() {
    if (!this._cachedContainerRect) {
        this._cachedContainerRect = this._els.container.getBoundingClientRect();
    }
    return this._cachedContainerRect;
},

_invalidateContainerRect() {
    this._cachedContainerRect = null;
},
```

Then:
- Replace `this._els.container.getBoundingClientRect()` in `_zoomAtPoint` and
  `_constrainPan` with `this._getContainerRect()`
- Call `_invalidateContainerRect()` in the existing resize handler and when
  entering/leaving fullscreen
- Also invalidate when the image changes (navigation) since the container
  might have different dimensions

**Risk:** Low. The cache is invalidated conservatively. Worst case is a stale
rect for one frame after a resize, which the debounced resize handler will fix.

---

### F2. Throttle `_showOverlays()` in high-frequency handlers

**File:** `fullscreen.js`

**Problem:** `_showOverlays()` is called on every wheel event and every
mousemove event. It does 4 `classList.remove()` calls, a
`_updateTaggingButton()` call, and clears/sets a timeout — all on every event.

**Fix:** Guard with a visibility flag. If overlays are already visible, skip
the DOM work and just reset the hide timer.

```javascript
_showOverlays() {
    if (!this._overlaysVisible) {
        this._els.filename.classList.remove('hidden');
        this._els.toolbar.classList.remove('hidden');
        this._els.prevBtn.classList.remove('hidden');
        this._els.nextBtn.classList.remove('hidden');
        this._updateTaggingButton();
        this._overlaysVisible = true;
    }

    // Reset hide timer (lightweight — just timer management)
    if (this._overlayTimeout) {
        clearTimeout(this._overlayTimeout);
    }
    this._overlayTimeout = setTimeout(() => {
        this._els.filename.classList.add('hidden');
        this._els.toolbar.classList.add('hidden');
        this._els.prevBtn.classList.add('hidden');
        this._els.nextBtn.classList.add('hidden');
        this._overlaysVisible = false;
        this._overlayTimeout = null;
    }, this.FILENAME_DISPLAY_MS);
},
```

When overlays are already visible (which they are during continuous
interaction), this reduces to just `clearTimeout` + `setTimeout` — no DOM
access at all.

**Risk:** Very low. Same behavior, just avoids redundant DOM operations.

---

### F3. Cache grid rect in gallery scroll overlay

**File:** `gallery.js`

**Problem:** `_updateScrollOverlayFromScroll()` calls `getBoundingClientRect()`
twice per scroll event (lines 965, 970). During rapid scrolling this forces
repeated layout calculations.

**Fix:** Cache the grid rect on overlay show (when `_scrollOverlayAnchor` is
set) and the overlay's own height (which doesn't change once shown). Invalidate
on resize.

```javascript
_showScrollOverlay(text, scrollTop) {
    // ... existing code to set text and show overlay ...

    // Cache rects when overlay first appears
    this._cachedGridRect = this._els.grid.getBoundingClientRect();
    this._cachedOverlayHeight = this._scrollOverlay.getBoundingClientRect().height;
    // ... set anchor ...
},

_updateScrollOverlayFromScroll(scrollTop) {
    if (!this._scrollOverlay || !this._scrollOverlayAnchor) return;

    const grid = this._els.grid;
    if (!grid) return;

    const scrollDelta = scrollTop - this._scrollOverlayAnchor.scrollTop;
    const scrollableHeight = grid.scrollHeight - grid.clientHeight;
    if (scrollableHeight <= 0) return;

    // Use cached rects instead of forcing reflow
    const trackHeight = this._cachedGridRect?.height || grid.getBoundingClientRect().height;
    const thumbDelta = (scrollDelta / scrollableHeight) * trackHeight;

    const newTop = this._scrollOverlayAnchor.overlayY + thumbDelta;
    const overlayHeight = this._cachedOverlayHeight || 30;
    const clampedTop = Math.max(8, Math.min(newTop, window.innerHeight - overlayHeight - 8));

    this._scrollOverlay.style.top = clampedTop + 'px';
},
```

**Risk:** Low. The grid rect and overlay height don't change during a scroll
gesture. Cache is refreshed each time the overlay appears.

---

## Implementation Order

| # | Fix | Impact | Risk | Rationale |
|---|-----|--------|------|-----------|
| 1 | B2 — Vectorize face grouping | Critical | Low-Med | 125B Python iterations at 500K faces; minutes of GIL |
| 2 | B1 — Vectorize reassessment | High | Low | 50K iterations every 2s; frequent GIL contention |
| 3 | F1 — Cache container rect | Medium | Low | Eliminates reflows during zoom/pan |
| 4 | F2 — Guard _showOverlays | Medium | Low | Reduces DOM work during rapid interaction |
| 5 | B3 — Yield between batches | Medium | Very low | Simple sleep; helps during long indexing |
| 6 | F3 — Cache grid rect | Low-Med | Low | Only affects gallery scroll overlay |

---

## Testing

- **B1/B2:** Run face reassessment and grouping on a large dataset. Compare
  matched results before/after to verify identical outputs. Monitor Flask
  response times during processing (should improve).
- **B3:** Run full indexing. Verify thumbnails load smoothly during processing.
  Check total indexing time hasn't regressed meaningfully.
- **F1:** Zoom/pan rapidly in fullscreen. Verify smooth interaction. Resize
  window while zoomed — verify pan constraints still correct.
- **F2:** Move mouse in fullscreen — overlays should appear/disappear normally.
  Rapid wheel zoom — should feel smoother.
- **F3:** Scroll gallery rapidly with date/rating sort. Verify overlay tracks
  correctly and doesn't jump on resize.
