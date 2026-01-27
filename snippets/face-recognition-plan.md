# Face Recognition Integration Plan

This document describes how face recognition could be integrated into Imaginary, allowing users to tag people in photos and search for specific individuals.

## Overview

**Goal**: Enable users to tag faces with names, have Imaginary learn to recognise those people, and search for photos containing specific individuals.

**Technology**:
- **facenet-pytorch** (MIT licensed, PyTorch-native)
- MTCNN for face detection and alignment
- InceptionResnetV1 for 512D face embeddings
- Cosine similarity for matching (same approach as OpenCLIP image search)

---

## Database Schema

### New Tables

```sql
-- Known people (identities)
CREATE TABLE people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    preferred_face_id TEXT,  -- Which face to use as the representative headshot
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Detected faces in images
CREATE TABLE faces (
    id TEXT PRIMARY KEY,
    image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,

    -- Bounding box (normalised 0-1 coordinates, always square)
    box_x REAL NOT NULL,
    box_y REAL NOT NULL,
    box_w REAL NOT NULL,
    box_h REAL NOT NULL,

    -- Detection confidence
    confidence REAL,

    -- 512D embedding (stored as blob)
    embedding BLOB NOT NULL,

    -- Link to identified person (NULL if untagged)
    person_id TEXT REFERENCES people(id) ON DELETE SET NULL,

    -- User marked this as not-a-face (prevents re-detection on reindex)
    suppressed BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for finding faces by image
CREATE INDEX idx_faces_image ON faces(image_id);

-- Index for finding faces by person
CREATE INDEX idx_faces_person ON faces(person_id);
```

### Notes

- **No `confirmed` flag** - any face with a person_id is considered identified
- **`suppressed` flag** - for false positive detections; keeps the bounding box recorded so it's not re-detected on reindex, but excluded from all face-related queries and UI
- **`preferred_face_id`** - each person has one preferred headshot for display in pickers; if person has only one face, that's the preferred by default
- **Auto-delete people** - when a person's last face is unlinked/deleted, delete the person record too (KISS principle)

### Embedding Storage

Face embeddings are 512 floats = 2KB per face. For a collection with 100,000 faces, that's ~200MB of embedding data.

Storage: SQLite BLOB (packed floats), same as current approach.

### Face Thumbnail Storage

Face thumbnails are generated from **full-size images** (not image thumbnails) during indexing:
- Size: 200x200 only
- Naming: follows existing thumbnail naming conventions to minimise code changes
- Reuses existing thumbnail infrastructure: sharpening, caching, etc.

---

## Processing Pipeline

### During Image Indexing

Add face detection/embedding as a new phase after the current processing:

```
Existing pipeline:
  1. Discover files
  2. Generate thumbnails
  3. Extract metadata
  4. Compute perceptual hash
  5. Generate OpenCLIP embedding

New addition:
  6. Detect faces (MTCNN)
  7. Generate face embeddings (InceptionResnetV1)
  8. Generate face thumbnails (200x200 from full image)
  9. Attempt auto-recognition against known faces
  10. Store faces in database
```

### Face Detection Settings

Configurable in `.imaginary.yml`:

```yaml
# Face recognition settings
face_detection_enabled: true
face_detection_min_confidence: 0.95    # MTCNN confidence threshold
face_detection_min_size: 40            # Minimum face size in pixels
face_recognition_threshold: 0.65       # Cosine similarity for auto-match
```

### Auto-Recognition Logic

When a new face is detected:

1. Compare embedding against ALL known face embeddings (no averaging - comparing against averaged embeddings of the same person at different ages would produce poor results)
2. Find the best match above `face_recognition_threshold`
3. If found, auto-assign that face's person_id to the new face
4. Otherwise, leave as untagged face

This runs silently - users can correct any mistakes in tagging mode.

---

## API Endpoints

### People Management

```
GET    /api/people                    List all people with face counts
POST   /api/people                    Create person {name}
GET    /api/people/:id                Get person details
PATCH  /api/people/:id                Update person {name}
DELETE /api/people/:id                Delete person (faces become untagged)
GET    /api/people/:id/faces          Get all faces for a person
GET    /api/people/:id/thumbnail      Get preferred face thumbnail
```

### Face Management

```
GET    /api/images/:id/faces          Get faces detected in an image (excludes suppressed)
POST   /api/faces/:id/identify        Assign face to person {person_id or name}
POST   /api/faces/:id/suppress        Mark as false positive (not a face)
DELETE /api/faces/:id                 Remove face detection entirely
GET    /api/faces/:id/thumbnail       Get cropped face thumbnail
```

### Search

```
GET    /api/search/people?q=name      Search people by name (case-insensitive)
GET    /api/images?people=id1,id2     Filter images containing ALL specified people
```

---

## GUI/UX Design

### Tagging Mode Toggle

In the Gallery toolbar, a new button toggles between normal (viewing) mode and face tagging mode.

When face detection is disabled in config, this button is visible but unclickable (greyed out), not hidden.

### Fullscreen Tagging Mode

When in tagging mode, images viewed full-screen show face overlays:

**Bounding boxes:**
- Green border = known face (has person_id)
- Red border = unknown face (no person_id)
- Orange border = currently focused (input field has focus)

**Name labels:**
- Positioned below each face (or above if near bottom of screen)
- Known faces: read-only label showing name; click to edit
- Unknown faces: empty input field ready for typing

**Delete button:**
- Small X button in corner of each bounding box
- Tooltip: "Remove face detection (not a real face)"
- Clicking deletes the face candidate (marks as suppressed in DB)
- Works for both known and unknown faces

**Input field behaviour:**
- Typing triggers needle-in-haystack search of existing names
- Matching names appear in popup menu (see below)
- Click menu item to select that name (stays editable)
- Commit on blur:
  - If name matches existing person (case-insensitive), link to that person
  - If new name, create new person record
  - If empty (after trimming), face remains unknown
- Arrow keys move text cursor (normal editing behaviour)
- Arrow keys only navigate images when no input field has focus

**Keyboard navigation:**
- On first displaying image, no labels have focus
- Tab key focuses first unknown face's input field
- Tab cycles through unknown faces (round-robin)
- Tab while editing commits current field (blur fires) then focuses next unknown
- Left/Right arrows navigate images only when no input field has focus

### Needle-in-Haystack Search Component

Reusable autocomplete component shared across all face-related UI:

- Shows deduplicated list of people names, sorted alphabetically, case-insensitive
- Filters as user types (substring match)
- Popup menu positioned below or above input field (whichever fits)
- Must not cause page scrollbars or overflow viewport
- Max height with scrolling; if truncated, last visible entry shows "..." to indicate user should type more characters
- Click item or press Enter to select

### Filter Screen: People Field

New "People" field in the Filter screen:
- Default placeholder text: "Click to add people"
- When face detection disabled: field is visible but unclickable (greyed out)
- Click opens People Picker dialog

**Filter logic:** Images must contain ALL selected people (AND logic)

### People Picker Dialog

Modal dialog with two panels:

**Left panel - Available people:**
- Grid of all known people (headshot thumbnails + name)
- Sorted alphabetically by name (case-insensitive)
- Search box at top filters the grid (needle-in-haystack)
- Double-click or drag to add to selection

**Right panel - Selected people:**
- Palette showing currently selected people
- Double-click or drag to remove from selection
- Initially empty (or populated from current filter)

**Footer:**
- "Done" button closes dialog and returns to Filter screen
- People field shows selected names alphabetically, truncated with ellipsis if too long

### Faces Screen

New screen accessible from toolbar button (middle section, alongside Database/Duplicates/Filter). When face detection disabled, button is visible but unclickable.

**Layout:**
- Grid view of face thumbnails (like Gallery)
- Known faces first, sorted alphabetically by person name
- Unknown faces after, sorted by parent image timestamp

**Labels:**
- Known faces: person name + star icon
- Unknown faces: "&lt;Unknown&gt;"
- Click any label to edit (same needle-in-haystack behaviour)

**Star icon (preferred headshot):**
- Gold star = preferred headshot for this person
- Grey star = alias (same person, different appearance)
- If person has only one face, star is gold by default
- Click grey star to make it the preferred headshot

**Toolbar controls:**
- Thumbnail size buttons (smaller/larger)
- "Only unknowns" toggle
- Sort direction button (for unknowns section - ascending/descending by image timestamp)

**Re-sorting:**
- When a name changes, the grid re-sorts immediately
- Known faces maintain alphabetical order
- Unknowns remain sorted by image timestamp

### Gallery: Sort by People

New sort option in Gallery toolbar:

**Logic:**
1. For each image, collect names of all known people in that image
2. Sort those names alphabetically (case-insensitive) and join to create the image's "names string"
3. Sort all images by their names strings (case-insensitive)

Images with no known faces sort together (empty string).

### Duplicates: Sort by People

Same logic as Gallery sort by people.

---

## Performance Considerations

### Face Detection Speed

- MTCNN on CPU: ~100-200ms per image
- MTCNN on GPU: ~20-50ms per image

Recommendation: Run face detection in the existing background processing thread, same as OpenCLIP embeddings.

### Embedding Comparison

Comparing against all known faces (not averaged per person):
- 1,000 known faces = ~1ms
- 10,000 known faces = ~10ms
- Still fast for typical collections

### Memory Usage

- MTCNN model: ~100MB
- InceptionResnetV1: ~100MB
- Total additional memory: ~200MB for models (lazy loaded on first use)

### Startup Time

Models load once on first face operation (lazy loading), similar to OpenCLIP.

---

## Configuration Options

```yaml
# .imaginary.yml additions

# Face recognition
face_detection_enabled: true           # Set false to disable (UI buttons greyed out)
face_detection_min_confidence: 0.95    # MTCNN confidence threshold
face_detection_min_size: 40            # Minimum face size in pixels
face_recognition_threshold: 0.65       # Cosine similarity for auto-match
```

---

## Migration / Existing Images

For existing image collections:

```bash
python app.py --detect-faces
```

- Processes all images that haven't had face detection run
- Runs in background (like `--generate-thumbnails`)
- Progress shown in Database screen
- Respects `face_detection_enabled` config

---

## Execution Checklist

Execution guidelines:

- reuse and adapt existing code where reasonable
- put bulk of new face-related code into new `faces.py` and `faces.js` modules
- think about time complexity of algorithms O(n) much better than O(n^2) for 50,000+ images/faces!
- use threads during indexing, and plumb correctly into graceful shutdown code
- remember that all colours in the CSS should be indirected through CSS variables
- avoid thrashing the database and think about thread safety throughout
- good commenting with JDoc and PEP formats
- at the end of implementing each phase, commit the changes with a plain English commit message

### Phase 1: Foundation
- [ ] Add facenet-pytorch dependency
- [ ] Create database schema (people, faces tables) and one-time migration
- [ ] Implement FaceDetector class (MTCNN + InceptionResnetV1)
- [ ] Add face detection to image processing pipeline
- [ ] Generate and store face thumbnails (200x200) - adapt/reuse `thumbnails.py` as much as reasonably possible
- [ ] Store detected faces in database
- [ ] Add configuration options to `config.py`
- [ ] Implement auto-recognition logic

### Phase 2: Core API
- [ ] Implement people CRUD endpoints
- [ ] Implement face management endpoints (identify, suppress)
- [ ] Implement face thumbnail endpoint
- [ ] Add `?people=` filter to image listing endpoint
- [ ] Implement people search endpoint

### Phase 3: Tagging Mode UI
- [ ] Add tagging mode toggle button to Gallery toolbar
- [ ] Implement fullscreen face overlay rendering (bounding boxes, labels)
- [ ] Implement name input fields with commit-on-blur (careful handling of special chars in names!)
- [ ] Implement needle-in-haystack autocomplete component
- [ ] Implement delete (X) button for false positives
- [ ] Handle keyboard navigation (Tab, arrow keys)
- [ ] Bounding box colour states (green/red/orange - keep to existing pastel colours scheme)

### Phase 4: Faces Screen
- [ ] Create Faces screen with virtual grid layout (see `thumbnails.js`)
- [ ] Add Faces (screen) toolbar button
- [ ] Implement known/unknown sections with proper sorting
- [ ] Implement star icon for preferred headshot
- [ ] Add toolbar controls (thumbnail size, only unknowns toggle, sort direction)
- [ ] Click-to-edit labels with needle-in-haystack

### Phase 5: Filter Integration
- [ ] Add People field to Filter screen
- [ ] Implement People Picker dialog (two-panel layout)
- [ ] Implement AND filter logic for multiple people
- [ ] Add "Sort by people" to Gallery toolbar
- [ ] Add "Sort by people" to Duplicates toolbar

### Phase 6: Polish
- [ ] CLI flag `--detect-faces` for existing images
- [ ] Progress indication for face detection in Database screen
- [ ] Handle disabled state (greyed out buttons/fields)
- [ ] Auto-delete people with zero faces
- [ ] Performance optimisation (batching, lazy loading)
- [ ] Documentation update (README)
