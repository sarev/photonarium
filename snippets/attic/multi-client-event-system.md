# Multi-Client Event System

## Context

Photonarium currently only works correctly with a single browser client. The event queue drains on first read (other clients miss events), user-initiated mutations are never broadcast, and custom group operations have a concurrency gap. This change adds multi-client support for home network use — no user accounts, just "all clients see all changes."

Three aspects:
1. **Cursor-based event queue** — replace drain-on-read with timestamp-based cursors
2. **Fix duplicates concurrency** — custom group operations bypass `_db_lock`
3. **Mutation event broadcasting** — user mutations emit diff-shaped events

---

## Part 1: Cursor-Based Event Queue

### Backend: `imagedb.py` (EventQueue class, lines 3883-3951)

**Modify `Event` dataclass:**
- Change `timestamp` from `datetime` to `float` (Unix seconds via `time.time()`)

**Modify `EventQueue` class:**
- Increase `MAX_EVENTS` from 100 to 200
- Replace `get_pending()` (drain-on-read) with `get_since(since: float)`:
  - Returns events where `event.timestamp >= since - 0.1` (100ms safety margin for near-simultaneous events)
  - Returns `{'events': [...], 'server_time': time.time(), 'stale': bool}`
  - `stale = True` when: `since > 0` AND buffer is at capacity AND `since < oldest_event.timestamp`
  - Events stay in the buffer (not cleared on read)
- Keep `emit()` as-is but use `time.time()` for timestamps
- Trimming: oldest events dropped when buffer exceeds `MAX_EVENTS` (existing behaviour)

**Modify `ImageDatabase.get_pending_events()`:**
- Accept `since: float = 0` parameter
- Call `event_queue.get_since(since)` instead of `get_pending()`
- Return `{events, server_time, stale}` (events converted to dicts as before)

### Backend: `app.py` (`/api/events` endpoint, line 1854)

- Accept `?since=T` query parameter (`float(request.args.get('since', 0))`)
- Pass `since` to `get_pending_events(since=...)`
- Response shape: `{success: true, data: {events: [...], server_time: 1707800005.123, stale: false}}`

### Frontend: `static/appstate/events.js`

**New state:**
- `_lastServerTime = 0` — timestamp from previous poll response

**Modify `poll()`:**
- Send `?since=${_lastServerTime}` on each request
- Store `response.data.server_time` into `_lastServerTime`
- On `response.data.stale === true`: call `handleStaleReload()` instead of processing events

**New `handleStaleReload()`:**
- Full reload of all domains (same as initial page load):
  - `AppState.images.invalidate()` then `AppState.images.load()`
  - `AppState.folders.load()`
  - `AppState.people.load(true)`
  - `AppState.faces.load(true)` (if faces are loaded)
  - `AppState.duplicates.invalidate()`
- Log a warning: `[AppState.events] Client is stale, reloading all state`

**On `stopPolling()`:** reset `_lastServerTime = 0` (next start polls from beginning)

---

## Part 2: Fix Duplicates Concurrency

### Backend: `imagedb.py` (ImageDatabase wrapper methods, lines 5969-6012)

Wrap all five custom group methods with `self._db_lock`:

```python
def create_custom_group(self, group_hash, name, image_ids):
    with self._db_lock:
        self._duplicate_manager.create_custom_group(group_hash, name, image_ids)

def rename_custom_group(self, group_hash, name):
    with self._db_lock:
        self._duplicate_manager.rename_custom_group(group_hash, name)

def delete_custom_group(self, group_hash):
    with self._db_lock:
        self._duplicate_manager.delete_custom_group(group_hash)

def add_images_to_custom_group(self, group_hash, image_ids):
    with self._db_lock:
        self._duplicate_manager.add_images_to_custom_group(group_hash, image_ids)

def remove_images_from_custom_group(self, group_hash, image_ids):
    with self._db_lock:
        self._duplicate_manager.remove_images_from_custom_group(group_hash, image_ids)
```

DuplicateManager still creates its own SQLite connections internally — that's fine. SQLite WAL handles concurrent readers, and `_db_lock` (RLock) serializes all writers. The lock also covers the cache updates inside DuplicateManager methods.

---

## Part 3: Mutation Event Broadcasting

### 3A. New Event Constants (`imagedb.py`, after line 3880)

```python
EVENT_FACES_CHANGED = 'faces_changed'
EVENT_PEOPLE_CHANGED = 'people_changed'
EVENT_IMAGES_CHANGED = 'images_changed'
EVENT_GROUPS_CHANGED = 'groups_changed'
```

### 3B. Event Emissions in Flask Routes (`app.py`)

Each mutation endpoint emits an event after the mutation succeeds. Events carry diff-shaped data — just the changed fields, not full objects.

**Face mutations:**

| Route | Event data |
|-------|-----------|
| `POST /faces/assign` | `faces_changed: {updated: [{id, person_id, person_name}]}` |
| `POST /faces/unassign` | `faces_changed: {updated: [{id, person_id: null}]}` |
| `POST /faces/suppress` | `faces_changed: {updated: [{id, suppressed: true}]}` |
| `PATCH /faces` (lock) | `faces_changed: {updated: [{id, manually_tagged}]}` |
| `DELETE /faces/<id>` | `faces_changed: {removed: [id]}` |
| `POST /faces/<id>/identify` | `faces_changed` + possibly `people_changed` |
| `POST /faces/identify-batch` | `faces_changed` + possibly `people_changed` |
| `POST /faces/<id>/unidentify` | `faces_changed` + possibly `people_changed` (if person deleted) |
| `POST /faces/<id>/suppress` | `faces_changed` + possibly `people_changed` (if person deleted) |
| `POST /faces/<id>/unassign` | `faces_changed` + possibly `people_changed` |
| `POST /faces/unassign-batch` | `faces_changed` + possibly `people_changed` |
| `POST /faces/<id>/toggle-manual` | `faces_changed: {updated: [{id, manually_tagged}]}` |

**People mutations:**

| Route | Event data |
|-------|-----------|
| `POST /people` | `people_changed: {upserted: [{id, name, face_count, preferred_face_id}]}` |
| `PATCH /people/<id>` | `people_changed: {upserted: [{id, ...changed}]}` + possibly `faces_changed` (ejected faces) |
| `DELETE /people/<id>` | `people_changed: {removed: [id]}` + `faces_changed: {updated: [{id, person_id: null}]}` |
| `POST /people/<id>/set-preferred` | `people_changed: {upserted: [{id, preferred_face_id}]}` |
| `POST /people/<id>/merge` | `people_changed: {removed: [source_id], upserted: [target]}` + `faces_changed` |
| `POST /people/<id>/dissolve` | `people_changed: {removed: [id]}` + `faces_changed: {updated: [...]}` |

**Image mutations:**

| Route | Event data |
|-------|-----------|
| `POST /images/<id>` (rate/describe) | `images_changed: {updated_ids: [id]}` |
| `POST /images/trash` | `images_changed: {removed_ids: [...]}` |
| `POST /images/rotate` | Already emits `images_modified` — no change needed |

**Group mutations:**

| Route | Event data |
|-------|-----------|
| `POST /groups` | `groups_changed: {level: 5, invalidate: true}` |
| `PATCH /groups/<hash>` | `groups_changed: {level: 5, invalidate: true}` |
| `DELETE /groups/<hash>` | `groups_changed: {level: 5, invalidate: true}` |
| `POST /groups/<hash>/images` | `groups_changed: {level: 5, invalidate: true}` |
| `POST /groups/<hash>/images/remove` | `groups_changed: {level: 5, invalidate: true}` |

**Other:**

| Route | Event data |
|-------|-----------|
| `POST /duplicates/prune` | `images_changed: {removed_ids: [...]}` + `groups_changed: {level, invalidate: true}` |

### 3C. Frontend Auto-Apply Methods

Methods that update AppState cache directly from event data, without API calls (backend already persisted). Pattern matches existing `autoAssign()` in `identity.js:907`.

**`static/appstate/identity.js` — Faces domain (after `autoAssign` at line 918):**

```javascript
autoUpdate(updates) {
    if (!updates?.length || !_cache) return;
    transaction(() => {
        for (const upd of updates) {
            const face = _cache.get(upd.id);
            if (!face) continue;
            if ('person_id' in upd) {
                face.person_id = upd.person_id;
                face.person_name = upd.person_name || null;
            }
            if ('suppressed' in upd) face.suppressed = upd.suppressed;
            if ('manually_tagged' in upd) face.manually_tagged = upd.manually_tagged;
        }
        invalidateDerived();
        markDirty(domainRef);
    });
},

autoRemove(faceIds) {
    if (!faceIds?.length || !_cache) return;
    transaction(() => {
        for (const fid of faceIds) _cache.delete(fid);
        invalidateDerived();
        markDirty(domainRef);
    });
},
```

**`static/appstate/identity.js` — People domain (after existing `delete` method):**

```javascript
autoUpsert(people) {
    if (!people?.length) return;
    if (!_cache) return;
    transaction(() => {
        for (const p of people) {
            const existing = _cache.get(p.id);
            if (existing) Object.assign(existing, p);
            else _cache.set(p.id, { ...p });
        }
        markDirty(domainRef);
    });
},

autoRemove(personIds) {
    if (!personIds?.length || !_cache) return;
    transaction(() => {
        for (const pid of personIds) _cache.delete(pid);
        markDirty(domainRef);
    });
},
```

**`static/appstate/images.js` — Images domain (after existing `refreshByIds`):**

```javascript
autoRemove(ids) {
    if (!ids?.length || !_cache) return;
    transaction(() => {
        for (const id of ids) {
            handleFaceCleanup(id);           // existing function at line 361
            AppState.duplicates._internal.removeImage(id);
            _internal.remove(id);
        }
    });
},
```

### 3D. Frontend Event Handlers (`static/appstate/events.js`)

New cases in the `processEvent` switch (after line 101):

```javascript
case 'faces_changed':
    handleFacesChanged(data);
    break;
case 'people_changed':
    handlePeopleChanged(data);
    break;
case 'images_changed':
    await handleImagesChanged(data);
    break;
case 'groups_changed':
    handleGroupsChanged(data);
    break;
```

New handler functions:

- **`handleFacesChanged(data)`**: Calls `AppState.faces.autoUpdate(data.updated)` and `AppState.faces.autoRemove(data.removed)`. Invalidates people cache (face counts changed).
- **`handlePeopleChanged(data)`**: Calls `AppState.people.autoUpsert(data.upserted)` and `AppState.people.autoRemove(data.removed)`.
- **`handleImagesChanged(data)`**: Calls `AppState.images.autoRemove(data.removed_ids)` and `AppState.images.refreshByIds(data.updated_ids)` (existing delta sync).
- **`handleGroupsChanged(data)`**: Calls `AppState.duplicates.invalidate(data.level)` then broadcasts `groupsChanged`.

### 3E. Self-Event Deduplication

No explicit dedup needed — all auto-apply operations are idempotent:
- Assigning `{face_id: X, person_id: Y}` when cache already has that is a no-op (Object.assign with same values)
- Removing an already-removed ID from a Map returns false, `_internal.remove` checks this
- Delta sync for images returns nothing if images haven't changed since the stored epoch

---

## Part 4: Offline Detection and Mutation Guard

### Problem

If a user's device loses Wi-Fi, the AppState optimistic update pattern means mutations appear to succeed locally (cache updated, UI reflects change). When connectivity returns and the client is marked stale, everything reloads and those changes are silently lost.

### Solution

Track connectivity via polling success/failure. When offline, block all mutations before the optimistic update — show a toast instead of allowing a change that can't be persisted.

### Implementation

**`static/core.js` — App-level connectivity tracking:**

```javascript
// State
App._lastSuccessfulPoll = Date.now();
App._wasOffline = false;

// Called by events.js and status.js on every successful poll
App.markOnline = function() {
    const wasOfflineMs = Date.now() - App._lastSuccessfulPoll;
    App._lastSuccessfulPoll = Date.now();
    if (App._wasOffline) {
        App._wasOffline = false;
        // Only show "restored" toast if offline for >10 minutes.
        // On flaky Wi-Fi, brief dropouts are common and the toast
        // would be annoyingly spammy.  The "offline" toast is safe
        // because it only fires in response to a user-initiated
        // mutation, not on every poll failure.
        if (wasOfflineMs > 10 * 60 * 1000) {
            App.showToast('Connection restored.');
        }
    }
};

// Check if backend is unreachable
App.isOffline = function() {
    return Date.now() - App._lastSuccessfulPoll > 6000;  // 3 missed 2-second polls
};

// Guard for mutation methods — returns false and shows toast if offline
App.requireOnline = function() {
    if (App.isOffline()) {
        App._wasOffline = true;
        App.showToast('You appear to be offline. Changes cannot be saved.');
        return false;
    }
    return true;
};
```

**`static/appstate/events.js` and `status.js` — Mark online on successful poll:**

```javascript
// In poll() success path:
App.markOnline();
```

**`static/appstate/identity.js`, `images.js`, etc. — Guard mutation methods:**

Add `if (!App.requireOnline()) return;` as the first line of every AppState mutation method that makes API calls. These include:

- `AppState.faces`: `identify()`, `unassign()`, `suppress()`, `lock()`, `deleteFace()`
- `AppState.people`: `create()`, `rename()`, `delete()`, `merge()`, `dissolve()`, `setPreferred()`, `setThreshold()`
- `AppState.images`: `update()`, `delete()`, `rotate()`
- `AppState.folders`: `add()`, `remove()`
- `AppState.duplicates`: `createGroup()`, `renameGroup()`, `deleteGroup()`, `addToGroup()`, `removeFromGroup()`

This prevents the optimistic update from even starting, so the user sees the toast immediately with no visual flash of the change being applied then reverted.

---

## Files Modified

| File | Changes |
|------|---------|
| `imagedb.py` | EventQueue: cursor-based `get_since()`, new event constants, custom group lock wrappers |
| `app.py` | `/api/events` accepts `?since=`, event emissions in ~20 route handlers |
| `static/core.js` | `markOnline()`, `isOffline()`, `requireOnline()` connectivity tracking |
| `static/appstate/events.js` | Cursor-based polling, `_lastServerTime`, stale reload, 4 new event handlers, `markOnline()` calls |
| `static/appstate/status.js` | `markOnline()` call on successful poll |
| `static/appstate/identity.js` | `autoUpdate`/`autoRemove` on faces, `autoUpsert`/`autoRemove` on people, `requireOnline()` guards |
| `static/appstate/images.js` | `autoRemove` for trashed images, `requireOnline()` guards |
| `static/appstate/folders.js` | `requireOnline()` guards |
| `static/appstate/duplicates.js` | `requireOnline()` guards |

---

## Implementation Order

1. **Cursor-based EventQueue** — `imagedb.py` EventQueue class
2. **`/api/events` endpoint** — `app.py` accept `?since=`
3. **Frontend polling** — `events.js` use `?since=`, handle staleness
4. **Fix duplicates concurrency** — `imagedb.py` wrapper methods (independent of 1-3)
5. **Offline detection** — `core.js` connectivity tracking
6. **Event constants** — `imagedb.py`
7. **Frontend auto-apply methods** — `identity.js`, `images.js`
8. **Backend mutation events** — `app.py` route handlers (most numerous step)
9. **Frontend event handlers** — `events.js` switch cases
10. **Offline guards** — `requireOnline()` in all AppState mutation methods

---

## Verification

1. Open two browser tabs to the app
2. **Tab A names a face** → Tab B sees the face move from unknown to the person's section
3. **Tab B rates an image** → Tab A sees the rating change in the info panel
4. **Tab A creates a group** → Tab B sees it appear in Groups screen
5. **Tab A trashes images** → Tab B sees them disappear from Gallery
6. **Close Tab B, make changes in Tab A, reopen Tab B** → Tab B catches up via event polling
7. **Close Tab B for 10+ minutes, make many changes** → Tab B gets `stale: true`, full reload
    - Note: full reload should be as unintrusive to the user as possible — stay on same screen, same
      objects selected and in view, where possible, input focus in same place, etc. Also needs to deal
      with edge-cases like image is trashed by user A that user B is looking at fullscreen...
8. **Two tabs modify groups simultaneously** → no data corruption (lock serializes)
9. **Rapid face naming in both tabs** → both converge to same state (idempotent events)
10. **Stop the backend server** → after ~6 seconds, mutations blocked with "offline" toast
11. **Restart backend after brief outage** → silently reconnects, no toast (brief). After 10+ min outage → "Connection restored" toast
12. **Wi-Fi disconnect during face naming** → immediate toast, no phantom changes in cache
