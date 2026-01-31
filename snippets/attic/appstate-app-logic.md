# AppState App Logic - Pseudocode Analysis

This document enumerates all user actions and traces through the app logic flows,
identifying which state domains are touched and what internal subroutines are needed.

---

## Read-Only Accessors (Synchronous)

These are synchronous reads from cached state. No transactions needed.
Used by UI components for display and autocomplete.

### People Domain
```
people.getAll() → person[]
    // Returns all cached people, sorted by name
    // Used by: autocomplete dropdown, people filter in search

people.get(person_id) → person | null
    // Get single person by ID
    // Used by: displaying person details

people.findByName(name) → person | null
    // Case-insensitive exact match
    // Used by: Identify Face (to check if person exists)

people.search(query) → person[]
    // Prefix/substring match, case-insensitive
    // Returns matches sorted by relevance (face_count desc, then name)
    // Used by: autocomplete filtering as user types

people.isLoaded() → boolean
    // Check if cache is populated
```

### Faces Domain
```
faces.getAll() → face[]
    // All non-suppressed faces
    // Used by: faces screen grid

faces.get(face_id) → face | null
    // Single face by ID

faces.getForPerson(person_id) → face[]
    // All faces for a person
    // Used by: pick-preferred mode, person detail view

faces.getForImage(image_id) → face[]
    // All faces on an image
    // Used by: face tagging overlay in fullscreen

faces.getUnknown() → face[]
    // Faces without a person_id
    // Used by: faces screen "unknown" section

faces.findFirstForPerson(person_id, options?) → face | null
    // Get first face for person (for setting new preferred)
    // options.excluding: image_id to exclude (when deleting image)

faces.isLoaded() → boolean
```

### Images Domain
```
images.getAll() → image[]
    // All non-deleted images

images.get(image_id) → image | null

images.getByFolder(folder_path) → image[]
    // Images in a specific folder
    // Used by: Remove Folder (to find affected images)

images.isLoaded() → boolean
```

### Duplicates Domain
```
duplicates.getGroups(level) → group[]
    // Groups at similarity level 0-3

duplicates.getGroup(group_hash) → group | null

duplicates.isLoaded(level) → boolean
```

---

## UI Component Workflows (Read-Only)

### Person Name Autocomplete
```
COMPONENT: Autocomplete popup for naming faces

ON FOCUS (input field gains focus):
    // Pre-populate dropdown with all people
    suggestions = people.getAll()
    RENDER dropdown with suggestions

ON INPUT (user types):
    query = input.value
    IF query is empty:
        suggestions = people.getAll()
    ELSE:
        suggestions = people.search(query)

    // Show "Create new: {query}" option if no exact match
    exact_match = people.findByName(query)
    IF exact_match is null AND query is not empty:
        suggestions = [{ isCreateNew: true, name: query }, ...suggestions]

    RENDER dropdown with suggestions

ON SELECT (user picks suggestion):
    IF suggestion.isCreateNew:
        → CALL Identify Face (face_id, suggestion.name)  // Will create person
    ELSE:
        → CALL Identify Face (face_id, suggestion.name)  // Will link to existing

ON BLUR / ESCAPE:
    HIDE dropdown
```

Note: The autocomplete is purely read-only until the user confirms selection.
All reads are synchronous from cached state - no async/API calls during typing.

---

## Face Operations

### 1. Identify Face (Name a Face)
```
INPUT: face_id, name_string

IF name_string is empty or whitespace:
    → GOTO Unidentify Face

face = faces.get(face_id)
IF face is null:
    → ERROR: face not found

normalized_name = trim(name_string)
IF face.person_name equals normalized_name (case-insensitive):
    → NO-OP (already has this name)

old_person_id = face.person_id  // may be null

// Find or create target person
person = people.findByName(normalized_name)  // case-insensitive
IF person is null:
    person = people.create(normalized_name)
    // New person has no preferred face yet

// Link face to person
face.person_id = person.id
face.person_name = person.name
API: POST /faces/{face_id}/identify { person_id }
DIRTY: faces

// Update new person's face count
person.face_count++
DIRTY: people

// Set preferred face if person had none
IF person.preferred_face_id is null:
    person.preferred_face_id = face_id
    API: POST /people/{person_id}/set-preferred { face_id }
    // people already dirty

// Handle old person (if face was previously identified)
IF old_person_id is not null AND old_person_id != person.id:
    old_person = people.get(old_person_id)
    old_person.face_count--

    IF old_person.face_count == 0:
        people.delete(old_person_id)
        API: DELETE /people/{old_person_id}
    ELSE IF old_person.preferred_face_id == face_id:
        // Need new preferred - pick first remaining face
        new_preferred = faces.findFirstForPerson(old_person_id)
        old_person.preferred_face_id = new_preferred.id
        API: POST /people/{old_person_id}/set-preferred { new_preferred.id }

    DIRTY: people  // (already dirty, but noting it)
```

### 2. Unidentify Face
```
INPUT: face_id

face = faces.get(face_id)
IF face is null OR face.person_id is null:
    → NO-OP

old_person_id = face.person_id
old_person = people.get(old_person_id)

// Unlink face
face.person_id = null
face.person_name = null
API: POST /faces/{face_id}/unidentify
DIRTY: faces

// Update old person
old_person.face_count--

IF old_person.face_count == 0:
    people.delete(old_person_id)
    API: DELETE /people/{old_person_id}
ELSE IF old_person.preferred_face_id == face_id:
    new_preferred = faces.findFirstForPerson(old_person_id)
    old_person.preferred_face_id = new_preferred.id
    API: POST /people/{old_person_id}/set-preferred { new_preferred.id }

DIRTY: people
```

### 3. Suppress Face (Mark as Not-a-Face)
```
INPUT: face_id

face = faces.get(face_id)
IF face is null:
    → ERROR: face not found

// If face was identified, unidentify first
IF face.person_id is not null:
    → CALL Unidentify Face (face_id)  // internal call

// Remove from faces
faces.remove(face_id)
API: POST /faces/{face_id}/suppress
DIRTY: faces
```

### 4. Rename Person
```
INPUT: person_id, new_name

IF new_name is empty:
    → ERROR: name cannot be empty

person = people.get(person_id)
IF person is null:
    → ERROR: person not found

normalized_name = trim(new_name)
IF person.name equals normalized_name (case-insensitive):
    → NO-OP

// Check if name already exists (would be a merge)
existing = people.findByName(normalized_name)
IF existing is not null AND existing.id != person_id:
    → CALL Merge People (person_id → existing.id)
    RETURN

// Simple rename
person.name = normalized_name
API: PATCH /people/{person_id} { name }
DIRTY: people

// Update all faces with this person to reflect new name
FOR each face WHERE face.person_id == person_id:
    face.person_name = normalized_name
DIRTY: faces
```

### 5. Merge People
```
INPUT: source_person_id, target_person_id

IF source_person_id == target_person_id:
    → NO-OP

source = people.get(source_person_id)
target = people.get(target_person_id)

// Reassign all faces from source to target
FOR each face WHERE face.person_id == source_person_id:
    face.person_id = target_person_id
    face.person_name = target.name
    API: POST /faces/{face.id}/identify { person_id: target_person_id }
DIRTY: faces

// Update target's face count
target.face_count += source.face_count

// Delete source person
people.delete(source_person_id)
API: DELETE /people/{source_person_id}
DIRTY: people
```

### 6. Set Preferred Face
```
INPUT: person_id, face_id

person = people.get(person_id)
face = faces.get(face_id)

IF person is null OR face is null:
    → ERROR

IF face.person_id != person_id:
    → ERROR: face does not belong to this person

IF person.preferred_face_id == face_id:
    → NO-OP

person.preferred_face_id = face_id
API: POST /people/{person_id}/set-preferred { face_id }
DIRTY: people
```

---

## Image Operations

### 7. Delete Image
```
INPUT: image_id

image = images.get(image_id)
IF image is null:
    → ERROR

// Handle faces on this image
image_faces = faces.getForImage(image_id)
FOR each face in image_faces:
    IF face.person_id is not null:
        person = people.get(face.person_id)
        person.face_count--

        IF person.face_count == 0:
            people.delete(person.id)
            API: DELETE /people/{person.id}
        ELSE IF person.preferred_face_id == face.id:
            new_preferred = faces.findFirstForPerson(person.id, excluding: image_id)
            IF new_preferred:
                person.preferred_face_id = new_preferred.id
                API: POST /people/{person.id}/set-preferred { new_preferred.id }
            // else person will have faces but no preferred - edge case

    faces.remove(face.id)
DIRTY: faces, people

// Remove from duplicate groups
duplicates.removeImage(image_id)
DIRTY: duplicates

// Mark image deleted
images.remove(image_id)
API: DELETE /images/{image_id}
DIRTY: images
```

### 8. Delete Multiple Images
```
INPUT: image_ids[]

// GUI batches the intent, calls this once
FOR each image_id in image_ids:
    → CALL Delete Image (image_id)  // internal calls

// One set of notifications at end for all dirty domains
```

### 9. Rate Image
```
INPUT: image_id, rating

image = images.get(image_id)
IF image is null:
    → ERROR

IF image.rating == rating:
    → NO-OP

image.rating = rating
API: POST /images/{image_id} { rating }
DIRTY: images
```

### 10. Update Image Description
```
INPUT: image_id, description

image = images.get(image_id)
IF image is null:
    → ERROR

image.description = description
API: POST /images/{image_id} { description }
DIRTY: images
```

### 11. Rotate Image
```
INPUT: image_id, direction (cw|ccw)

image = images.get(image_id)
IF image is null:
    → ERROR

// Swap dimensions
[image.width, image.height] = [image.height, image.width]

// Faces need bounding box rotation (done server-side)
// Thumbnail cache needs busting

API: POST /images/{image_id}/rotate { direction }
DIRTY: images

// Notify for thumbnail refresh
EMIT: imageRotated(image_id)
```

---

## Filter/View Operations

### 12. Apply People Filter
```
INPUT: person_ids[]

// This affects which images are shown, not the data itself
filter.people = person_ids
DIRTY: filter  // or view?

// Gallery will re-filter images based on this
// May need to fetch filtered image IDs from API if not cached
IF person_ids not empty:
    filtered_image_ids = API: GET /images?people={ids}
    filter.filteredImageIds = filtered_image_ids
```

### 13. Apply Semantic Search
```
INPUT: query_string

IF query_string is empty:
    → Clear filter

results = API: POST /search { query, threshold, limit }
filter.type = 'semantic'
filter.query = query_string
filter.results = results  // includes similarity scores
DIRTY: filter
```

---

## Duplicate Operations

### 14. Delete Others in Group (Keep Selected)
```
INPUT: group_hash, keep_image_ids[]

group = duplicates.getGroup(group_hash)
delete_ids = group.image_ids.filter(id => !keep_image_ids.includes(id))

FOR each image_id in delete_ids:
    → CALL Delete Image (image_id)  // internal

// Group may dissolve if only 1 image remains
IF group.image_ids.length <= 1:
    duplicates.removeGroup(group_hash)
DIRTY: duplicates
```

---

## Folder/Database Operations

### 15. Add Folder
```
INPUT: folder_path

IF folder_path is empty:
    → ERROR

IF folders.exists(folder_path):
    → ERROR: folder already registered

API: POST /folders { path: folder_path }
folders.add(folder_path)
DIRTY: folders

// Trigger scan (async, status updates via polling or events)
status.setScanning(true)
```

### 16. Remove Folder
```
INPUT: folder_path

IF NOT folders.exists(folder_path):
    → ERROR

// Images from this folder will be removed
affected_image_ids = images.getByFolder(folder_path)

FOR each image_id in affected_image_ids:
    → CALL Delete Image (image_id)  // internal

folders.remove(folder_path)
API: DELETE /folders/{encoded_path}
DIRTY: folders
```

### 17. Rescan All Folders
```
API: POST /rescan
status.setScanning(true)
DIRTY: status

// New images will appear via delta updates
// Status polling will track progress
```

---

## Common Subroutines

These are shared internal helpers that multiple operations use.
None of these are exposed externally - they are internal side-effects.

### createPerson(name) → person [INTERNAL ONLY]
```
// Called by: findOrCreatePerson
// NOT exposed externally - people are created as side-effect of face identification

normalized_name = trim(name)

person = {
    id: <from API response>,
    name: normalized_name,
    face_count: 0,
    preferred_face_id: null
}

API: POST /people { name: normalized_name }
people.add(person)
DIRTY: people

RETURN person
```

### deletePerson(person_id) [INTERNAL ONLY]
```
// Called by: decrementPersonFaceCount (when face_count reaches 0)
// NOT exposed externally - people are deleted as side-effect when their last face is removed
//
// Precondition: person has no faces (face_count == 0)
// If called with faces remaining, those faces become orphaned (person_id points to nothing)

person = people.get(person_id)
IF person is null:
    RETURN  // already deleted or never existed

// Sanity check - warn if faces still reference this person
remaining_faces = faces.findForPerson(person_id)
IF remaining_faces.length > 0:
    WARN: "Deleting person with remaining faces - they will be orphaned"

people.remove(person_id)
API: DELETE /people/{person_id}
DIRTY: people
```

### ensurePersonHasPreferred(person_id)
```
person = people.get(person_id)
IF person.preferred_face_id is null OR faces.get(person.preferred_face_id) is null:
    first_face = faces.findFirstForPerson(person_id)
    IF first_face:
        person.preferred_face_id = first_face.id
        API: POST /people/{person_id}/set-preferred { first_face.id }
        DIRTY: people
```

### unlinkFaceFromPerson(face_id)
```
face = faces.get(face_id)
IF face.person_id is null:
    RETURN

old_person_id = face.person_id
face.person_id = null
face.person_name = null
DIRTY: faces

→ CALL decrementPersonFaceCount(old_person_id, face_id)
```

### decrementPersonFaceCount(person_id, removed_face_id)
```
person = people.get(person_id)
person.face_count--

IF person.face_count == 0:
    → CALL deletePerson(person_id)
ELSE IF person.preferred_face_id == removed_face_id:
    → CALL ensurePersonHasPreferred(person_id)

// Note: DIRTY: people is handled by deletePerson or ensurePersonHasPreferred
```

### linkFaceToPerson(face_id, person_id)
```
face = faces.get(face_id)
person = people.get(person_id)

face.person_id = person_id
face.person_name = person.name
person.face_count++
API: POST /faces/{face_id}/identify { person_id }
DIRTY: faces, people

IF person.preferred_face_id is null:
    → CALL Set Preferred Face (person_id, face_id)
```

### findOrCreatePerson(name) → person
```
// Called by: Identify Face
// Returns existing person or creates new one

normalized_name = trim(name)
person = people.findByName(normalized_name)  // case-insensitive

IF person is null:
    person = → CALL createPerson(normalized_name)

RETURN person
```

---

## Domain Dependencies Matrix

| Operation | faces | people | images | duplicates | folders | filter | status |
|-----------|-------|--------|--------|------------|---------|--------|--------|
| Identify Face | W | W | | | | | |
| Unidentify Face | W | W | | | | | |
| Suppress Face | W | W | | | | | |
| Rename Person | W | W | | | | | |
| Merge People | W | W | | | | | |
| Set Preferred | | W | | | | | |
| Delete Image | W | W | W | W | | | |
| Rate Image | | | W | | | | |
| Update Description | | | W | | | | |
| Rotate Image | | | W | | | | |
| People Filter | | R | | | | W | |
| Semantic Search | | | | | | W | |
| Keep Selected (dups) | W | W | W | W | | | |
| Add Folder | | | | | W | | W |
| Remove Folder | W | W | W | W | W | | |
| Rescan | | | | | | | W |

W = Write (mutates), R = Read only

---

## Observations

### High-Coupling Operations
- **Delete Image** touches 4 domains (faces, people, images, duplicates)
- **Remove Folder** touches 5 domains
- **Identify Face** and **Unidentify Face** always touch both faces and people

### Subroutine Reuse
- `createPerson` used by: findOrCreatePerson [INTERNAL ONLY - no external API]
- `deletePerson` used by: decrementPersonFaceCount [INTERNAL ONLY - no external API]
- `findOrCreatePerson` used by: Identify Face, Rename (when merge target exists)
- `decrementPersonFaceCount` used by: Unidentify, Suppress, Delete Image
- `ensurePersonHasPreferred` used by: decrementPersonFaceCount, linkFaceToPerson
- `unlinkFaceFromPerson` used by: Unidentify, Suppress
- `linkFaceToPerson` used by: Identify Face, Merge People

### Internal-Only Operations
These operations have no external API - they only happen as side-effects:
- **Create Person**: Only via face identification (findOrCreatePerson)
- **Delete Person**: Only when last face is removed (decrementPersonFaceCount)

### Transaction Boundaries
Each numbered operation above (1-17) represents a single transaction:
- GUI calls external API once
- Internal logic runs (may call subroutines, other internal methods)
- All dirty domains notified once at end

### Optimistic UI Candidates
Operations where we can update UI before API confirms:
- Rate Image (simple, low risk)
- Update Description (simple, low risk)
- Identify Face (complex, but we do it already)
- Suppress Face (simple removal)
- Delete Image (risky - better to wait for confirm)

---

## Implementation Plan

### Phase 1: Transaction Infrastructure
**Goal:** Add core transaction system without breaking existing functionality

1. Add to `appstate.js`:
   - `_epoch` counter
   - `_inTransaction` flag
   - `_dirtyDomains` Set
   - `markDirty(domain)` helper
   - `flushDirty()` helper
   - `transaction(fn)` wrapper (handles sync + async)
   - `queueTransaction(fn)` for sequential execution

2. Add `_notify` method to each domain's subscriber system

3. **No changes to existing domain APIs yet** - just infrastructure

---

### Phase 2: Refactor `people` Domain (Proof of Concept)
**Goal:** Validate the pattern on simpler domain

1. Split into internal/external:
   ```
   people._internal.create(name)
   people._internal.delete(id)
   people._internal.update(id, changes)
   people._internal.findByName(name)

   people.rename(id, name)        // external, wrapped
   people.setPreferredFace(...)   // external, wrapped
   people.getAll()                // sync read, no wrapper
   people.search(query)           // sync read, no wrapper
   ```

2. Move logic from GUI into internal methods

3. Test: rename person, verify single notification

---

### Phase 3: Refactor `faces` Domain
**Goal:** Cross-domain transactions working

1. Split into internal/external:
   ```
   faces._internal.linkToPerson(faceId, personId)
   faces._internal.unlinkFromPerson(faceId)
   faces._internal.remove(faceId)

   faces.identify(faceId, name)       // external - calls people._internal
   faces.identifyMultiple(ids, name)  // external - batched
   faces.unidentify(faceId)           // external
   faces.suppress(faceId)             // external
   ```

2. `identify()` now contains full app logic:
   - Calls `people._internal.findOrCreate()`
   - Handles old person cleanup via `people._internal`
   - Both domains marked dirty, ONE notification batch

3. Update `faces.js` GUI to just call `AppState.faces.identify()`

---

### Phase 4: Refactor `images` Domain
**Goal:** Handle high-coupling operations

1. Key operations:
   ```
   images.delete(id)           // touches faces, people, duplicates, images
   images.deleteMultiple(ids)  // GUI batches intent
   images.rate(id, rating)
   images.updateDescription(id, text)
   images.rotate(id, direction)
   ```

2. `delete()` contains full cascade logic:
   - Iterates faces on image
   - Calls `faces._internal` and `people._internal`
   - Calls `duplicates._internal.removeImage()`
   - All dirty domains notified once

---

### Phase 5: Refactor Remaining Domains
- `duplicates` - simpler, mostly reads
- `folders` - add/remove with cascade
- `filter` / `view` - may stay simple

---

### Phase 6: GUI Cleanup
**Goal:** GUI becomes thin render + subscribe layer

1. Remove app logic from:
   - `faces.js` - no more inline person creation, preferred face logic
   - `gallery.js` - no more inline delete cascades
   - `duplicates.js` - just calls AppState

2. Pattern for each screen:
   ```javascript
   onEnter() {
       this._unsub = AppState.faces.onChanged(() => this._render());
       this._render();
   }

   _onUserAction(faceId, name) {
       AppState.faces.identify(faceId, name);  // Fire and forget
       // Optimistic UI update if desired
   }

   _render() {
       const faces = AppState.faces.getAll();  // Sync read
       // Render...
   }
   ```

---

### File Changes Summary

| File | Changes |
|------|---------|
| `appstate.js` | Major refactor - transaction system, internal/external split |
| `faces.js` | Remove app logic, simplify to render + call AppState |
| `gallery.js` | Remove delete cascade logic, simplify |
| `duplicates.js` | Minor - already mostly using AppState |
| `core.js` | Minor - may simplify event handling |

---

### Testing Strategy

After each phase:
1. Manual test the refactored operations
2. Verify single notification per transaction (console.log in flush)
3. Verify cross-domain operations notify all affected domains
4. Verify optimistic UI still works

---

### Estimated Scope

- Phase 1: ~100 lines new code
- Phase 2: ~150 lines refactor
- Phase 3: ~300 lines refactor (most complex)
- Phase 4: ~200 lines refactor
- Phase 5: ~100 lines refactor
- Phase 6: Net negative lines (removing GUI logic)
