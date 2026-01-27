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




Face Recognition Update - Verification Checklist

  Toolbar Changes

  - "Only unknowns" is a toggle button (not checkbox) in left toolbar
  - "Only unknowns" button uses help icon
  - "Only unknowns" button shows .active class when enabled
  - Focus button exists with center_focus_strong icon
  - Focus button is disabled when no person card is selected
  - Focus button is enabled when exactly one person card is selected
  - Focus button shows .active class when in pick-preferred mode
  - Focus button tooltip changes based on state ("Focus on one person" vs "Exit focus mode")

  Known Faces Section

  - Shows only ONE card per person (preferred face thumbnail)
  - Uses /api/people/{id}/thumbnail endpoint (not face thumbnail)
  - Sorted alphabetically by name (case-insensitive)
  - Sort direction toggle reverses alphabetical order (Z-A)
  - Person cards have .person-card class for identification
  - Person cards show face count badge when person has multiple faces
  - Single-click on person card toggles selection
  - Only one person card can be selected at a time
  - Double-click on person card enters pick-preferred mode

  Edge cases:
  - Person with only 1 face - no badge shown, pick-preferred still works
  - Person with 0 faces (deleted all) - should not appear / person deleted
  - Two people with same name (different case) - both appear, sorted correctly
  - Very long person name - truncated with ellipsis

  Pick-Preferred Mode

  - Header shows person name and hint text
  - Shows ALL faces for the selected person (not just preferred)
  - Each face has star overlay in corner
  - Preferred face has gold star (.preferred class)
  - Non-preferred faces have grey star
  - Clicking star sets that face as preferred (API call)
  - After star click, only one gold star exists
  - Previous preferred face's star becomes grey
  - Faces sorted by image timestamp (oldest first by default)
  - Sort direction toggle reverses timestamp order
  - VirtualGrid used for performance with many faces
  - Delete key unassigns selected faces (returns to unknown pool)
  - Delete triggers confirmation dialog
  - After unassign, faces removed from pick-preferred view
  - Unassigned faces appear in unknown section on exit
  - Focus button click exits pick-preferred mode
  - Exiting mode returns to normal known/unknown view
  - Unknown faces section hidden during pick-preferred mode

  Edge cases:
  - Unassign the preferred face - new preferred auto-selected from remaining
  - Unassign ALL faces - person deleted, exit pick-preferred mode automatically
  - Star click while face is selected - works (selection not required)
  - Double-click face in pick-preferred - sets as preferred (same as star)
  - Enter pick-preferred for person with 100+ faces - VirtualGrid handles it

  Unknown Faces Section

  - Uses VirtualGrid (not static DOM)
  - Uses /api/faces/{id}/thumbnail for thumbnails
  - unknown_group_id column exists in faces table
  - Faces grouped by embedding similarity (threshold ~0.65)
  - Groups sorted by size descending (largest first)
  - Within groups, sorted by image timestamp
  - Singletons (no group) appear after grouped faces
  - Sort direction toggle reverses both orderings
  - group_size field included in API response
  - image_timestamp field included in API response
  - Input field for naming (autocomplete works)
  - Naming a face triggers group recalculation
  - Selection works (click, Ctrl+click, Shift+click, drag-box)
  - Delete key suppresses selected faces

  Edge cases:
  - 0 unknown faces - empty state shown, VirtualGrid handles gracefully
  - 1 unknown face - shown as singleton (group_size = 1)
  - 1000+ unknown faces - VirtualGrid virtualization works
  - Name an unknown face - it leaves unknown section immediately
  - Group recalc while viewing - UI updates appropriately
  - Two faces with identical embedding - grouped together

  API Endpoints

  GET /api/faces
  - Returns unknown_group_id for unknown faces
  - Returns group_size for unknown faces
  - Returns image_timestamp for all faces
  - Returns is_preferred for known faces
  - unknown_only=true parameter works

  GET /api/people/{id}/faces
  - Returns all faces for person
  - Includes is_preferred field
  - Includes image_timestamp field
  - Sorted by timestamp

  POST /api/people/{id}/set-preferred
  - Requires face_id in body
  - Returns error if face doesn't belong to person
  - Returns error if person not found
  - Returns error if face not found
  - Updates preferred_face_id in people table
  - Returns updated person object

  POST /api/faces/{id}/unassign
  - Sets person_id to NULL
  - If was preferred face, auto-selects new preferred from remaining
  - If no faces remain, deletes the person
  - Triggers group recalculation
  - Returns updated/deleted person info

  GET /api/faces/group-status
  - Returns status field ('idle', 'computing', 'done', 'error')
  - Returns n_groups when done

  Database Schema

  - faces.unknown_group_id column exists (TEXT, nullable)
  - Migration runs on startup for existing databases
  - Group IDs are short UUIDs (8 chars)
  - Group IDs cleared when face is identified

  Group Computation

  - Uses UnionFind algorithm (imported from duplicates.py)
  - Chunked matrix multiplication for memory efficiency
  - Runs asynchronously in background thread
  - Only one computation runs at a time
  - Triggered after: face identification, unassignment, suppression
  - Clears existing group IDs before computing new ones
  - Only groups faces with person_id IS NULL AND suppressed = 0

  Performance considerations:
  - Chunk size of 1000 for similarity computation
  - Uses pre-normalized embeddings (no re-normalization)
  - Index on unknown_group_id for query performance
  - VirtualGrid only renders visible faces (~50 at a time)
  - ThumbnailLoader limits concurrent requests
  - Grouping computation doesn't block UI

  Screen Transitions

  - VirtualGrid bound on screen enter
  - VirtualGrid unbound on screen leave
  - Selection unbound on screen leave
  - Scroll position restored on re-enter
  - Pick-preferred mode exits when leaving faces screen
  - needsRefresh flag triggers reload on next enter

  Integration with Other Features

  - "Only unknowns" toggle works correctly with pick-preferred mode (exits first)
  - Database changes trigger needsRefresh
  - Reassessment completion reloads faces
  - Tagging mode in fullscreen still works for unknown faces
  