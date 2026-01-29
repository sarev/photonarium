# AppState Design Sketch

## Core Principles

1. **Single source of truth** - all state lives here
2. **Reactive subscriptions** - components subscribe to what they care about
3. **Persistence abstraction** - callers don't know if it's localStorage, backend, or memory
4. **Derived views** - computed properties that auto-update

---

## Images Domain - Examples

```javascript
AppState.images.getAll()              // Filtered/sorted per current criteria
AppState.images.getById(id)           // Single image with metadata
AppState.images.getCount()            // For stats display

AppState.images.update(id, {rating, description})  // Partial update
AppState.images.rotate(id, degrees)   // Triggers thumbnail regeneration
AppState.images.delete(ids)           // Batch delete

AppState.images.onChanged(callback)   // Any image added/removed/modified
AppState.images.onDeleted(callback)   // Specific hook for deletion cleanup
```

**Persisted:** Backend database
**Used by:** Gallery, Fullscreen, Search results

---

## Selection Domain - Examples

```javascript
AppState.selection.get()              // Current selected IDs
AppState.selection.set(ids)           // Replace selection
AppState.selection.add(id)            // Ctrl+click
AppState.selection.remove(id)         // Deselect
AppState.selection.toggle(id)         // Toggle single
AppState.selection.clear()            // Clear all
AppState.selection.selectAll()        // Select all visible
AppState.selection.getAnchor()        // For shift+click ranges
AppState.selection.setAnchor(id)

AppState.selection.onChanged(callback)
```

**Persisted:** Memory only (ephemeral)
**Used by:** Gallery, Duplicates, Faces - each screen could have its own selection context, or one global

**Design question:** One global selection, or per-screen? Current code has separate selections per screen which seems right.

---

## View Settings Domain - Examples

```javascript
AppState.view.getThumbnailSize()
AppState.view.setThumbnailSize(size)
AppState.view.getSort()               // {field, ascending}
AppState.view.setSort(field, ascending)
AppState.view.getTheme()              // 'light' | 'dark'
AppState.view.setTheme(theme)

AppState.view.onChanged(callback)     // Any view setting changed
```

**Persisted:** localStorage
**Used by:** Gallery, Duplicates, Faces, all toolbars

---

## Filter Domain - Examples

```javascript
AppState.filter.get()                 // Current filter object
AppState.filter.set(filter)           // {text, dateFrom, dateTo, rating, people}
AppState.filter.clear()
AppState.filter.isActive()            // Quick check for UI indicator

AppState.filter.onChanged(callback)
```

**Persisted:** Memory (or localStorage for persistence across sessions?)
**Used by:** Search screen (sets), Gallery (applies), Info panel (shows)

---

## People Domain - Examples

```javascript
AppState.people.getAll()              // List with face counts
AppState.people.getById(id)           // Single person with details
AppState.people.create(name)          // Returns new person
AppState.people.rename(id, name)
AppState.people.delete(id)
AppState.people.setPreferredFace(personId, faceId)
AppState.people.setThreshold(personId, threshold)

AppState.people.getThumbnailUrl(id)   // Handles cache busting internally

AppState.people.onChanged(callback)   // Any person added/removed/modified
```

**Persisted:** Backend database
**Used by:** Faces screen, Search (people filter), Fullscreen tagging autocomplete

---

## Faces Domain - Examples

```javascript
AppState.faces.getAll()               // All faces
AppState.faces.getUnknown()           // Derived: where person_id is null
AppState.faces.getForPerson(personId) // All faces for a person
AppState.faces.getForImage(imageId)   // Faces in an image (for tagging overlay)

AppState.faces.identify(faceIds, name, preferredFaceId)  // Batch identify
AppState.faces.unassign(faceIds)      // Return to unknown pool
AppState.faces.suppress(faceIds)      // Mark as false positive

AppState.faces.search(query)          // Semantic search, returns filtered+sorted

AppState.faces.onChanged(callback)    // Any face modified
AppState.faces.onIdentified(callback) // Specific hook for reassessment trigger
```

**Persisted:** Backend database
**Used by:** Faces screen, Fullscreen tagging mode

---

## Duplicates Domain - Examples

```javascript
AppState.duplicates.getGroups(level)  // Groups at similarity level 0-3
AppState.duplicates.isComputing()     // Background computation in progress
AppState.duplicates.recompute()       // Trigger recomputation

AppState.duplicates.onChanged(callback)
AppState.duplicates.onComputationComplete(callback)
```

**Persisted:** Backend database
**Used by:** Duplicates screen

---

## Folders Domain - Examples

```javascript
AppState.folders.getAll()             // With image counts, scan status
AppState.folders.add(path)
AppState.folders.remove(path)
AppState.folders.rescan(path)         // Or all if no path
AppState.folders.getScanStatus()      // {scanning, queue, current}

AppState.folders.onChanged(callback)
AppState.folders.onScanProgress(callback)
AppState.folders.onScanComplete(callback)
```

**Persisted:** Backend database
**Used by:** Database screen

---

## Navigation Domain - Examples

```javascript
AppState.nav.getScreen()              // Current screen name
AppState.nav.setScreen(name, data)    // Navigate
AppState.nav.goBack()                 // History navigation
AppState.nav.getFullscreenImageId()   // Current image in fullscreen

AppState.nav.onScreenChanged(callback)
```

**Persisted:** Memory (with history stack)
**Used by:** Core navigation, all screens

---

## Subscription Pattern

```javascript
// Component subscribes on init
const unsubscribe = AppState.faces.onChanged((event) => {
    // event: {type: 'added'|'removed'|'modified', ids: [...], data: {...}}
    this.refresh();
});

// Component unsubscribes on destroy
unsubscribe();
```

---

## Concrete Example: Faces Domain

The faces/people data is a good candidate for initial AppState integration due to its complexity and the current pain points around cache invalidation.

### State Shape

```javascript
AppState.faces = {
    // Raw data (cached from backend)
    _faces: Map<faceId, Face>,        // All face records
    _people: Map<personId, Person>,   // All people records

    // Derived views (cached, invalidated on change)
    _unknownFaces: null,              // Lazy: faces where person_id is null
    _facesByPerson: null,             // Lazy: Map<personId, Face[]>

    // Sync state
    _epoch: 0,                        // Current state version
    _pendingChanges: [],              // Debounce buffer
    _debounceTimer: null,
}
```

### Optimistic Update Flow

**Example: User selects 5 faces and clicks "Suppress"**

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. GUI ACTION                                                            │
│    User selects faces, presses Delete                                    │
│    → faces.js calls AppState.faces.suppress([id1, id2, id3, id4, id5])  │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 2. DEBOUNCE                                                              │
│    AppState buffers the change for ~50ms                                 │
│    If more suppress() calls arrive, they're batched together            │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 3. OPTIMISTIC UPDATE                                                     │
│    AppState increments internal epoch (e.g., epoch = 1706540000123)     │
│    Updates RAM cache: marks faces as suppressed                          │
│    Invalidates derived views (_unknownFaces = null)                     │
│    Broadcasts to listeners: { type: 'changed' }  ← simple, no epoch     │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
┌─────────────────────────────────┐   ┌─────────────────────────────────┐
│ 4a. LISTENERS UPDATE            │   │ 4b. BACKEND PERSIST             │
│     Faces screen re-renders     │   │     POST /api/faces/suppress    │
│     (faces already gone from    │   │     Body: { ids: [...],         │
│      view - instant feedback)   │   │             epoch: 170654... }  │
└─────────────────────────────────┘   └─────────────────────────────────┘
                                                    │
                                    ┌───────────────┴───────────────┐
                                    ▼                               ▼
                    ┌─────────────────────────────┐   ┌─────────────────────────────┐
                    │ 5a. SUCCESS                 │   │ 5b. FAILURE                 │
                    │     Backend returns:        │   │     Backend returns:        │
                    │     { ok: true,             │   │     { ok: false,            │
                    │       epoch: 170654... }    │   │       error: "...",         │
                    │                             │   │       epoch: 170653...,     │
                    │     AppState absorbs -      │   │       state: [...] }        │
                    │     epoch matches, no-op    │   │                             │
                    └─────────────────────────────┘   │     AppState:               │
                                                      │     - Replaces RAM cache    │
                                                      │     - Sets epoch to older   │
                                                      │     - Broadcasts correction │  <-- just another `type: 'changed'` broadcast
                                                      │     - Broadcasts error      │  <-- e.g. for an error banner listener to consume
                                                      └─────────────────────────────┘
```

### Event Types

Subscribers receive simple events - epochs are internal to AppState/backend sync.

```javascript
// Change events (for data subscribers)
{
    type: 'changed',              // Faces data changed
    ids: ['face-1', 'face-2'],    // Affected entities (optional, for fine-grained updates)
}

// Error events (for error banner UI)
{
    type: 'error',
    message: 'Failed to suppress faces: network error',
}
```

### Subscriber Simplicity

The key benefit: **subscribers don't need to think about what to refresh**.

```javascript
// OLD: Complex flag juggling in faces.js
if (needsRefresh) {
    await loadAllFaces();
    needsRefresh = false;
} else if (needsRerender) {
    unknownFacesGrid.render();
    needsRerender = false;
}
// Plus: reloadPending checks, peopleCacheTime invalidation,
// "set flag BEFORE clearing selection" ordering bugs...

// NEW: Simple subscription
AppState.faces.onChanged(() => {
    // Data already updated in AppState, just re-render
    this.render();
});

AppState.people.onChanged(() => {
    // People list changed, refresh the known faces section
    this.renderKnownFaces();
});
```

Subscribers don't care whether the change was:
- A user action (optimistic update)
- A backend confirmation
- A rollback after failure
- A sync from another tab

They just react to "the data I care about changed" and render current state.

### Debouncing Logic

```javascript
suppress(faceIds) {
    this._pendingChanges.push({ op: 'suppress', ids: faceIds });

    if (this._debounceTimer) {
        clearTimeout(this._debounceTimer);
    }

    this._debounceTimer = setTimeout(() => {
        this._flushChanges();
    }, 50);  // 50ms debounce window
}

_flushChanges() {
    const batch = this._consolidateChanges(this._pendingChanges);
    this._pendingChanges = [];

    // Optimistic update
    const epoch = Date.now();
    this._applyChanges(batch, epoch);
    this._broadcast({ ...batch, epoch });

    // Persist to backend
    this._persistToBackend(batch, epoch);
}
```

### Epoch-Based Reconciliation (Internal)

Epochs are an **internal implementation detail** that spans the full persistence stack. Subscribers never see them.

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│   AppState   │ ←──→ │   Backend    │ ←──→ │   Database   │
│  (frontend)  │epoch │  (Waitress)  │epoch │   (SQLite)   │
└──────────────┘      └──────────────┘      └──────────────┘
```

At each boundary, epochs provide the recovery mechanism:
- Backend operation succeeds but database write fails asynchronously
- Database write happens in another thread and fails
- Network hiccup causes retry with stale data

The epoch serves as a logical clock for ordering updates. Responses include two epochs:
- `request_epoch` (X): The epoch of the request being answered
- `response_epoch` (Y): The epoch of the state being returned

**Response handling logic:**

1. **Stale response**: If X < current epoch, a newer request superseded this one - ignore
2. **Success**: X == current AND Y == X - state confirmed, no action needed
3. **Rollback**: X == current AND Y < X - apply older state, broadcast change + error

```javascript
async _persistToBackend(batch, requestEpoch) {
    const response = await App.api('/faces/batch', {
        method: 'POST',
        body: { operations: batch, epoch: requestEpoch }
    });

    // Response contains: { request_epoch: X, response_epoch: Y, state?: [...], error?: "..." }

    // Stale response - a newer request superseded this one, ignore
    if (response.request_epoch < this._epoch) {
        return;
    }

    // Success - state confirmed
    if (response.response_epoch === response.request_epoch) {
        return;
    }

    // Rollback - response_epoch < request_epoch means persistence failed
    this._epoch = response.response_epoch;
    this._faces = new Map(response.state.map(f => [f.id, f]));
    this._invalidateDerivedViews();

    // Subscribers just see "data changed" - they don't know it was a rollback
    this._broadcast({ type: 'changed' });

    // Error banner gets notified separately
    if (response.error) {
        this._broadcastError({ message: response.error });
    }
}
```

From the subscriber's perspective, a rollback is indistinguishable from any other state change - they just re-render with current data. This keeps subscriber code trivially simple.

### Why Faces First?

The faces domain is a good starting point because:

1. **High pain currently** - `peopleCacheTime`, `thumbnailCacheBust`, `needsRefresh` flags
2. **Multi-consumer** - Faces screen, fullscreen tagging, autocomplete all need people data
3. **Frequent updates** - Identifying faces, suppressing, preferred face changes
4. **Complex derived views** - Unknown faces, faces-by-person, people-with-counts
5. **Isolated enough** - Can migrate without touching gallery/duplicates initially

---

## Benefits for Current Pain Points

| Current Pain | With AppState |
|--------------|---------------|
| `peopleCacheTime = 0` scattered everywhere | `AppState.people.invalidate()` or automatic on mutation |
| `thumbnailCacheBust` Map | Internal to `AppState.people.getThumbnailUrl()` |
| `needsRefresh` / `needsRerender` flags | Subscribers just re-render when notified |
| `reloadPending` flag dance | Subscription system handles ordering |
| "set flag BEFORE clearing selection" | Mutations are atomic, notifications after |

---

## Migration Strategy

### Phase 1: Audit

Audit each `*.js` file to identify:

1. **Persistent state it interacts with** - what data does it read/write?
2. **Cache data structures** - lists, dicts, arrays that exist to avoid backend calls
3. **Time-critical paths** - is this in a hot loop (scroll handler, render)?
4. **Async-safety requirements** - can this race with background operations?

### Phase 2: Analysis

Study audit results to identify:

1. **Multi-consumer state** - data needed by multiple modules
2. **Convergent evolution** - similar structures that evolved independently, could consolidate
3. **Caching candidates** - "hot" data that must be RAM-cached vs infrequent pass-through

### Phase 3: Incremental Implementation

Implement one domain at a time:

1. Start with a domain that has clear boundaries (e.g., Folders)
2. Create AppState domain with same data shape as current
3. Migrate one consumer at a time
4. Test at each step
5. Remove old cache/state code once all consumers migrated
6. Repeat for next domain

---

## Preliminary Observations from Codebase

### Likely Consolidation Candidates

- `peopleCache` (faces.js) and autocomplete data structures - both hold people lists for slightly different purposes
- `allFaces` / `displayedFaces` / `knownPeople` - three related views of the same underlying data
- Image metadata fetched/cached differently in gallery.js vs when needed elsewhere

### Multi-Consumer State

| State | Consumers |
|-------|-----------|
| People data | Faces screen, fullscreen tagging autocomplete, search filter, gallery "sort by people" |
| Current filter | Search screen (sets), Gallery (applies), Info panel (shows) |
| Scan/indexing status | Database screen, Faces screen (refresh after new faces detected) |

### Hot Paths (RAM Cache Essential)

- **Thumbnail URLs with cache-busting** - called on every render of visible items
- **`getItems()` for VirtualGrid** - called on every scroll event
- **Autocomplete filtering** - called on every keystroke

### Pass-Through Candidates (No Caching Needed)

- Folder add/remove - infrequent, can wait for backend
- Image rotation - already has loading states
- Person threshold changes - rare operation

### Async-Safety Concerns

These scenarios can cause race conditions with current architecture:

1. **Background reassessment completing** while user is mid-selection/operation
2. **Scan completing** and adding new images/faces while user is browsing
3. **Multiple rapid identifies** before first API call completes
4. **Suppress in fullscreen** while faces screen has stale data

AppState would address these by:
- Queueing mutations and processing sequentially
- Notifying all subscribers after each mutation completes
- Providing consistent snapshots during reads

---

## Open Questions

1. **Per-screen selection or global?** Current code has separate selections per screen which seems right
    Answer: selections are typically per ThumbnailGrid - there's nothing to stop a screen having multiple of these.
2. **Filter persistence?** Memory only, or localStorage for session persistence?
    Answer: Memory only.
3. **Optimistic updates?** Update local state immediately, revert on backend failure?
    Answer: Yes, pass-through cache strategy, unless that doesn't make sense in a specific context.
4. **Batching notifications?** Multiple rapid changes → single notification?
    Answer: If that makes sense. Ideally, we should already have frontend/backend APIs that address common examples of this. There's no reason this component couldn't consolidate incoming changes itself, too.
5. **Derived view caching?** `getUnknown()` recomputes each call, or caches until invalidated?
    Answer: Cache derived views that are called in hot paths (scroll handlers, renders). Recompute for infrequent calls. Example: `getUnknown()` is called by VirtualGrid's `getItems()` on every scroll, so it should cache. `getForImage(imageId)` is called once when entering fullscreen, so recompute is fine.

---

## Implementation Plan

### Audit Phase

- [ ] Audit `core.js` - document state variables, localStorage usage, event bus
- [ ] Audit `gallery.js` - document image data caching, selection state, view settings
- [ ] Audit `fullscreen.js` - document navigation state, current image tracking
- [ ] Audit `faces.js` - document all caches (peopleCache, allFaces, knownPeople, etc.), flags
- [ ] Audit `duplicates.js` - document duplicate groups caching, computation status
- [ ] Audit `database.js` - document folder state, scan status tracking
- [ ] Audit `search.js` - document filter state management
- [ ] Audit `thumbnails.js` - document ThumbnailLoader state, cache-busting

### Analysis Phase

- [ ] Compile audit results into single view
- [ ] Identify multi-consumer state (see "Multi-Consumer State" table)
- [ ] Identify consolidation candidates (see "Likely Consolidation Candidates")
- [ ] Classify each state as: hot-path cached / pass-through / derived view
- [ ] Resolve open questions (see "Open Questions" section)
- [ ] Finalize domain boundaries (see domain sections above)

### Implementation Phase

Order domains by dependency (least dependencies first):

- [ ] Implement `AppState.nav` - navigation state (currently in core.js)
- [ ] Implement `AppState.view` - view settings, theme, thumbnail size (localStorage)
- [ ] Implement `AppState.folders` - folder management, scan status
- [ ] Implement `AppState.filter` - search/filter criteria
- [ ] Implement `AppState.images` - image metadata, the core dataset
- [ ] Implement `AppState.people` - people with cache-busted thumbnail URLs
- [ ] Implement `AppState.faces` - faces with derived views (unknown, by-person, by-image)
- [ ] Implement `AppState.duplicates` - duplicate groups by level
- [ ] Implement `AppState.selection` - per-screen selection contexts

### Migration Phase (per domain)

For each domain above:

- [ ] Create AppState domain module with API from design
- [ ] Add subscription infrastructure
- [ ] Migrate first consumer, verify functionality
- [ ] Migrate remaining consumers one at a time
- [ ] Remove old cache variables and flags
- [ ] Update documentation (faces.js, thumbnails.js, etc.)

### Cleanup Phase

- [ ] Remove obsolete flags: `needsRefresh`, `needsRerender`, `reloadPending`, `peopleCacheTime`
- [ ] Remove obsolete caches: `peopleCache`, `thumbnailCacheBust`, duplicated structures
- [ ] Simplify module documentation now that state management is centralized
- [ ] Update CLAUDE.md with AppState architecture overview
