# Face Recognition Update

## Faces Screen: Current State

This displays a thumbnail grid. The top-most thumbnails are named people, and following them are all the unknown (unnamed people). We have a checkbox in the toolbar to show "Only unknowns", so the named faces are hidden.

## Proposed Updates

### Toolbar Changes

**Change 1.** "Only unknowns" becomes a toggle button (e.g. `?` symbol or `help` icon) that can be selected/unselected. Move it to the **left toolbar** area, similar to how "min group size" is positioned on the Duplicates screen.

**Change 4.** Add a "focus on one person" toolbar button (`center_focus_strong` icon) to the left toolbar. States:
- **Disabled + unselected**: Default state when viewing all named faces (preferred thumbnails only)
- **Enabled + unselected**: Exactly one named face is selected
- **Enabled + selected**: Viewing all aliases for a specific person

Clicking when enabled+unselected enters focus mode (same as double-clicking the selected thumbnail). Clicking when enabled+selected exits focus mode and returns to the all-named-faces view.

### Known Faces Section

**Change 2.** When showing all named faces, display only the **preferred thumbnail** for each unique person, sorted alphabetically (case-insensitive). Uses the existing `preferred_face_id` column in the `people` table.

**Change 3.** Double-clicking a named face (or clicking the focus button with one selected) enters "pick preferred face" mode:
- Only thumbnails identified as that person are shown
- All other named faces are hidden temporarily
- Each thumbnail has a **star icon overlaid** on its corner:
  - **Gold star**: The current preferred face for this person
  - **Grey star**: Other faces (aliases) for this person
- Clicking a grey star makes that face the preferred one (becomes gold, previous gold becomes grey)
- There must ALWAYS be exactly one preferred face per person

The star overlay works well because face thumbnails are cropped circular, so corner overlays don't obscure the face much.

**Change 5.** Sort order for named faces (preferred only view): **alphabetical by name**, case-insensitive.

**Change 6.** Sort order for "pick preferred face" mode: **by timestamp** of the source image.

### Unknown Faces Section

**Change 7.** Add a face grouping stage to database indexing that runs after face detection/embedding:
- Groups unknown faces by embedding similarity (similar to Duplicate Finding Level 2)
- Uses configurable threshold: `face_grouping_similarity` (new config value, default ~0.65)
- Persisted in a new database table (e.g. `unknown_face_groups`)
- Reuse/adapt existing duplicate finding code for performance

**Important:** Unknown face groups must be recalculated when:
- New faces are detected during indexing
- Faces are identified (named) - they leave the unknown pool
- Faces are unidentified - they return to the unknown pool
- Faces are suppressed/deleted

**Change 8.** Sort unknown face thumbnails by:
1. **Group size** (descending): Largest groups first, then smaller groups, then singletons
2. **Within each group** (or among singletons): By source image timestamp

When "reverse sort" is toggled, reverse **both** orderings (smallest groups first, oldest timestamps first within groups).

This surfaces frequently-photographed people at the top of the unknown list, with their faces clustered together for easy batch identification.

### Keyboard/Selection Behaviour

- In "pick preferred face" mode, **Delete key** on selected faces should **unassign them** from the person (return to unknown faces), not suppress them
- Standard selection model (click, Ctrl+click, Shift+click, drag-box) applies throughout
- Clicking the star to set preferred does NOT require the face to be selected first

## UI Flow Recap

1. **Initial state**: Preferred thumbnail for each named person shown, sorted A-Z. Focus button disabled+unselected.
2. **User selects one named face**: Focus button becomes enabled (still unselected).
3. **User double-clicks or clicks focus button**: Enters "pick preferred" mode for that person. Focus button becomes selected. Selection is cleared. Unknown faces section hidden.
4. **User clicks star on a face**: That becomes the preferred face (gold star).
5. **User clicks focus button again**: Exits "pick preferred" mode. Returns to all-named-faces view. Focus button becomes disabled+unselected.

## Implementation Notes

- The `people` table already has `preferred_face_id` - use this
- Consider adding `face_grouping_similarity` to config.py (default: 0.65)
- Unknown face group table schema suggestion:
  ```sql
  CREATE TABLE unknown_face_groups (
      group_id TEXT PRIMARY KEY,
      face_ids TEXT,  -- JSON array of face IDs
      size INTEGER,
      created_at TEXT,
      updated_at TEXT
  );
  ```
- Alternatively, add a `group_id` column to the `faces` table for unknown faces
