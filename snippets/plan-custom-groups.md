# Plan: Custom Groups (Albums)

## Objective

Add a fifth slider position, "Custom", to the Groups screen (currently "Duplicates"). Custom groups are user-curated, named collections of images — equivalent to albums, but expressed as a natural extension of the existing group infrastructure rather than a separate feature.

Unlike the four automatic similarity levels (0–3), where an image belongs to at most one group per level, custom groups may freely overlap: the same image can appear in any number of custom groups.

---

## Screen Rename: Duplicates → Groups

The screen and its references throughout the codebase are renamed from "Duplicates" to "Groups". This reflects that the screen now serves both automatic duplicate detection and user-curated collections.

Rename scope:
- Navigation label in `index.html` (sidebar/toolbar)
- Screen ID and CSS class names
- Module name (`Duplicates` → `Groups` or keep `Duplicates` internally to minimise churn — **decision needed**)
- Tooltip text, empty-state messages
- `CLAUDE.md` documentation references

**Recommendation:** Keep the JS module name as `Duplicates` internally (avoids massive rename across every file that references `Duplicates.navigateToGroup()`, `AppState.duplicates`, filter types, etc.). Only rename the user-visible label from "Duplicates" to "Groups" in the UI. If needed, add comments noting this screen is now referred to as the Groups screen in user-faceing locations.

---

## Slider Extension

### Current slider (4 positions)

```
Slider:  0          1          2               3
Level:   3          2          1               0
Label:   Related    Similar    Near-identical   Identical
```

### New slider (5 positions)

```
Slider:  0        1          2          3               4
Level:   4        3          2          1               0
Label:   Custom   Related    Similar    Near-identical   Identical
```

The "Custom" position is at the left end (loosest grouping). Level 4 is used internally for custom groups.

**Changes:**
- `index.html`: `max="3"` → `max="4"` on the slider input
- `duplicates.js`: `SIMILARITY_LABELS` gains `'Custom'` at index 0
- `duplicates.js`: `_sliderToLevel()` / `_levelToSlider()` updated: `level = 4 - sliderValue`
- Label text update: left end now reads "Custom" instead of "Related"

---

## Data Model

### Option: Reuse `duplicate_groups` table

Custom groups use the same `duplicate_groups` table with `level = 4`. This reuses all existing infrastructure (cascade deletes, `_internal.removeImage()`, lightweight queries, group rendering).

**Additional table** for custom group metadata:

```sql
CREATE TABLE IF NOT EXISTS custom_groups (
    group_hash  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
```

The `group_hash` in `custom_groups` matches the `group_hash` in `duplicate_groups` where `level = 4`. This is a 1:1 join — every level-4 group has a corresponding row in `custom_groups` with its name and timestamps.

**Index:**
```sql
CREATE INDEX IF NOT EXISTS idx_custom_groups_name ON custom_groups(name)
```

### group_hash generation

For automatic levels, `group_hash` is derived from content (checksum, perceptual hash, cluster ID). For custom groups, generate a UUID: `custom_{uuid4}`. The `custom_` prefix makes them visually distinct in debug/logs.

### Image overlap

Automatic levels enforce one-group-per-image via the computation algorithm. Custom groups have no such constraint — the `duplicate_groups` table allows the same `image_id` to appear in multiple `group_hash` values at `level = 4`. No schema change needed.

---

## API Endpoints

### Existing (works unchanged)

| Method | Endpoint | Notes |
|--------|----------|-------|
| GET | `/api/duplicates?level=4` | Returns custom groups in the same lightweight format, plus `name` field |

The existing `get_duplicate_groups_lightweight()` query works for level 4. The response is extended to include the group name from `custom_groups`:

```json
{
    "groups": [
        {
            "group_hash": "custom_a1b2c3",
            "name": "Beach Holiday 2024",
            "count": 12,
            "image_ids": ["id1", "id2", ...],
            "best_image": {"id": "id1", "basename": "sunset.jpg"}
        }
    ],
    "status": "done",
    "epoch": "..."
}
```

For levels 0–3, the `name` field is absent (null/omitted). For level 4, `status` is always `"done"` (no computation needed).

### New endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/groups` | Create custom group `{name, image_ids}` |
| PATCH | `/api/groups/:hash` | Update group `{name}` (rename) |
| DELETE | `/api/groups/:hash` | Delete entire custom group |
| POST | `/api/groups/:hash/images` | Add images `{image_ids}` |
| DELETE | `/api/groups/:hash/images` | Remove images `{image_ids}` |

**POST `/api/groups`** — Create:
- Frontend generates `group_hash` (UUID with `custom_` prefix)
- Request: `{group_hash, name, image_ids}`
- Backend: inserts into `custom_groups` + `duplicate_groups` (level 4)
- Response: `{ok: true}`

**PATCH `/api/groups/:hash`** — Rename:
- Request: `{name}`
- Backend: updates `custom_groups.name` and `updated_at`
- Response: `{ok: true}`

**DELETE `/api/groups/:hash`** — Delete group:
- Backend: deletes from `custom_groups` and `duplicate_groups` where level=4 and group_hash=:hash
- Response: `{ok: true}`

**POST `/api/groups/:hash/images`** — Add images:
- Request: `{image_ids}`
- Backend: inserts into `duplicate_groups` (level 4, group_hash, each image_id). Skips images already in the group.
- Response: `{ok: true}`

**DELETE `/api/groups/:hash/images`** — Remove images:
- Request: `{image_ids}`
- Backend: deletes from `duplicate_groups` where level=4, group_hash=:hash, image_id IN (...)
- If group drops to 0 images, also delete from `custom_groups`
- Response: `{ok: true}`

---

## AppState Changes

### `appstate/duplicates.js`

Extend with custom group methods. All follow the standard optimistic-update pattern.

**New methods:**

```
createGroup(name, imageIds)     → POST /api/groups
renameGroup(groupHash, name)    → PATCH /api/groups/:hash
deleteGroup(groupHash)          → DELETE /api/groups/:hash
addImages(groupHash, imageIds)  → POST /api/groups/:hash/images
removeImages(groupHash, imageIds) → DELETE /api/groups/:hash/images
```

Each method:
1. Sync phase: update `_groupCache[4]` optimistically, broadcast
2. Async phase: API call, rollback on error

**`_internal.removeImage()` update:**
- Already iterates all cached levels and removes the image. Level 4 is handled automatically since it's in `_groupCache`. No change needed, but verify it also cleans up `custom_groups` if a group empties (the backend cascade handles this via the DELETE endpoint, but the optimistic local prune should also remove empty groups from the cache).

**`loadLevel(4)` behaviour:**
- No polling needed (status is always "done")
- Loads from `/api/duplicates?level=4` as normal

### Notes

- Custom groups are allowed to be empty; if all the images within the group are deleted, that doesn't delete the group - it needs to be manually deleted by the user.
- Deleting a group doesn't touch the images within it - it *doesn't* cause images to be deleted!

---

## Frontend: Groups Screen UI

### Level 4 (Custom) specific UI

When the slider is at "Custom" (level 4), the screen shows additional controls that are hidden for levels 0–3:

**Toolbar additions (visible only at level 4):**
- **New Group** button — creates an empty named group (prompts for name)
- **Rename** — rename selected group (single selection only)
- **Delete Group** — delete selected group(s) with danger confirmation
- **Filter Groups** - works much like the autocomplete face name mechanism (characters appear somewhere in string in same order). Reuse code, if at all possible
    - This would just be an input field next to the "Clear filter" button, with suitable `placeholder` text.

These buttons are placed in a new toolbar group that toggles visibility based on the current level. Ensure both Unicode (offline mode) and Material-Symbols (online mode) are supported.

**Stack display changes at level 4:**
- The stack label shows the group **name** instead of `"N images"`. The count is shown in smaller text below the name.
- Toolbar sort options:
    - Add sort alphabetically (default when moving into Level 4)
    - Sort "By Size", "By People", and "By similarity" still available.

**Empty state at level 4:**
- Instead of "No duplicates found at this level", show: "No custom groups yet. Select images in the Gallery and use 'Add to Group' to create one."

### Managing group membership from Gallery thumbnails

The primary workflow for adding/removing images to/from custom groups is via a **hover control on Gallery thumbnails**, following the same pattern as the Suppress/Ignore buttons on face cards in the Faces screen.

**Hover control:**
- A small button overlaid on the thumbnail (e.g. top-right corner), hidden by default, visible on hover via CSS `opacity` transition.
- Uses Material Symbols icon (`photo_prints`) with Unicode fallback U+1F5C2 (CARD INDEX DIVIDERS) for offline mode.
- Appears on all Gallery thumbnails (not just when viewing a custom group).
- Clicking opens the **Group Picker** modal.

**Group Picker modal** (modelled on the People Picker in Search):
- **Two-panel layout**: available groups on the left, groups this image belongs to on the right.
- **Search/filter input** at the top of the left panel — fuzzy substring match on group names, reusing the same matching logic as the face name autocomplete if possible.
- **Click** a group on the left to add the image to it (moves to right panel). **Click** a group on the right to remove the image from it (moves back to left).
- **Drag and drop** between panels also supported (same pattern as People Picker).
- **"New Group..."** button (or entry) at the top/bottom of the left panel — prompts for a name, creates the group, and immediately adds the image to it.
    - This is unique to the Group Picker; the People Picker has no analogue.
- **Done / Cancel** buttons. Done persists changes, Cancel discards.
- Each group item shows: group name, image count, and a small thumbnail of the group's best image.

Try to make the People Picker and Group Picker based upon the same code and CSS to the maximum extent possible - these will be extremely similar, might need to be careful about `title` (tooltips) text, of course. Much like we have a reusable Virtual Grid, we probably need a reusable Entity Picker modal.

**Batch operation:**
- If multiple images are selected in Gallery and the hover button is clicked on one of them, the picker operates on all selected images at once. Groups are added/removed for the entire selection. The right panel shows groups that *all* selected images belong to; groups that only *some* belong to are hidden.

---

## Backend Changes

### `duplicates.py`

**New functions:**

```python
def create_custom_group(conn, group_hash, name, image_ids)
def rename_custom_group(conn, group_hash, name)
def delete_custom_group(conn, group_hash)
def add_images_to_custom_group(conn, group_hash, image_ids)
def remove_images_from_custom_group(conn, group_hash, image_ids)
```

These are simple CRUD operations on `duplicate_groups` (level=4) and `custom_groups`.

**`_get_duplicate_groups_lightweight()` update:**
- For level 4, LEFT JOIN `custom_groups` to include the `name` field in the response
- For levels 0–3, no join needed (name is null/absent)

**Computation skip:**
- `compute_all()` must skip level 4 entirely. Custom groups are never auto-computed, only user-managed.
- Status for level 4 is always "done"

### `imagedb.py`

**Schema migration:**
- Add `custom_groups` table
- Add index on `custom_groups(name)`

**New methods on ImageDB:**
- Thin wrappers around the `duplicates.py` functions above
- Called by `app.py` endpoint handlers

### `app.py`

- Register the five new API endpoints
- Standard validation (group exists, image IDs valid, name non-empty)

---

## Files to Modify

At least the following files will need to be touched...

| File | Change |
|------|--------|
| `imagedb.py` | Schema migration (add `custom_groups` table + index), new ImageDB methods for custom group CRUD |
| `duplicates.py` | Custom group CRUD functions, skip level 4 in `compute_all()`, extend lightweight query for level 4 names |
| `app.py` | Five new API endpoints for custom group management |
| `static/index.html` | Slider `max="4"`, new toolbar buttons for level 4 (New Group, Rename, Delete Group), "Groups" nav label, `<dialog id="dialog-group-picker">` |
| `static/styles.css` | Styling for custom group toolbar buttons, name labels on stacks |
| `static/duplicates.js` | Slider 5-position support, level 4 UI (name display, toolbar toggle, empty state), CRUD action handlers |
| `static/appstate/duplicates.js` | Custom group methods (create, rename, delete, add/remove images), optimistic updates |
| `static/gallery.js` | Thumbnail hover control for group picker, "Remove from Group" toolbar button when viewing custom group, Group Picker dialog logic |
| `static/core.js` | Update navigation label from "Duplicates" to "Groups" |
| `demo-seed/tutorial.py` | Update tutorial slides: rename "Duplicates" references to "Groups", add slide(s) covering custom groups |
| `README.md` | Update screen descriptions, feature list, screenshots references |
| `CLAUDE.md` | Update Key Screens list, Duplicate Detection Levels table, API Endpoints |

---

## Interaction with Existing Features

### Image deletion cascade
When an image is deleted (from Gallery, Fullscreen, or Cull Weakest), the existing `ON DELETE CASCADE` on `duplicate_groups.image_id` removes it from all groups including custom ones. The frontend `_internal.removeImage()` already handles this across all cached levels. If a custom group drops to 0 images, it should be cleaned up (delete `custom_groups` row).

### Duplicate group navigation
`navigateToGroup()` already works generically — it uses `group_hash` and `image_ids`. Custom groups flow through the same path. The `sourceLevel` in the filter will be `4` for custom groups.

### Prev/next group navigation in Gallery
Already works by iterating `Duplicates.getGroups()`. Custom groups at level 4 will appear in this list when the slider is at "Custom".

### Quality sort in Gallery (snippets/plan-quality-metric.md)
When viewing a custom group in Gallery, quality sort applies the same within-group percentile ranking. No special handling needed — the group is just a set of image IDs.

We don't need to implement as a part of this plan - skip.

### Cull Weakest (snippets/plan-cull-weakest.md)
Works on custom groups the same way as automatic groups. The weakest image per selected group is identified and deleted. After culling, if a custom group drops to ≤1 images, it should probably be preserved (unlike automatic groups where a singleton is meaningless). **Decision:** Cull Weakest skips custom groups with ≤2 images (can't cull from a group that would become empty or singleton).

We don't need to implement as a part of this plan - skip.

---

## Migration

On first startup after schema migration:

1. Create `custom_groups` table
2. Add index `idx_custom_groups_name`
3. No data migration needed — custom groups start empty

---

## Gotchas / Mitigations

1. **Overlap allowed** — An image in multiple custom groups means deleting that image affects all of them. The cascade handles this correctly. The UI should not be confused by this (e.g. when removing an image from one group, it stays in others).

2. **Group auto-computation must skip level 4** — `compute_all()` currently iterates levels. Add an explicit check to skip level 4. Status for level 4 should never be "computing" or "pending".

3. **Group naming** — Names should be non-empty, trimmed, unique. Duplicates are not allowed. Max length: reasonable limit (255 chars).

4. **Empty groups** — A custom group with 0 images should be deleted automatically (backend cleanup). The UI never shows empty groups.

5. **Slider label width** — "Custom" is shorter than "Near-identical", so `min-width` on the label span should accommodate the longest label. Already the case since "Near-identical" sets the minimum.

6. **"Best image" for custom groups** — Uses the same resolution/size/quality ranking as automatic groups. Alternatively, let the user pick a cover image. Start with automatic selection; user-picked cover is a future enhancement.

---

## Verification

1. Slide to "Custom" → empty state message shown, "New Group" button enabled
2. Hover over Gallery thumbnail → group button appears, click → Group Picker opens
3. In Group Picker, click "New Group..." → enter name → group created, image added, shown in right panel
4. Close picker, slide to "Custom" on Groups screen → new group appears with correct count
5. Open custom group in Gallery → images displayed, prev/next group navigation works
6. Hover a thumbnail, open picker → existing groups shown, click to add/remove, drag between panels
7. Select multiple images, click hover button on one → picker operates on entire selection
8. Remove image from group via picker → image remains in Gallery, removed from group
9. Use "Remove from Group" toolbar button when viewing a custom group → removes selected images
10. Rename group on Groups screen → label updates
11. Delete group on Groups screen → group removed, images unaffected
12. Filter groups by name → only matching groups shown
8. Delete an image that's in multiple custom groups → removed from all groups
9. Same image in two custom groups: verified by checking both groups after adding
10. Cull Weakest on custom groups → weakest deleted, group preserved if ≥2 images remain
11. Slider positions 1–4 (automatic levels) behave exactly as before — no regression
12. Navigation label reads "Groups" throughout the UI
