# Plan: Image Deletion Fixes + "Cull Weakest" Button

## Context

Two related issues to address together:

1. **Bug: Gallery/Fullscreen deletion doesn't delete files from disk.** Both `Gallery._deleteImages()` and `Fullscreen._deleteAndAdvance()` call `AppState.images.delete(ids)` without `{ deleteFiles: true }`, so only a soft delete (`deleted=1`) is performed. The file remains on disk.

2. **Feature: "Cull Weakest" button on the Duplicates screen.** A toolbar button that identifies and batch-deletes the weakest image from each selected duplicate group. This shares the same deletion cascade (faces, people, duplicates pruning, file removal) as the Gallery/Fullscreen delete, so the code must not be duplicated.

## Files to Modify

| File | Change |
|------|--------|
| `imagedb.py` | Add `laplacian_var` to lightweight + delta image queries |
| `static/appstate/images.js` | Change `deleteFiles` default from `false` to `true` |
| `static/gallery.js` | Use danger confirm dialog for deletion |
| `static/fullscreen.js` | Use danger confirm dialog for deletion |
| `static/index.html` | Add cull toolbar button + separator after "Min size" control |
| `static/styles.css` | Danger-hover styling for cull button |
| `static/core.js` | Extend `App.confirm()` with danger mode + custom button text |
| `static/duplicates.js` | Button wiring, weakest-selection algorithm, optimistic pruning |

---

## 0. Fix: Delete Files from Disk by Default

### 0a. `AppState.images.delete()` — change default

**`static/appstate/images.js`** — `delete()` at line 467

Currently `deleteFiles` defaults to `false` (soft delete). Change to `true`:

```javascript
const { deleteFiles = true } = options;
```

This single change fixes both Gallery and Fullscreen deletion — they both call `AppState.images.delete(ids)` without options, so they'll now hard-delete by default. The backend already supports `?delete_file=true` (app.py line 523) and handles the cascade correctly (`ON DELETE CASCADE` for faces).

No callers currently pass `{ deleteFiles: false }` explicitly, so nothing breaks.

### 0b. Use danger confirmation for Gallery deletion

**`static/gallery.js`** — `_deleteImages()` at line 1500

Currently uses `App.confirm('Delete Images', message)` with the default primary-blue OK button. Change to use the new danger mode (from step 2 below):

```javascript
const confirmed = await App.confirm('Delete Images', message, { danger: true, okText: 'Delete' });
```

This makes the destructive nature of the operation clearer now that it actually removes files from disk.

### 0c. Add confirmation to Fullscreen deletion

**`static/fullscreen.js`** — `_deleteAndAdvance()` at line 1280

Currently deletes immediately with **no confirmation dialog**. This was perhaps acceptable for soft-delete but is not acceptable for file-from-disk deletion. Add a danger confirmation before proceeding:

```javascript
const confirmed = await App.confirm(
    'Delete Image',
    'Permanently delete this image?',
    { danger: true, okText: 'Delete' }
);
if (!confirmed) return;
```

---

## 1. Add `laplacian_var` to Lightweight Image Data

**`imagedb.py`** — `get_all_images_lightweight()` (line 797) and `get_images_delta()` (line 857)

Add `laplacian_var` to both SELECT clauses. This makes the focus/sharpness score available client-side for the weakest-selection algorithm without any extra API calls. It's a single REAL column (~8 bytes per image in JSON).

---

## 2. Enhance `App.confirm()` with Danger Mode

**`static/core.js`** — `confirm()` at line 1297

Add optional third parameter `options = {}`:

- `options.danger` — swap OK button from `.primary` to `.danger` class (red, already defined in CSS at line 3502)
- `options.okText` — custom label for OK button (e.g. "Delete")

Reset both in `cleanup()` so subsequent calls aren't affected. The `.action-btn.danger` CSS already exists and is currently unused.

---

## 3. Add Toolbar Button

**`static/index.html`** — after the `min-group-size-control` div (line 173)

```html
<div class="toolbar-separator"></div>
<button id="btn-dup-cull-weakest" class="toolbar-btn"
        title="Delete the weakest image from each selected group" disabled>
    <span class="icon" data-icon="auto_delete">🗑</span>
</button>
```

Starts disabled; enabled when groups are selected.

**`static/styles.css`** — icon turns red on hover when enabled:

```css
#btn-dup-cull-weakest:not(:disabled):hover {
    color: var(--color-danger);
}
```

---

## 4. Core Logic in Duplicates Module

**`static/duplicates.js`** — three new methods plus wiring.

### 4a. Wiring

- Add `btnCullWeakest: App.$('btn-dup-cull-weakest')` to `_els` in `init()`
- Bind click: `this._els.btnCullWeakest.addEventListener('click', () => this._cullWeakest())`
- Update enabled state in `onSelectionChanged` callback: `this._els.btnCullWeakest.disabled = hashes.length === 0`
- Also sync button state in `onEnter()`

### 4b. `_findWeakest(group)` — Weakest-Selection Algorithm

For each image ID in `group.image_ids`, look up metadata via `AppState.images.getById(id)`. Compare using the ranking (lowest = weakest):

1. Resolution `(width * height)` — ascending (lowest is weakest)
2. File size — ascending (smallest is weakest)
3. Laplacian variance — ascending (most blurry is weakest)
4. Basename length — **descending** (longest is weakest, e.g. "Foo (2).jpg")

Returns the single weakest image ID, or null if group has ≤1 images.

This is the inverse of the backend's "best image" ranking in `duplicates.py` line 1917:
```sql
ORDER BY (i.width * i.height) DESC, i.lossless DESC, i.size DESC, i.laplacian_var DESC, i.id ASC
```

### 4c. `_cullWeakest()` — Main Handler

```
1. Collect weakest image ID from each selected group via _findWeakest()
2. Skip groups where _findWeakest returns null
3. If nothing to cull, return early
4. Show danger confirmation:
   App.confirm('⚠️ Cull Weakest', message, { danger: true, okText: 'Delete' })
   Message: "Delete the weakest image from each of N groups (N images total)?"
5. On confirm:
   a. OPTIMISTIC LOCAL UPDATE (instant):
      - Prune culled IDs from state.allGroups[].image_ids
      - Update group.count on each surviving group
      - Remove groups that drop to ≤1 images
      - Re-apply min-group-size filter → state.groups
      - Clear selection (some selected groups may have been removed)
      - Re-render immediately
   b. PERSIST (async):
      - await AppState.images.delete(toCull)
        This handles the full cascade: face cleanup, people cleanup,
        duplicates._internal.removeImage, images cache, API DELETE calls
      - On error: flag needsRefresh, reload groups from backend
```

**Why optimistic local update is needed:** `AppState.images.delete()` uses `queueTransaction()` which chains via `.then()`, so the cascade runs on the next microtick — not synchronously. If we only called `images.delete()` and then refreshed, the UI wouldn't update until all sequential API DELETE calls completed. By pruning the local `state.allGroups`/`state.groups` arrays first, the Duplicates grid re-renders instantly.

**Why `best_image` doesn't need recomputing:** We always delete the weakest, never the best. In a 2-image group, deleting the weakest removes the group entirely (drops to 1 image). So surviving groups always still contain their original `best_image`.

---

## Key Design Decisions

1. **Hard delete by default** — `AppState.images.delete()` now defaults to `deleteFiles: true`. All deletion paths (Gallery, Fullscreen, Cull Weakest) go through the same method, so there's one place to change the default. The backend cascade (`ON DELETE CASCADE` on faces) handles DB cleanup for hard deletes.

2. **No code duplication** — all three deletion UIs (Gallery batch, Fullscreen single, Cull Weakest batch) call `AppState.images.delete(ids)`. The cascade logic (face cleanup, people cleanup, duplicates pruning, API calls) lives in one place. Each UI only adds its own confirmation dialog and post-delete UI refresh.

3. **Danger confirmation everywhere** — now that deletion removes files from disk, all three paths use `App.confirm()` with `{ danger: true }` (red button, focus on Cancel). Fullscreen previously had no confirmation at all.

4. **No duplicate recomputation** — `_internal.removeImage()` prunes the in-memory AppState cache. Cull Weakest's local pruning mirrors that for instant UI. No backend `/api/duplicates` call needed.

5. **One image per group** — the cull button deletes exactly one image (the weakest) per selected group, not "all but the best." This is conservative and predictable.

6. **`laplacian_var` in lightweight data** — adding ~8 bytes/image avoids an extra round-trip and keeps the optimistic update pattern pure.

---

## Verification

### Deletion fix
1. Select an image in Gallery, delete it → file should be gone from disk
2. Open an image in Fullscreen, press Ctrl+Backspace → confirmation dialog appears (previously it didn't), confirm → file gone from disk
3. Gallery batch delete (select multiple, Delete key) → danger dialog with red button, files removed

### Cull Weakest
4. Open Duplicates screen, select one or more groups
5. Verify the cull button is enabled (disabled with no selection)
6. Click the cull button → danger confirmation with red "Delete" button, focus on Cancel
7. Press Cancel → nothing happens
8. Click Delete → groups immediately update (reduced count, removed groups), files gone from disk
9. Switch to Gallery → deleted images are gone
10. Switch to Faces screen → faces from deleted images are cleaned up
11. Verify people with no remaining faces are auto-deleted
12. Error case: if backend is down, UI should reload to consistent state after error
