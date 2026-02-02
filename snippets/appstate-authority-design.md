# AppState as Single Source of Truth - Design Document

## Overview

This document outlines a refactored architecture where:
1. **AppState generates all IDs** (using `crypto.randomUUID()`)
2. **Backend is a dumb persistence layer** - validates and stores, doesn't compute
3. **Application logic lives only in AppState** - no duplication
4. **API is simplified** - backend only returns success/error, not "here's what I created"

## User Actions Inventory

### Faces Screen - People List (Known Section)
| Action | Trigger | Description |
|--------|---------|-------------|
| Select person | Click card | Toggle selection (enables focus button) |
| Enter picker | Double-click card | Open preferred picker for this person |
| Enter picker | Focus button | Open preferred picker for selected person |
| Filter gallery | Click filter badge | Navigate to gallery filtered to this person |
| Identify as person | Drop faces onto card | Assign dragged faces to this person |

### Faces Screen - Unknown Faces List
| Action | Trigger | Description |
|--------|---------|-------------|
| Identify face(s) | Type name, blur/Enter | Assign selected face(s) to existing or new person |
| Suppress face(s) | X button | Mark as false positive, hide from list |
| Start drag | Drag face(s) | Begin drag to drop onto person card (identify dropped faces) |
| Select | Click, Ctrl+click, Shift+click | Standard selection |
| Open full-screen | Double-click | Open related image in fullscreen viewer |
| Bulk select | Drag-box | Rectangular selection |

### Faces Screen - Preferred Picker
| Action | Trigger | Description |
|--------|---------|-------------|
| Set preferred | Click star icon | Set as person's representative thumbnail |
| Unassign face(s) | Delete key | Return selected faces to unknown pool |
| Lock face | Click padlock | Mark as manually tagged (not auto-removable) |
| Unlock face | Click padlock | Unmark (allow auto-removal) |
| Adjust threshold | Header slider | Change auto-match sensitivity for this person |
| Rename person | Click rename button, type | Change person's display name |
| Merge person | Rename to existing name | Move all faces to that person, delete this one |
| Dissolve person | Rename to empty | Return (unidentify) all faces to unknown, delete person |
| Reassign face | Type name on face card | Move that face to different person (or unidentify with empty string) |
| Open fullscreen | Double-click face / Enter | View related image in fullscreen viewer |
| Select | Click, Ctrl+click, Shift+click | Standard selection |
| Exit picker | Escape / Focus toolbar button | Return to normal view |

### Fullscreen Viewer - Tagging Mode
| Action | Trigger | Description |
|--------|---------|-------------|
| Identify face | Click bbox, type name, blur/Enter | Assign face to person |
| Unidentify face (1) | Click bbox, clear name, blur/Enter | Return face to unknown |
| Unidentify face (2) | Click X button on identified bbox | Return face to unknown |
| Suppress face | Click X button on unidentified bbox | Mark as false positive |

### Future / Potential Actions
| Action | Description |
|--------|-------------|
| Un-suppress face | Restore a suppressed face (requires admin UI) |
| Bulk import names | CSV import of face assignments |
| Drag person to merge | Drag person A onto person B to merge (shortcut) |

---

## Current Architecture Problems

### 1. Dual ID Generation
```
Frontend: tempId = "temp-" + Date.now()
Backend:  realId = uuid4()
Frontend: reconcile(tempId → realId)  // Complex, error-prone
```

### 2. Duplicated Application Logic
```python
# Backend (app.py) - identify endpoint
person = find_or_create_person(name)  # Logic here
update_face_counts()                   # And here
set_preferred_if_needed()              # And here
delete_empty_persons()                 # And here
```

```javascript
// Frontend (appstate.js) - identify method
person = findOrCreate(name)           // Same logic duplicated
updateFaceCounts()                     // Same
setPreferredIfNeeded()                 // Same
deleteEmptyPersons()                   // Same
```

### 3. Complex API Responses
```javascript
// Current: Backend returns computed state
POST /faces/identify-batch
Response: {
    person: { id, name, face_count, preferred_face_id },
    identified_count: 3,
    face_ids: [...],
    reassessment_triggered: true
}
// Frontend must interpret and reconcile
```

### 4. Race Conditions
- Backend reassessment can modify faces while frontend has pending changes
- Temp ID reconciliation can conflict with other operations
- Multiple concurrent identify operations can clash

---

## Proposed Architecture

### Principle 1: Frontend Generates All IDs

```javascript
// AppState generates IDs
const personId = crypto.randomUUID();  // "a1b2c3d4-..."

// API includes the ID
await App.apiPost('/people', {
    id: personId,      // Frontend-generated
    name: 'Bob'
});

// No reconciliation needed - ID was correct from start
```

### Principle 2: Backend is Dumb Persistence

Backend endpoints become simple CRUD:

```python
@app.route('/api/people', methods=['POST'])
def create_person():
    data = request.json
    # Validate ID format (UUID)
    # Validate name not empty
    # Check for ID collision (error if exists)
    # INSERT INTO people (id, name) VALUES (?, ?)
    return {'success': True}

@app.route('/api/faces/assign', methods=['POST'])
def assign_faces():
    data = request.json
    # Validate face_ids exist
    # Validate person_id exists
    # UPDATE faces SET person_id = ? WHERE id IN (?)
    return {'success': True}
```

No application logic in backend:
- No "find or create person"
- No "update face counts" (counts are derived, computed by SELECT)
- No "set preferred if needed"
- No "delete empty persons"

Exceptions:

**Backend similarity search** (the one place backend does real work):

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. User identifies face(s) manually                                     │
│    AppState.faces.identify() → locks faces → POST /faces/assign         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 2. Backend stores assignment, then searches unknown faces pool          │
│    - Compares embedding of identified face(s) against unknown faces     │
│    - Uses person's threshold (or default) for similarity cutoff         │
│    - Assigns matches directly in DB (not locked!)                       │
│    - Marks reassessment as pending (for polling)                        │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 3. AppState polls for completion, receives auto-matched face IDs        │
│    AppState.faces.autoAssign() → updates cache (no lock, no persist)    │
│    - No lock = can be auto-removed later if threshold changes           │
│    - No persist = backend already stored it                             │
│    - No cascade = doesn't trigger another similarity search             │
└─────────────────────────────────────────────────────────────────────────┘
```

**Threshold adjustment** (re-evaluation of unlocked faces):

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. User adjusts threshold slider                                        │
│    AppState.people.setThreshold() → PATCH /people/:id { threshold }     │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 2. Backend re-evaluates UNLOCKED faces for this person                  │
│    - Faces above new threshold → unassign (return to unknown)           │
│    - Unknown faces below threshold → assign (unlocked)                  │
│    - LOCKED faces are NEVER touched                                     │
│    - Returns { assigned: [...], unassigned: [...] }                     │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 3. AppState polls for completion, receives changes                      │
│    AppState.faces.applyThresholdChanges() → updates cache               │
│    - Assigns new faces (unlocked)                                       │
│    - Unassigns faces that no longer match                               │
│    - No persist = backend already stored it                             │
└─────────────────────────────────────────────────────────────────────────┘
```

These are the areas where:
- Backend does actual logic (similarity search with ML embeddings)
- AppState is **reactive** not optimistic (responds to backend, doesn't predict)
- Locked vs unlocked distinction matters:
  - Only locked faces are used for similarity comparison
  - Only unlocked faces can be auto-removed by threshold changes

### Principle 3: Application Logic Only in AppState

All business logic lives in AppState methods, not duplicated in backend:

- **Find or create person** → `people._internal.findByName()` / `add()`
- **Track affected persons** → `assignToPersonBatch()` returns old person IDs
- **Link/unlink faces** → `assignToPersonBatch()` / `unassignBatch()`
- **Lock/unlock** → controlled by `{ lock }` parameter
- **Set preferred** → `setPreferred()` (also locks)
- **Cleanup empty persons** → `reconcilePerson()` / `reconcileAll()`

See **Detailed Action Flows** below for reference implementations.

### Principle 4: Simplified API Contract

**Request format**: "Here's the exact state, persist it"
**Response format**: Success or error (with rollback info if needed)

```javascript
// Identify faces
POST /faces/assign
{ face_ids: [...], person_id: "uuid" }
→ { success: true }
→ { success: false, error: "Face abc not found" }

// Create person
POST /people
{ id: "uuid", name: "Bob" }
→ { success: true }
→ { success: false, error: "ID collision" }

// Update person
PATCH /people/:id
{ name: "Robert", preferred_face_id: "uuid" }
→ { success: true }

// Delete person
DELETE /people/:id
→ { success: true }

// Suppress faces
POST /faces/suppress
{ face_ids: [...] }
→ { success: true }

// Update face properties
PATCH /faces
{ face_ids: [...], locked: true }
→ { success: true }
```

---

## Detailed Action Flows

### Design Principles

1. **Composition over duplication**: Higher-level operations call lower-level ones
2. **Invariants enforced at lowest level**:
   - Setting preferred face → also locks that face
   - Manual identification → locks the face
   - First faces for person → set newest (by image timestamp) as preferred
3. **Single responsibility**: Each primitive does one thing

### Layered Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  UI Layer                                                       │
│  - Collects selection (faceIds array)                          │
│  - Calls AppState.faces.identify(faceIds, name)                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Public Methods (batch-aware)                                   │
│  - faces.identify(faceIds[], name)                             │
│  - faces.unassign(faceIds[])                                   │
│  - faces.suppress(faceIds[])                                   │
│  - faces.setLocked(faceIds[], locked)                          │
│                                                                 │
│  These methods:                                                 │
│  1. Open transaction                                            │
│  2. Loop over items, call cache primitives                      │
│  3. Close transaction (broadcasts once)                         │
│  4. Make ONE batched API call                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────────┐  ┌─────────────────────────────────┐
│  Cache Primitives           │  │  Persist Functions (batch)      │
│  (single-item, sync)        │  │  (array-based, async)           │
│                             │  │                                 │
│  _internal.linkToPerson()   │  │  _persistIdentify(faceIds[])    │
│  _internal.setLocked()      │  │  _persistUnassign(faceIds[])    │
│  _internal.unlinkFromPerson │  │  _persistSuppress(faceIds[])    │
│                             │  │                                 │
│  Fast in-memory ops.        │  │  → POST /faces/assign           │
│  Called in loops.           │  │    { face_ids: [...] }          │
│  No persistence.            │  │                                 │
└─────────────────────────────┘  └─────────────────────────────────┘
```

**Key insight**: Cache primitives are single-item because:
- They're synchronous, in-memory operations (fast even in loops)
- Looping 50 times over `setLocked()` is ~microseconds
- The transaction batches all cache changes into one broadcast

Batching matters for:
- **API calls** (network latency, server load) → persist functions take arrays
- **UI updates** (avoid flicker) → transaction batches broadcasts

### Cache Primitives (`_internal`)

Single-item operations for cache mutation. Never persist directly.

```javascript
// faces._internal primitives
linkToPerson(faceId, personId, personName)  // Just sets person_id, person_name
unlinkFromPerson(faceId)                     // Just clears person_id, person_name
setLocked(faceId, locked)                    // Just sets manually_tagged
setSuppressed(faceId, suppressed)            // Just sets suppressed flag

// people._internal primitives
add(person)                                  // Just adds to cache
remove(personId)                             // Just removes from cache
setName(personId, name)                      // Just updates name (+ denormalized on faces)
setPreferred(personId, faceId)               // Sets preferred AND locks that face
setThreshold(personId, threshold)            // Just updates threshold
bustThumbnail(personId)                      // Invalidates cached thumbnail
```

### Batch Helpers (`_internal`)

Compose primitives for common operations. Used by public methods.

```javascript
// faces._internal.assignToPersonBatch(faceIds, personId, { lock })
// Links faces to person, optionally locks. Returns affected old person IDs.
assignToPersonBatch(faceIds, personId, { lock = false } = {}) {
    const person = people._internal.get(personId);
    if (!person) return new Set();

    const affectedPersonIds = new Set();

    for (const faceId of faceIds) {
        const face = this.get(faceId);
        if (!face || face.suppressed) continue;

        // Track old person (for reconciliation)
        if (face.person_id && face.person_id !== personId) {
            affectedPersonIds.add(face.person_id);
        }

        this.linkToPerson(faceId, personId, person.name);
        if (lock) {
            this.setLocked(faceId, true);
        }
    }

    return affectedPersonIds;
}

// faces._internal.unassignBatch(faceIds)
// Unlinks faces from their persons, unlocks. Returns affected person IDs.
unassignBatch(faceIds) {
    const affectedPersonIds = new Set();

    for (const faceId of faceIds) {
        const face = this.get(faceId);
        if (!face?.person_id) continue;

        affectedPersonIds.add(face.person_id);
        this.unlinkFromPerson(faceId);
        this.setLocked(faceId, false);
    }

    return affectedPersonIds;
}

// people._internal.reconcileAll(personIds)
// Reconcile multiple persons (recalc counts, auto-delete, reassign preferred)
reconcileAll(personIds) {
    for (const personId of personIds) {
        this.reconcilePerson(personId);
    }
}

// faces._internal.pickNewestFace(faceIds)
// Pick face with newest image_timestamp (for setting preferred on new person)
pickNewestFace(faceIds) {
    let newest = null;
    let newestTime = null;
    for (const faceId of faceIds) {
        const face = this.get(faceId);
        const ts = face?.image_timestamp || 0;
        if (newestTime === null || ts > newestTime) {
            newestTime = ts;
            newest = faceId;
        }
    }
    return newest || faceIds[0];
}
```

### 1. Identify Face(s) - Manual

**User action**: Type name on face card, press Enter/blur
**Triggers**: Unknown face input, picker face input, drop onto person, fullscreen bbox

Manual identification:
- **Locks** the faces (marks as manually_tagged)
- **Triggers backend search** for similar faces in unknown pool
- Backend responds with auto-matched face IDs → handled by `autoAssign()`

**AppState.faces.identify(faceIds, personName, options = {})**:
```javascript
// options: { preferredFaceId: string|null }  // Which face to prefer if creating person

identify(faceIds, personName, options = {}) {
    if (!faceIds?.length) return;

    // Empty name = unassign (delegate, don't push check to callers)
    const trimmedName = personName?.trim() || '';
    if (!trimmedName) {
        return this.unassign(faceIds);
    }

    let person, createdPerson = false;

    transaction(() => {
        // 1. Find or create person
        person = people._internal.findByName(trimmedName);
        if (!person) {
            person = {
                id: crypto.randomUUID(),
                name: trimmedName,
                face_count: 0,
                preferred_face_id: null,
                threshold: null
            };
            people._internal.add(person);
            createdPerson = true;
        }

        // 2. Assign faces (locked) + track affected persons
        const affectedPersonIds = _internal.assignToPersonBatch(faceIds, person.id, { lock: true });

        // 3. Set preferred face if person has none
        if (!person.preferred_face_id) {
            const preferredId = options.preferredFaceId || _internal.pickNewestFace(faceIds);
            people._internal.setPreferred(person.id, preferredId);
        }

        // 4. Reconcile this person + old persons
        people._internal.reconcilePerson(person.id);
        people._internal.reconcileAll(affectedPersonIds);
    });

    // Persist - backend will search for similar faces and respond with matches
    return this._persistIdentify(faceIds, person.id, createdPerson);
}
```
`
### 1b. Auto-Assign Face(s) - Backend-Detected

**Trigger**: Polling response with auto-matched face IDs after similarity search

Auto-assignment (backend-detected matches):
- **Does NOT lock** (these are auto-detected, not manually tagged)
- **Does NOT trigger another search** (would cause infinite cascade!)
- Can be auto-removed later if user adjusts threshold

This is reactive, not optimistic - AppState responds to backend's similarity search.

**AppState.faces.autoAssign(faceIds, personId)**:
```javascript
autoAssign(faceIds, personId) {
    if (!faceIds?.length) return;
    if (!people._internal.get(personId)) return;  // Person may have been deleted

    transaction(() => {
        // Assign faces (NOT locked - auto-detected)
        const affectedPersonIds = _internal.assignToPersonBatch(faceIds, personId, { lock: false });

        // Reconcile this person + any old persons
        people._internal.reconcilePerson(personId);
        people._internal.reconcileAll(affectedPersonIds);
    });

    // No persist - backend already stored these assignments
}
```

### 2. Unassign Face(s)

**User action**: Delete key in picker, clear name on bbox
**Effect**: Return faces to unknown pool (not suppressed), **unlocks** them

**AppState.faces.unassign(faceIds)**:
```javascript
unassign(faceIds) {
    if (!faceIds?.length) return;

    transaction(() => {
        const affectedPersonIds = _internal.unassignBatch(faceIds);
        people._internal.reconcileAll(affectedPersonIds);
    });

    return this._persistUnassign(faceIds);
}
```

### 3. Suppress Face(s)

**User action**: X button on unknown face, X button on unidentified bbox

**AppState.faces.suppress(faceIds)**:
```javascript
suppress(faceIds) {
    if (!faceIds?.length) return;

    transaction(() => {
        // Unassign first (if any were identified)
        const affectedPersonIds = _internal.unassignBatch(faceIds);

        // Mark suppressed
        for (const faceId of faceIds) {
            _internal.setSuppressed(faceId, true);
        }

        people._internal.reconcileAll(affectedPersonIds);
    });

    return this._persistSuppress(faceIds);
}
```

### 4. Set Preferred Face

**User action**: Click star in picker

**AppState.people.setPreferred(personId, faceId)**:
```javascript
setPreferred(personId, faceId) {
    transaction(() => {
        const person = people._internal.get(personId);
        if (!person) throw new Error('Person not found');

        // Verify face belongs to this person
        const face = faces._internal.get(faceId);
        if (face?.person_id !== personId) {
            throw new Error('Face does not belong to this person');
        }

        // INVARIANT: preferred face must be locked
        faces._internal.setLocked(faceId, true);

        person.preferred_face_id = faceId;
        people._internal.bustThumbnail(personId);
    });

    return this._persistSetPreferred(personId, faceId);
}
```

### 5. Lock/Unlock Face(s)

**User action**: Click padlock in picker (single or with selection)

**AppState.faces.setLocked(faceIds, locked)**:
```javascript
setLocked(faceIds, locked) {
    if (!faceIds?.length) return;

    // INVARIANT: cannot unlock preferred faces
    if (!locked) {
        for (const faceId of faceIds) {
            const face = faces._internal.get(faceId);
            if (face?.is_preferred) {
                throw new Error('Cannot unlock the preferred face');
            }
        }
    }

    transaction(() => {
        for (const faceId of faceIds) {
            faces._internal.setLocked(faceId, locked);
        }
    });

    return this._persistSetLocked(faceIds, locked);
}
```

### 6. Rename Person

**User action**: Click rename button in picker, type name, blur/Enter

This is the most complex operation with many edge cases:

**AppState.people.rename(personId, newName)**:
```javascript
rename(personId, newName) {
    const person = people._internal.get(personId);
    if (!person) throw new Error('Person not found');

    const oldName = person.name;
    const trimmedNew = newName?.trim() || '';

    // Case A: No-op (same name)
    if (trimmedNew === oldName) {
        return Promise.resolve();
    }

    // Case B: Error (empty old name - shouldn't happen)
    if (!oldName) {
        throw new Error('Person has no name');
    }

    // Case C: Dissolve (new name empty → unidentify all faces)
    if (!trimmedNew) {
        return this.dissolve(personId);  // Delegate to dissolve()
    }

    // Case D: Merge (new name matches existing person)
    const collision = people._internal.findByName(trimmedNew);
    if (collision && collision.id !== personId) {
        return this.merge(personId, collision.id);  // Delegate to merge()
    }

    // Case E: Simple rename (new name doesn't exist)
    transaction(() => {
        people._internal.setName(personId, trimmedNew);
        // setName also updates denormalized person_name on all faces
    });

    return this._persistRename(personId, trimmedNew);
}
```

### 7. Merge People

**User action**: Rename person to existing person's name
**Effect**: Move all faces from source to target, delete source

**AppState.people.merge(fromId, toId)**:
```javascript
merge(fromId, toId) {
    if (fromId === toId) return Promise.resolve();

    const fromPerson = people._internal.get(fromId);
    const toPerson = people._internal.get(toId);
    if (!fromPerson || !toPerson) throw new Error('Person not found');

    // Capture face IDs before mutation (needed for persist)
    const faceIds = faces.getForPerson(fromId).map(f => f.id);

    transaction(() => {
        for (const faceId of faceIds) {
            faces._internal.linkToPerson(faceId, toId, toPerson.name);
            // Note: faces keep their locked status
        }
        toPerson.face_count += faceIds.length;

        // Remove source person (now has 0 faces)
        people._internal.remove(fromId);
    });

    // SEQUENCED: must await assign before delete (see API Sequencing below)
    return this._persistMerge(faceIds, fromId, toId);
}

async _persistMerge(faceIds, fromId, toId) {
    // Order matters! Backend is dumb - DELETE would fail if faces still reference fromId
    await App.apiPost('/faces/assign', { face_ids: faceIds, person_id: toId });
    await App.apiDelete(`/people/${fromId}`);
}
```

### 8. Dissolve Person

**User action**: Rename person to empty string
**Effect**: Unidentify all faces, delete person

**AppState.people.dissolve(personId)**:
```javascript
dissolve(personId) {
    const person = people._internal.get(personId);
    if (!person) throw new Error('Person not found');

    // Capture face IDs before mutation (needed for persist)
    const faceIds = faces.getForPerson(personId).map(f => f.id);

    transaction(() => {
        for (const faceId of faceIds) {
            faces._internal.unlinkFromPerson(faceId);
            faces._internal.setLocked(faceId, false);
        }

        // Remove the now-empty person
        people._internal.remove(personId);
    });

    // SEQUENCED: must await unassign before delete
    return this._persistDissolve(faceIds, personId);
}

async _persistDissolve(faceIds, personId) {
    // Order matters! Backend is dumb - DELETE would fail if faces still reference personId
    if (faceIds.length > 0) {
        await App.apiPost('/faces/unassign', { face_ids: faceIds });
    }
    await App.apiDelete(`/people/${personId}`);
}
```

### API Call Sequencing

The backend is a dumb persistence layer with foreign key constraints. It doesn't
know about operation semantics - it just validates and stores.

**Dependent operations must be sequenced with `await`:**

| Operation | Sequence | Why |
|-----------|----------|-----|
| Merge | assign faces → delete person | FK: faces.person_id → people.id |
| Dissolve | unassign faces → delete person | FK: faces.person_id → people.id |
| Create + Identify | create person → assign faces | FK: faces.person_id must exist |

**Independent operations can be parallel:**

```javascript
// These don't depend on each other - fire in parallel
await Promise.all([
    App.apiPost('/faces/suppress', { face_ids: batch1 }),
    App.apiPost('/faces/suppress', { face_ids: batch2 }),
]);
```

**The cache update (transaction) is always instant.** Only the persist calls need sequencing.

### 9. Adjust Threshold

**User action**: Slider in picker header

The `locked` (manually_tagged) flag serves two purposes:

1. **Similarity search scope**: Backend only compares unknown faces against **locked** faces
   when searching for matches. Auto-detected faces don't cascade more searches.

2. **Threshold protection**: When threshold changes, backend re-evaluates **unlocked** faces:
   - Unlocked faces above new threshold → unassigned (returned to unknown pool)
   - Unknown faces below new threshold → assigned (unlocked)
   - **Locked faces are NEVER removed** regardless of threshold

**AppState.people.setThreshold(personId, threshold)**:
```javascript
setThreshold(personId, threshold) {
    // threshold: number 0.60-0.99, or null for default

    transaction(() => {
        const person = people._internal.get(personId);
        if (!person) throw new Error('Person not found');
        person.threshold = threshold;
    });

    // Backend will re-evaluate faces and respond with changes
    return this._persistSetThreshold(personId, threshold);
}

async _persistSetThreshold(personId, threshold) {
    // Backend re-runs similarity matching with new threshold
    const response = await App.apiPatch(`/people/${personId}`, { threshold });

    // Response includes face changes (if any)
    // { assigned: [faceId, ...], unassigned: [faceId, ...] }
    if (response.assigned?.length || response.unassigned?.length) {
        this.applyThresholdChanges(personId, response.assigned, response.unassigned);
    }
}
```

### 9b. Apply Threshold Changes (Backend Response)

**Trigger**: Backend response after threshold adjustment with reassessment results

```javascript
// Called when backend finishes re-evaluating faces after threshold change
applyThresholdChanges(personId, assignedFaceIds, unassignedFaceIds) {
    if (!people._internal.get(personId)) return;  // Person may have been deleted

    transaction(() => {
        // 1. Assign new faces (unlocked - auto-detected)
        if (assignedFaceIds?.length) {
            _internal.assignToPersonBatch(assignedFaceIds, personId, { lock: false });
        }

        // 2. Unassign faces that no longer match (with sanity check)
        if (unassignedFaceIds?.length) {
            // Filter out locked faces (backend shouldn't send these, but be defensive)
            const unlocked = unassignedFaceIds.filter(id => {
                const face = _internal.get(id);
                if (face?.manually_tagged) {
                    console.warn(`Backend tried to unassign locked face ${id}`);
                    return false;
                }
                return true;
            });
            _internal.unassignBatch(unlocked);
        }

        // 3. Reconcile person
        people._internal.reconcilePerson(personId);
    });

    // No persist - backend already made these changes
}
```

### Internal Helper: reconcilePerson

Called after faces are unlinked from a person. Recalculates count from actual faces
(avoids drift from manual counting), handles auto-delete and preferred reassignment.

```javascript
// people._internal.reconcilePerson(personId)
reconcilePerson(personId) {
    const person = this.get(personId);
    if (!person) return;

    // Recalculate face_count from actual linked faces (source of truth)
    const linkedFaces = faces.getForPerson(personId);
    person.face_count = linkedFaces.length;

    if (person.face_count === 0) {
        // Auto-delete empty person
        this.remove(personId);
        return;
    }

    // Check if preferred face was removed
    const preferredStillExists = linkedFaces.some(f => f.id === person.preferred_face_id);
    if (!preferredStillExists) {
        // Pick new preferred (newest by image timestamp)
        const newest = linkedFaces.reduce((a, b) =>
            (a.image_timestamp || 0) > (b.image_timestamp || 0) ? a : b
        );
        this.setPreferred(personId, newest.id);  // Also locks the new preferred
    }
}
```

---

## Backend Simplification

### Current Endpoints (Complex)
```
POST /faces/identify-batch     - Find/create person, assign, update counts, etc.
POST /faces/:id/identify       - Same but single face
POST /people/:id/merge         - Move faces, update counts, delete source
POST /people/:id/set-preferred - Update preferred, bust cache
```

### Proposed Endpoints (Simple CRUD)
```
# People
POST   /people              - Create with frontend-provided ID
GET    /people              - List all (face_count computed via JOIN)
GET    /people/:id          - Get one
PATCH  /people/:id          - Update name, preferred_face_id, threshold
DELETE /people/:id          - Delete (faces become unassigned)

# Faces
GET    /faces               - List all
PATCH  /faces               - Batch update (person_id, locked, suppressed)
POST   /faces/assign        - Assign faces to person (sugar for PATCH)
POST   /faces/suppress      - Suppress faces (sugar for PATCH)

# Images (unchanged)
GET    /images/:id/faces    - Get faces for image
```

### Backend Responsibilities (Minimal)
1. **Validate** - IDs are UUIDs, references exist, permissions OK
2. **Persist** - Store exactly what frontend sends
3. **Enforce constraints** - Foreign keys, uniqueness
4. **Serve data** - Return current state on GET requests

### Backend Does NOT
- Generate IDs (frontend does)
- Compute derived state (frontend does)
- Chain operations (frontend orchestrates)
- Send "here's what I created" responses

---

## Error Handling & Rollback

### Optimistic Update Pattern
```javascript
async someAction() {
    // 1. Backup current state
    const backup = this._backupState();

    // 2. Apply changes optimistically
    transaction(() => {
        // ... mutations ...
    });
    // UI is now updated

    // 3. Persist
    try {
        await this._persist();
    } catch (err) {
        // 4. Rollback on error
        transaction(() => {
            this._restoreState(backup);
        });
        // UI reverts
        throw err;
    }
}
```

### What Can Fail?
1. **Network error** - Request didn't reach server
2. **Validation error** - Server rejected (bad ID, missing reference)
3. **Conflict** - Another client modified same data
4. **Constraint violation** - Unique name, etc.

### Rollback Strategy
- AppState keeps backup before optimistic update
- On any error, restore backup and re-broadcast
- Show error toast to user
- Log for debugging

---

## Migration Path

### Phase 1: Frontend ID Generation
- Add `crypto.randomUUID()` for new person creation
- Backend accepts frontend IDs, ignores if it would generate different one
- Remove temp ID reconciliation code

### Phase 2: Simplify Identify Flow
- AppState does all logic (find/create, counts, cleanup)
- Backend endpoint becomes simple assignment
- Remove duplicated logic from backend

### Phase 3: Simplify Other Flows
- Merge, dissolve, rename follow same pattern
- Each action: AppState logic → simple persist

### Phase 4: API Cleanup
- Deprecate complex endpoints
- Document new simple CRUD API
- Remove unused response fields

---

## Benefits Summary

| Aspect | Current | Proposed |
|--------|---------|----------|
| ID ownership | Backend | Frontend |
| Application logic | Duplicated | AppState only |
| API complexity | Complex responses | Simple success/error |
| Reconciliation | Temp → Real ID dance | None needed |
| Race conditions | Possible | Reduced (frontend authoritative) |
| Offline potential | Hard | Possible (queue operations) |
| Code duplication | High | Minimal |
| Testing | Need backend for full test | Can unit test AppState |

---

## Resolved Questions

1. **Auto-matching**: Backend runs face similarity matching.

   **Resolution**: Stay in backend (ML embeddings live there). Two triggers:
   - After manual `identify()` → backend searches unknown pool → AppState polls for completion → receives matched IDs → calls `autoAssign()` (no lock, no cascade)
   - After `setThreshold()` → backend re-evaluates unlocked faces → returns `{ assigned, unassigned }` → AppState calls `applyThresholdChanges()`

2. **Cascading deletes**: When person deleted, what happens to faces?

   **Resolution**: Frontend handles before DELETE. Operations like `dissolve()` and `merge()` must:
   1. Unassign/reassign all faces first (sequenced `await`)
   2. Then DELETE the empty person

   Backend enforces FK constraint - DELETE fails if faces still reference the person.

3. **Derived values**: `face_count` storage strategy.

   **Resolution**: Store and update (faster reads). Drift prevention:
   - `reconcilePerson()` recalculates from actual faces (frontend source of truth)
   - Backend includes `face_count` in key responses (person creation, bulk operations)
   - Periodic full reload from backend resyncs if needed

4. **Bulk operations**: Strategy for large batches (1000+ faces).

   **Resolution**: Background job with polling. Flow:
   1. Frontend sends batch request
   2. Backend returns immediately with `{ job_id }`
   3. AppState polls `/jobs/:id/status` for progress
   4. Final poll returns results → AppState updates cache

   Existing reassessment polling infrastructure supports this pattern.

---

## Implementation Plan

### Strategy: Parallel Development

Create `appstate2.js` alongside existing `appstate.js`:
- Reference current implementation while building new one
- Test new implementation in isolation
- Swap in when ready, delete old file

### Phase 1: Foundation (`appstate2.js`)

**1.1 Core Infrastructure**
```
[ ] Transaction system (transaction(), markDirty(), flushDirty())
[ ] Subscriber system (createSubscriberSystem())
[ ] Queue transaction for async operations
```

**1.2 Cache Primitives (`_internal`)**
```
[ ] faces._internal: get, linkToPerson, unlinkFromPerson, setLocked, setSuppressed
[ ] people._internal: get, add, remove, findByName, setName, setPreferred, setThreshold, bustThumbnail
```

**1.3 Batch Helpers (`_internal`)**
```
[ ] faces._internal.assignToPersonBatch(faceIds, personId, { lock })
[ ] faces._internal.unassignBatch(faceIds)
[ ] faces._internal.pickNewestFace(faceIds)
[ ] people._internal.reconcilePerson(personId)
[ ] people._internal.reconcileAll(personIds)
```

### Phase 2: Public Methods (faces domain)

**2.1 Manual Operations (optimistic + persist)**
```
[ ] identify(faceIds, personName, options) - lock=true, triggers backend search
[ ] unassign(faceIds) - unlocks
[ ] suppress(faceIds) - unassigns first if needed
[ ] setLocked(faceIds, locked) - with preferred-face guard
```

**2.2 Reactive Operations (backend-triggered, no persist)**
```
[ ] autoAssign(faceIds, personId) - lock=false, after identify response
[ ] applyThresholdChanges(personId, assigned, unassigned) - after threshold change
```

**2.3 Data Access**
```
[ ] load() - fetch from backend, populate cache
[ ] getAll() - return cached faces
[ ] getForPerson(personId) - filtered view
[ ] getUnknown() - faces without person_id
```

### Phase 3: Public Methods (people domain)

**3.1 CRUD Operations**
```
[ ] load() - fetch from backend
[ ] getAll() - return cached people
[ ] get(personId) - single person
[ ] search(query) - fuzzy name search
```

**3.2 Mutations**
```
[ ] rename(personId, newName) - handles merge/dissolve delegation
[ ] merge(fromId, toId) - sequenced persist
[ ] dissolve(personId) - sequenced persist
[ ] setPreferred(personId, faceId) - also locks
[ ] setThreshold(personId, threshold) - triggers backend re-evaluation
```

### Phase 4: Persist Functions

**4.1 Simple Persists**
```
[ ] _persistIdentify(faceIds, personId, createdPerson)
[ ] _persistUnassign(faceIds)
[ ] _persistSuppress(faceIds)
[ ] _persistSetLocked(faceIds, locked)
[ ] _persistRename(personId, name)
[ ] _persistSetPreferred(personId, faceId)
[ ] _persistSetThreshold(personId, threshold) - handles response
```

**4.2 Sequenced Persists**
```
[ ] _persistMerge(faceIds, fromId, toId) - assign THEN delete
[ ] _persistDissolve(faceIds, personId) - unassign THEN delete
```

### Phase 5: Polling Integration

```
[ ] Reassessment polling after identify() → poll until complete → autoAssign()
[ ] Threshold polling after setThreshold() → poll until complete → applyThresholdChanges()
[ ] Status polling for background jobs (bulk operations)
```

### Phase 6: Cutover

Swap files as soon as appstate2.js is complete. Screens then migrate against the new appstate.js directly - no conditionals.

```
[ ] Test appstate2.js in isolation (unit tests, console testing)
[ ] Replace: mv appstate.js appstate-old.js && mv appstate2.js appstate.js
[ ] Verify app still loads (new AppState, existing screens)
[ ] Keep appstate-old.js until all migrations complete (rollback option)
```

### Phase 7: Screen Migration

Migrate screens one at a time to use new AppState patterns:

**7.1 faces.js**
```
[ ] Replace direct API calls with AppState methods
[ ] Subscribe to AppState.faces.onChanged / AppState.people.onChanged
[ ] Remove local state arrays (use AppState.faces.getAll() etc.)
[ ] Test all user actions from inventory
```

**7.2 fullscreen.js (tagging mode)**
```
[ ] Use AppState.faces.identify/unassign/suppress for bbox actions
[ ] Subscribe to face changes for overlay updates
```

**7.3 gallery.js (people filter)**
```
[ ] Use AppState.people for filter dropdown
[ ] Subscribe to people changes
```

### Phase 8: Backend Simplification

**8.1 New Simple Endpoints**
```
[ ] POST /people - accept frontend-generated ID
[ ] PATCH /people/:id - simple field updates
[ ] DELETE /people/:id - just delete (FK enforced)
[ ] POST /faces/assign - simple assignment
[ ] POST /faces/unassign - simple unassignment
[ ] PATCH /faces - batch field updates
```

**8.2 Response Changes**
```
[ ] identify response includes { auto_assigned: [...] }
[ ] setThreshold response includes { assigned: [...], unassigned: [...] }
```

**8.3 Deprecate Complex Endpoints**
```
[ ] /faces/identify-batch (replaced by /faces/assign + auto_assigned response)
[ ] /people/:id/merge (frontend orchestrates)
[ ] /people/:id/dissolve (frontend orchestrates)
```

### Phase 9: Cleanup

```
[ ] Delete appstate-old.js
[ ] Update CLAUDE.md with new architecture as current (not "to be")
[ ] Remove any backwards-compatibility shims
```

### Testing Checklist

For each action in the User Actions Inventory:

| Action | Optimistic | Persist | Rollback | Poll |
|--------|------------|---------|----------|-----|
| Identify (new person) | [ ] | [ ] | [ ] | [ ] |
| Identify (existing) | [ ] | [ ] | [ ] | [ ] |
| Unassign | [ ] | [ ] | [ ] | [ ] |
| Suppress | [ ] | [ ] | [ ] | [ ] |
| Lock/Unlock | [ ] | [ ] | [ ] | [ ] |
| Set preferred | [ ] | [ ] | [ ] | [ ] |
| Rename (simple) | [ ] | [ ] | [ ] | [ ] |
| Rename → Merge | [ ] | [ ] | [ ] | [ ] |
| Rename → Dissolve | [ ] | [ ] | [ ] | [ ] |
| Adjust threshold | [ ] | [ ] | [ ] | [ ] |

Edge cases:
```
[ ] Identify empty name → delegates to unassign
[ ] Rename to same name → no-op
[ ] Rename to empty → dissolve
[ ] Rename to existing → merge
[ ] Unassign preferred face → new preferred selected
[ ] Unassign all faces → person auto-deleted
[ ] Suppress identified face → unassigns first
[ ] Unlock preferred face → error
[ ] Network error → rollback shown
```
