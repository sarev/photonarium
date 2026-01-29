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
