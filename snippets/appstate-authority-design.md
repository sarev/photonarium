# AppState as Single Source of Truth - Design Document

## Overview

This document outlines a refactored architecture where:
1. **AppState generates all IDs** (using `crypto.randomUUID()`)
2. **Backend is a dumb persistence layer** - validates and stores, doesn't compute
3. **Application logic lives only in AppState** - no duplication
4. **API is simplified** - backend only returns success/error, not "here's what I created"

## User Actions Inventory

### Faces Screen - People List
| Action | Current Trigger | Description |
|--------|-----------------|-------------|
| Rename person | Click name, type, blur/Enter | Change person's display name |
| Merge people | Drag person A onto person B | Move all faces from A to B, delete A |
| Dissolve person | Button in context menu | Return all faces to unknown, delete person |
| Pick preferred | Click person card | Enter picker mode to choose representative face |
| Set preferred | Click face in picker | Set this face as person's thumbnail |
| Adjust threshold | Slider in picker | Change auto-match sensitivity for this person |

### Faces Screen - Unknown Faces List
| Action | Current Trigger | Description |
|--------|-----------------|-------------|
| Identify face(s) | Type name, blur/Enter | Assign face(s) to existing or new person |
| Suppress face(s) | X button or keyboard | Mark as false positive, hide from list |
| Lock face(s) | Toggle in selection | Mark as reference for auto-matching |
| Unlock face(s) | Toggle in selection | Remove from auto-match references |

### Faces Screen - Preferred Picker
| Action | Current Trigger | Description |
|--------|-----------------|-------------|
| Set preferred | Click face | Set as person's representative thumbnail |
| Eject face | Drag out or button | Unassign face, return to unknown |
| Reassign face | Type different name | Move face to different person |
| Lock/unlock | Toggle | Control auto-match behavior |

### Fullscreen Viewer - Tagging Mode
| Action | Current Trigger | Description |
|--------|-----------------|-------------|
| Identify face | Click box, type name | Assign face to person |
| Unidentify face | Click box, clear name | Return face to unknown |
| Suppress face | Click X on box | Mark as false positive |

### Additional Actions (less common)
| Action | Description |
|--------|-------------|
| Un-suppress face | Restore a suppressed face (admin/undo) |
| Delete person | Same as dissolve (no faces = auto-delete) |
| Bulk import names | Future: CSV import of face assignments |

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

### Principle 3: Application Logic Only in AppState

```javascript
// AppState.faces.identify() contains ALL logic
identify(faceIds, personName, options = {}) {
    transaction(() => {
        // 1. Find or create person
        let person = people._internal.findByName(personName);
        if (!person) {
            const personId = crypto.randomUUID();
            person = { id: personId, name: personName, face_count: 0 };
            people._internal.add(person);
        }

        // 2. Track old persons for cleanup
        const oldPersonIds = new Set();
        for (const faceId of faceIds) {
            const face = faces._internal.get(faceId);
            if (face?.person_id) oldPersonIds.add(face.person_id);
        }

        // 3. Link faces to new person
        for (const faceId of faceIds) {
            faces._internal.linkToPerson(faceId, person.id, personName);
            person.face_count++;
        }

        // 4. Update old persons
        for (const oldPersonId of oldPersonIds) {
            const oldPerson = people._internal.get(oldPersonId);
            if (oldPerson) {
                oldPerson.face_count--;
                if (oldPerson.face_count === 0) {
                    people._internal.remove(oldPersonId);
                }
            }
        }

        // 5. Set preferred face if needed
        if (!person.preferred_face_id) {
            person.preferred_face_id = faceIds[0];
        }
    });
    // Broadcast happens here - UI updates instantly

    // 6. Persist to backend (fire and forget, handle errors)
    return this._persistIdentify(faceIds, person.id);
}

async _persistIdentify(faceIds, personId) {
    try {
        // Simple persistence calls - no logic
        await App.apiPost('/faces/assign', {
            face_ids: faceIds,
            person_id: personId
        });
    } catch (err) {
        // Rollback cache state
        this._rollbackIdentify(faceIds, backup);
        throw err;
    }
}
```

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

### 1. Identify Face(s) - New Person

**User action**: Type "Bob" on unknown face, press Enter

**AppState.faces.identify()**:
```javascript
transaction(() => {
    // Generate ID for new person
    const personId = crypto.randomUUID();

    // Create person in cache
    people._internal.add({
        id: personId,
        name: "Bob",
        face_count: faceIds.length,
        preferred_face_id: faceIds[0],
        threshold: null
    });

    // Link faces
    for (const faceId of faceIds) {
        faces._internal.linkToPerson(faceId, personId, "Bob");
    }
});
// UI updates instantly

// Persist (no ID reconciliation needed)
await App.apiPost('/people', { id: personId, name: "Bob" });
await App.apiPost('/faces/assign', { face_ids: faceIds, person_id: personId });
```

### 2. Identify Face(s) - Existing Person

**User action**: Type "Bob" (existing), press Enter

**AppState.faces.identify()**:
```javascript
transaction(() => {
    const person = people._internal.findByName("Bob");
    // person.id is already a real UUID

    // Track faces leaving old persons
    const affectedPersons = new Map();
    for (const faceId of faceIds) {
        const face = faces._internal.get(faceId);
        if (face?.person_id && face.person_id !== person.id) {
            affectedPersons.set(face.person_id,
                (affectedPersons.get(face.person_id) || 0) + 1);
        }
    }

    // Link faces to Bob
    for (const faceId of faceIds) {
        faces._internal.linkToPerson(faceId, person.id, "Bob");
    }
    person.face_count += faceIds.length;

    // Update/remove old persons
    for (const [oldId, count] of affectedPersons) {
        const oldPerson = people._internal.get(oldId);
        oldPerson.face_count -= count;
        if (oldPerson.face_count === 0) {
            people._internal.remove(oldId);
        }
    }
});
// UI updates instantly

// Persist
await App.apiPost('/faces/assign', { face_ids: faceIds, person_id: person.id });
// Note: No need to tell backend about face counts - it computes via JOIN
// Note: No need to tell backend to delete empty persons - ON DELETE CASCADE or cleanup job
```

### 3. Rename Person

**User action**: Click person name, type "Robert", press Enter

**AppState.people.rename()**:
```javascript
transaction(() => {
    const person = people._internal.get(personId);
    person.name = "Robert";

    // Update denormalized name on faces
    const personFaces = faces.getForPerson(personId);
    for (const face of personFaces) {
        face.person_name = "Robert";
    }
});
// UI updates instantly

// Persist
await App.apiPatch(`/people/${personId}`, { name: "Robert" });
```

### 4. Merge People

**User action**: Drag "Alice" onto "Bob"

**AppState.people.merge(fromId, toId)**:
```javascript
transaction(() => {
    const fromPerson = people._internal.get(fromId);
    const toPerson = people._internal.get(toId);

    // Move all faces
    const facesToMove = faces.getForPerson(fromId);
    for (const face of facesToMove) {
        faces._internal.linkToPerson(face.id, toId, toPerson.name);
    }

    // Update counts
    toPerson.face_count += fromPerson.face_count;

    // Remove source person
    people._internal.remove(fromId);
});
// UI updates instantly

// Persist
await App.apiPost('/faces/assign', {
    face_ids: facesToMove.map(f => f.id),
    person_id: toId
});
await App.apiDelete(`/people/${fromId}`);
```

### 5. Suppress Face(s)

**User action**: Select faces, press X

**AppState.faces.suppress(faceIds)**:
```javascript
transaction(() => {
    const affectedPersons = new Map();

    for (const faceId of faceIds) {
        const face = faces._internal.get(faceId);

        // Track person impact
        if (face.person_id) {
            affectedPersons.set(face.person_id,
                (affectedPersons.get(face.person_id) || 0) + 1);
            faces._internal.unlinkFromPerson(faceId);
        }

        // Mark suppressed
        face.suppressed = true;
    }

    // Update/remove affected persons
    for (const [personId, count] of affectedPersons) {
        const person = people._internal.get(personId);
        person.face_count -= count;
        if (person.face_count === 0) {
            people._internal.remove(personId);
        }
    }
});
// UI updates instantly

// Persist
await App.apiPost('/faces/suppress', { face_ids: faceIds });
```

### 6. Set Preferred Face

**User action**: Click face in picker mode

**AppState.people.setPreferred(personId, faceId)**:
```javascript
transaction(() => {
    const person = people._internal.get(personId);
    person.preferred_face_id = faceId;
    people._internal.bustThumbnail(personId);
});
// UI updates instantly

// Persist
await App.apiPatch(`/people/${personId}`, { preferred_face_id: faceId });
```

### 7. Lock/Unlock Faces

**User action**: Toggle lock on selected faces

**AppState.faces.setLocked(faceIds, locked)**:
```javascript
transaction(() => {
    for (const faceId of faceIds) {
        const face = faces._internal.get(faceId);
        face.manually_tagged = locked;
    }
});
// UI updates instantly

// Persist
await App.apiPatch('/faces', { face_ids: faceIds, locked: locked });
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

## Open Questions

1. **Auto-matching**: Backend currently runs face similarity matching. Should this:
   - Stay in backend (ML model lives there)
   - Return "suggested matches" that frontend confirms
   - Trigger via explicit "re-assess" action

2. **Cascading deletes**: When person deleted, should faces:
   - Become unassigned (current)
   - Be handled by frontend before delete API call

3. **Derived values**: `face_count` is currently stored. Should it:
   - Be computed via JOIN on every GET (simpler, always correct)
   - Be stored and updated (faster reads, risk of drift)

4. **Bulk operations**: For large batches (1000+ faces), should we:
   - Single API call (simple, but long request)
   - Chunked calls (complex, but resilient)
   - Background job (async, but need status polling)
