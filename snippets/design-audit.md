# Design Principles Audit

This document catalogues violations of the architectural design principles documented in CLAUDE.md. The audit covers:

1. **Backend/Frontend division** - Backend should be a dumb persistence layer
2. **AppState/GUI division** - AppState is single source of truth, GUI reads and subscribes
3. **AppState internal consistency** - Domains should follow consistent patterns

---

## Executive Summary

After detailed analysis, many "violations" turn out to be justified design decisions. The remaining architectural impurities work correctly and have been accepted as technical debt (documented with inline comments).

| Category | Fixed | Accepted Debt | Permitted Exceptions | Defensive/OK |
|----------|-------|---------------|---------------------|--------------|
| Backend | 0 | 2 (cleanup, auto-reassess) | 5 (cascades, computed reads) | 2 |
| GUI | 0 | 1 (suppress flag) | 2 (local presentation state) | 1 |
| AppState | 1 (_notify) | 1 (selection txn) | 0 | 5 |
| **Total** | **1** | **4** | **7** | **8** |

**Status: Audit complete. No further action required.**

### Key Distinctions

**Defensive Coding (Acceptable):** Backend validation that rejects invalid requests with errors. "You can't do X" is fine.

**Permitted Cascades (Acceptable):** When an operation necessarily results in invalid state (e.g., person with 0 faces), atomic cleanup is acceptable to maintain data integrity.

**Permitted Computed Reads (Acceptable):** GET responses returning aggregated/derived fields is standard API design.

**Local Presentation State (Acceptable):** GUI maintaining sorted/filtered copies for UI-specific criteria (user's sort preference, search query) is necessary when AppState can't know the criteria.

---

## Part 1: Backend Violations

The backend should be a "dumb persistence layer" that validates and stores, but doesn't compute application logic.

### HIGH SEVERITY

#### 1.1 Person Auto-Deletion When Faces Ejected

**File:** `app.py` lines 1426-1445
**Endpoint:** `PATCH /api/people/<person_id>`

```python
if threshold_changed and threshold_value is not None:
    ejected_face_ids = revalidate_person_faces(db.conn, person_id, threshold_value)
    if ejected_face_ids:
        remaining = get_faces_for_person(db.conn, person_id)
        if not remaining:
            delete_person(db.conn, person_id)  # Backend making business decision
            return success_response({'deleted': True, ...})
```

**Violation:** Backend decides to delete person when all faces are ejected.

**Assessment: PERMITTED EXCEPTION (Atomic Data Integrity)**

Justification:
- A person with 0 faces is an invalid state (no thumbnail, can't be used)
- Threshold change → face revalidation → potential deletion is a single logical operation
- Without atomic cascade, race conditions possible between backend response and frontend cleanup
- Alternative (frontend deletes empty person) requires extra roundtrip and risks inconsistent state

Document as: "Atomic cascade permitted when operation necessarily results in invalid entity state"

---

#### 1.2 Preferred Face Auto-Selection on Unassign

**File:** `app.py` lines 2317-2325, 2389-2407
**Endpoints:** `POST /api/faces/<face_id>/unassign`, `POST /api/faces/unassign-batch`

```python
if person and person.get('preferred_face_id') == face_id:
    remaining_faces = get_faces_for_person(db.conn, old_person_id)
    if remaining_faces:
        new_preferred = remaining_faces[-1]['id']
        update_person(db.conn, old_person_id, preferred_face_id=new_preferred)
        db.conn.execute("UPDATE faces SET manually_tagged = 1 ...")
```

**Violation:** Backend auto-selects new preferred face and locks it.

**Assessment: PERMITTED EXCEPTION (Data Integrity Constraint)**

Justification:
- A person with faces MUST have a valid `preferred_face_id` (used for thumbnails everywhere)
- Without auto-selection, person would be in invalid state until frontend follows up
- "Newest face" is a reasonable default heuristic
- Alternative requires frontend to always check and set preferred face - error-prone

However, the auto-locking (`manually_tagged = 1`) is questionable and could be removed.

Document as: "Auto-selection permitted to maintain required field invariants"

---

#### 1.3 Empty Person Auto-Deletion

**File:** `app.py` lines 2328, 2410
**Endpoints:** `POST /api/faces/<face_id>/unassign`, `POST /api/faces/unassign-batch`

```python
delete_people_without_faces(db.conn)
```

**Violation:** Backend automatically deletes all people with zero faces.

**Assessment: ACCEPTED TECHNICAL DEBT**

This is a global cleanup that deletes ALL empty people, not just the affected one.

Arguments for keeping:
- Prevents orphaned person records accumulating
- Simplifies frontend (doesn't need to track and cleanup empty people)
- A person with 0 faces has no utility (no thumbnail, can't filter by them)

Arguments against:
- Frontend loses control over when cleanup happens
- Global cleanup is non-obvious side effect of unassign operation

Verdict: **Accepted as-is.** The frontend already handles this case explicitly in picker mode (see "Partial Cache Limitation" in CLAUDE.md), so the backend cleanup is belt-and-suspenders. Changing to targeted cleanup risks missing edge cases. Inline comment added pointing to this audit.

---

#### 1.4 Face Ejection Logic in Helper Function

**File:** `faces.py` lines 1197-1223
**Function:** `revalidate_person_faces()`

```python
if ejected_ids:
    for face_id in ejected_ids:
        update_face_person(conn, face_id, None)

    person = get_person(conn, person_id)
    if person and person.get('preferred_face_id') in ejected_ids:
        remaining = conn.execute(...)
        if remaining:
            new_preferred = remaining[0]['id']
            conn.execute('UPDATE people SET preferred_face_id = ? ...')
```

**Violation:** Helper function combines ejection + preferred face selection.

**Assessment: PERMITTED (Implementation Detail of 1.1 and 1.2)**

This helper implements the atomic cascade from 1.1 and the preferred face invariant from 1.2. If those are permitted exceptions, this is just the implementation detail. Not a separate violation.

---

### MEDIUM SEVERITY

#### 1.5 Computing face_count in GET Responses

**File:** `faces.py` lines 1033-1042, 1066-1083
**Functions:** `get_person()`, `get_all_people()`

```python
SELECT p.*, COUNT(f.id) as face_count
FROM people p
LEFT JOIN faces f ON f.person_id = p.id AND f.suppressed = 0
WHERE p.id = ?
GROUP BY p.id
```

**Assessment: PERMITTED EXCEPTION (Read Efficiency)**

This is standard API design. Virtually every real-world API returns aggregated/computed fields in GET responses. The alternative would require frontend to:
1. GET all people (without counts)
2. GET all faces
3. Compute counts locally

This would be inefficient and defeat the purpose of having a database. The principle about "not computing state" applies to mutations (don't decide what to do), not reads (return useful data).

Document as: "Computed fields in GET responses are permitted for efficiency"

---

#### 1.6 Returning Computed Response Flags

**File:** `app.py` lines 1440-1444, 1457-1465
**Endpoint:** `PATCH /api/people/<person_id>`

```python
return success_response({
    'deleted': True,
    'unassigned': ejected_face_ids,
    'message': 'All faces ejected, person deleted'
})

response_data['faces_changed'] = faces_changed or (...)
```

**Assessment: NECESSARY CONSEQUENCE (of permitted cascades)**

These flags exist because the backend performs cascading operations (1.1, 1.2). Without them, frontend would have no way to know what the backend did.

If cascades are permitted, these flags are required. They're not the backend "computing application logic" - they're the backend reporting what it did so frontend can update correctly.

Alternative: Frontend refetches all affected state after mutation. But that's inefficient and the response flags are more precise.

Document as: "Response flags permitted when reporting results of permitted cascades"

---

### DEFENSIVE CODING (Acceptable)

#### 1.7 Backend Validating Person Name Uniqueness

**File:** `app.py` lines 1335-1338, 1399-1402
**Endpoints:** `POST /api/people`, `PATCH /api/people/<person_id>`

```python
existing_name = get_person_by_name(db.conn, name)
if existing_name:
    return error_response(f'Person with name "{name}" already exists', 409)
```

**Assessment:** Acceptable defensive coding. Backend rejects the request with an error rather than making a decision about what to do. Frontend AppState also checks for duplicates (find-or-create pattern), so this is belt-and-suspenders validation.

---

#### 1.8 Auto-Triggering Async Reassessment

**File:** `app.py` lines 1449-1455
**Endpoint:** `PATCH /api/people/<person_id>`

```python
if threshold_changed and threshold_value is not None:
    reassess_unknown_faces_async(
        db,
        threshold=threshold_value,
        person_id=person_id,
    )
```

**Assessment: ACCEPTED TECHNICAL DEBT**

Arguments for:
- The whole PURPOSE of changing threshold is to re-evaluate faces
- User expectation: "I lowered the threshold, now find more matches"
- Without auto-trigger, frontend must always send a second request
- ML operations are already backend-driven (exception in design)

Arguments against:
- `/api/faces/assign` has explicit `trigger_reassessment` flag - inconsistent
- Frontend loses control over when expensive operation runs
- Could be batching multiple changes before reassessing

Verdict: **Accepted as-is.** The current behavior matches user expectations and adding an explicit flag would be a breaking change for no practical benefit. Inline comment added pointing to this audit.

---

#### 1.9 Threshold Range Validation

**File:** `app.py` lines 1414-1420

```python
if not (0.0 <= threshold <= 1.0):
    return error_response('recognition_threshold must be between 0 and 1')
```

**Assessment:** Acceptable defensive coding. Rejects invalid input with an error rather than making decisions.

---

### EXCEPTIONS (Acceptable)

#### 1.10 Face ID Generation During Detection

**File:** `faces.py` lines 1336-1337

```python
if face_id is None:
    face_id = str(uuid.uuid4())
```

**Assessment:** Acceptable - ML-generated entities are exceptions per CLAUDE.md. Frontend cannot pre-generate IDs for faces it doesn't know about.

---

#### 1.11 Image ID Generation During Ingestion

**File:** `imagedb.py` line 1870

```python
image_id = str(uuid.uuid4())
```

**Assessment:** Acceptable - Image ingestion is backend-driven (folder scanning). Frontend cannot pre-generate IDs for images it doesn't know about.

---

## Part 2: GUI Module Violations

GUI modules should read from AppState, subscribe to changes, and never maintain local data copies or mutate AppState arrays.

### HIGH SEVERITY

#### 2.1 Subscription Suppression Flag

**File:** `faces.js` lines 299, 1029-1030, 4131-4132, 4156, 4180, 4223, 4602, 4659, 4700, 4704, 4719, 4732

```javascript
// Declaration
let suppressOverlayReload = false;

// In subscription handler
if (suppressOverlayReload) {
    facesLog('  -> Skipping fullscreen reload (suppressOverlayReload)');
} else {
    // ... reload faces
}

// Around async operations
suppressOverlayReload = true;
try {
    await AppState.faces.identify(unknownFaceIds, '-');
} finally {
    suppressOverlayReload = false;
}
```

**Assessment: ACCEPTED TECHNICAL DEBT**

The comment in code says "commitNameChange updates the UI directly, no need to re-render". This is avoiding REDUNDANT work - the identify operation already updates the fullscreen overlay directly, so the subscription would just re-do the same work.

Arguments for keeping:
- Avoids redundant DOM operations
- The overlay is already visually correct after the direct update
- Performance optimization for rapid face tagging

Arguments against:
- Violates "trust subscriptions" principle
- Better patterns exist: debouncing, request ID tracking
- Creates coupling between the mutation and the handler

Verdict: Technically a violation, but solving a real problem. Refactoring to debouncing would risk introducing race conditions without automated tests to catch regressions. **Accepted as-is.** Inline comment added pointing to this audit.

---

#### 2.2 Direct Array Mutation via sort()

**File:** `duplicates.js` lines 578, 626, 696

```javascript
// Direct mutation of state array
this.state.allGroups.sort((a, b) => b.count - a.count);

// Another sort
this.state.allGroups.sort((a, b) => {
    const namesA = peopleNames[a.group_hash] || '';
    const namesB = peopleNames[b.group_hash] || '';
    return namesA.localeCompare(namesB, ...);
});
```

**Assessment: PERMITTED EXCEPTION (Local Presentation State)**

Looking at the full context (see 2.4 below), this is actually sorting a LOCAL COPY, not the AppState array. The pattern is:
1. `this.state.allGroups = [...groups]` - copy from AppState
2. `this.state.allGroups.sort(...)` - sort the local copy

The sorting criteria includes:
- Size (count)
- People names (requires async face data fetch)
- Semantic similarity to user's search query (local state)

These are PRESENTATION concerns that depend on local UI state. AppState doesn't know what sort order the user wants or what search query they typed. The local copy is necessary.

Document as: "Local presentation state permitted for UI-specific sorting/filtering that depends on local criteria"

---

#### 2.3 Mutating imageList in Fullscreen

**File:** `fullscreen.js` line 1230

```javascript
this.state.imageList = imageList.filter(img => img.id !== imageId);
```

**Assessment: NOT A VIOLATION (Correct Optimistic Update)**

On closer inspection, this is CORRECT behavior:
1. Line 175: `this.state.imageList = AppState.images.getDisplayList()` - gets reference
2. Line 1230: `this.state.imageList = imageList.filter(...)` - creates NEW array via filter()
3. Line 1252: On error, restores from AppState

The `.filter()` method creates a new array - it does NOT mutate the original. This is the standard optimistic update pattern: update local state immediately, then persist, rollback on error.

**Remove from violations list.**

---

### MEDIUM SEVERITY

#### 2.4 Local Array Copy Pattern (Duplicates)

**File:** `duplicates.js` lines 349, 441, 480

```javascript
// Copy from AppState
this.state.allGroups = [...groups];  // Copy for sorting

// Then later mutate the copy
this.state.allGroups.sort((a, b) => b.count - a.count);
```

**Assessment: PERMITTED EXCEPTION (see 2.2 above)**

The copy-then-sort pattern is intentional and correct. The comment even says "We copy because sorting mutates the array". This is a GUI module maintaining presentation state that depends on local criteria (user's sort preference, search query).

The alternative would be for AppState.duplicates to accept sort parameters, but that would couple AppState to UI concerns like "sort by semantic similarity to this search query".

---

## Part 3: AppState Internal Consistency

AppState domains should follow consistent patterns for cache management, transactions, and broadcasting.

### HIGH SEVERITY

#### 3.1 ~~Loading Domain Missing `_notify`~~ (RESOLVED)

**File:** `loading.js`

**Status:** Fixed. Added `_notify: notify` to the loading domain's public API, matching the pattern used by other domains.

---

### MEDIUM SEVERITY

#### 3.2 Selection Domain Mutates Cache Outside Transactions

**File:** `selection.js` lines 126-199

```javascript
set(context, ids) {
    // Direct mutation without transaction
    const ctx = getContext(context);
    ctx.selected = new Set(idArray);
    ctx.anchor = idArray.length > 0 ? idArray[idArray.length - 1] : null;

    // Direct broadcast (not using markDirty)
    broadcast({ type: 'changed', context });
}
```

**Assessment: ACCEPTED (not actually a problem)**

Selection is synchronous-only and doesn't need cross-domain coordination. Transaction batching would add complexity for no benefit. Direct broadcast is fine here.

---

#### 3.3 Selection Domain Missing `_internal` API

**File:** `selection.js`

**Assessment: ACCEPTED (not actually a problem)**

No use case exists for atomic selection operations coordinated with other domains. Selection is a UI concern that doesn't need to participate in data transactions.

---

#### 3.4 Loading Domain Direct Broadcasts

**File:** `loading.js`

```javascript
show(owner, message = 'Loading…') {
    // ...
    broadcast({ type: 'changed', visible: true, owner, message });  // Direct
}
```

**Assessment: ACCEPTED (not actually a problem)**

Loading overlay is a UI concern with immediate visual feedback requirements. Transaction batching would add latency for no benefit.

---

#### 3.5 Inconsistent Dirty Flag Marking

- `images.js`, `identity.js`, `duplicates.js` - use `markDirty(domainRef)`
- `selection.js` - does NOT use `markDirty()` at all

Inconsistent approach to triggering broadcasts, but selection and loading don't need to batch with data domains. This is fine.

---

### LOW SEVERITY

#### 3.6 _internal Methods Only Used Internally

**File:** `identity.js` lines 209-256

Some batch helpers in `faces._internal` are only called from within the same domain:
- `assignToPersonBatch()` - only from `identify()`, `autoAssign()`
- `unassignBatch()` - only from `unassign()`, `suppress()`
- `pickNewestFace()` - only from `identify()`

**Assessment:** Not a bug - these could be private helpers instead of `_internal`, but current structure works.

---

## Recommendations

### Completed

1. ~~**Add `_notify` to loading.js** (AppState) - Critical. Transaction system expects this.~~ **DONE**

### Accepted Technical Debt

The following items are architectural impurities that work correctly. Given the lack of automated tests and the risk of introducing regressions, these are accepted as-is. Each has been documented with inline comments pointing to this audit.

2. **`suppressOverlayReload` flag** (faces.js) - Violates "trust subscriptions" principle but prevents redundant DOM updates during rapid face identification. Refactoring to debouncing would require careful timing analysis and risk race conditions. **ACCEPTED**

3. **`delete_people_without_faces()` global cleanup** (Backend) - Aggressively cleans ALL empty people on any unassign, not just the affected one. Works correctly and prevents orphaned records. Changing to targeted cleanup would reduce blast radius but risks missing edge cases. **ACCEPTED**

4. **Auto-trigger reassessment on threshold change** (Backend) - Inconsistent with `/api/faces/assign` which has explicit `trigger_reassessment` flag. However, the current behavior matches user expectations (change threshold → re-evaluate faces). **ACCEPTED**

5. **Selection domain bypasses transactions** (AppState) - Selection mutates directly without transaction batching. This is fine because selection is synchronous-only and doesn't need cross-domain coordination. **ACCEPTED** (not actually a problem)

### Documented as Permitted Exceptions

The following are intentional design decisions and should be documented in CLAUDE.md:

- **Atomic cascades:** Backend may clean up invalid state atomically (person deletion when all faces removed)
- **Data integrity invariants:** Backend may auto-select to maintain required fields (preferred_face_id)
- **Computed GET responses:** Returning aggregated fields in reads is standard API design
- **Local presentation state:** GUI may maintain sorted/filtered copies for UI-specific criteria
- **Defensive validation:** Backend rejecting invalid requests with errors is encouraged

---

## Notes

- This audit focuses on clear violations, not style preferences
- Many apparent "violations" are intentional design decisions with solid justifications
- The partial cache limitation (documented in CLAUDE.md) explains some GUI workarounds
- ML-generated data (faces, images) legitimately has backend-generated IDs

### Key Principles Clarified

| Principle | Violation | Not a Violation |
|-----------|-----------|-----------------|
| Backend doesn't compute | Auto-delete, auto-cascade | Defensive validation, computed GETs |
| Frontend owns logic | Backend orchestration | Backend enforcing invariants |
| Don't mutate AppState | Direct mutation | Copy-then-mutate for local sort |
| Trust subscriptions | Suppress flags | Debouncing, version tracking |

### The Key Question

When evaluating potential violations, ask: **"Is the backend/GUI making a DECISION, or just maintaining CONSTRAINTS?"**

- Decisions belong in AppState (what to do when X happens)
- Constraints belong where enforced (data must be valid, required fields must exist)
