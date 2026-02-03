# Plan: `images_modified` Event System

## Overview

Implement a generic event mechanism for notifying the frontend when images have been modified by the backend. This replaces the narrow `imageRotated` event with a comprehensive system that handles all image mutations (rotation, rescan changes, future edit operations).

## Problem Statement

When an image is modified, there's a cascade of potential changes:
- Image metadata (checksum, dimensions, size)
- Image thumbnails (regenerated on disk)
- Faces detected on that image (some removed, some added, bounding boxes changed)
- Face thumbnails (regenerated on disk)
- People (face counts changed, preferred faces gone, people deleted if no faces remain)

Currently:
- `imageRotated` is a narrow event only handled by Gallery for image thumbnail cache-busting
- Faces screen doesn't listen to any image modification events
- No face thumbnail cache-busting mechanism exists
- Rescan doesn't notify frontend when existing images are re-processed

## Design

### Canonical Flow for Image Modifications

All image modifications follow the same pattern:

```
Frontend                          Backend                         Frontend (later)
   |                                 |                                 |
   |  POST /api/images/rotate        |                                 |
   |  (or rescan triggers internally)|                                 |
   |-------------------------------->|                                 |
   |                                 |  1. Modify image file(s)        |
   |                                 |  2. Update DB metadata          |
   |                                 |  3. Regenerate image thumbnails |
   |                                 |  4. Update face bboxes in DB    |
   |                                 |  5. Regenerate face thumbnails  |
   |                                 |  6. Emit 'images_modified'      |
   |                                 |                                 |
   |  200 OK                         |                                 |
   |<--------------------------------|                                 |
   |                                 |                                 |
   |                                 |     GET /api/events (polling)   |
   |                                 |<--------------------------------|
   |                                 |     [{type: 'images_modified',  |
   |                                 |       image_ids: [...]}]        |
   |                                 |-------------------------------->|
   |                                 |                                 |
   |                                 |     Cache bust + refresh        |
   |                                 |     UI updates via subscriptions|
```

The frontend never emits its own events for image modifications. The backend is the single source of truth.

### Backend Event: `images_modified`

**Event data:**
```python
{
    'image_ids': ['abc123', 'def456', ...]
}
```

No `reason` field - the frontend treats all image-related data as potentially invalid and refreshes as needed.

**When emitted:**
- After `rotate_images()` completes - this is always a batch operation, even for single images
- At the end of a rescan/indexing pass, if any existing images were re-processed
- Any future image mutation operations (crop, color adjustment, etc.)

**Timing:** Event is emitted only after all disk operations complete (image files modified, image thumbnails regenerated, face thumbnails regenerated). This ensures the frontend fetches fresh data, not stale cached versions.

**Batching:** All image mutations are batch operations. `rotate_images()` already accepts a list of IDs. Rescan accumulates modified image IDs during processing. Single event emitted at the end with all affected IDs.

**Important:** The frontend does NOT emit its own events for rotation. It calls the backend, the backend does the work, the backend emits `images_modified`, the frontend receives it via polling and refreshes. This is the single canonical path for all image modifications.

### Frontend Event Handler

In `AppState.events`, handle `images_modified`:

```javascript
case 'images_modified':
    handleImagesModified(data.image_ids);
    break;
```

Handler orchestrates the cascade:
1. Cache-bust image thumbnails
2. Cache-bust face thumbnails for affected images
3. Invalidate/refresh image metadata in AppState
4. Invalidate/refresh faces for affected images (cascades to people)

### Face Thumbnail Cache-Busting

New module or addition to existing code to track face thumbnail cache-bust timestamps:

```javascript
const FaceThumbnails = {
    _cacheBust: new Map(),  // faceId -> timestamp

    bustCache(faceId) { ... },
    bustCacheForImages(imageIds) { ... },
    getUrl(faceId) { ... }
};
```

All face thumbnail URLs go through `FaceThumbnails.getUrl()` to append cache-bust parameter when needed.

### GUI Component Reactions

Components react via existing AppState subscriptions:
- **Gallery**: `AppState.images.onChanged()` triggers re-render, thumbnails already cache-busted
- **Faces screen**: `AppState.faces.onChanged()` triggers re-render, face thumbnails already cache-busted
- **Fullscreen**: If viewing modified image, subscription triggers reload
- **Duplicates**: May need refresh if affected image was in a group

---

## Implementation Tasks

### Phase 1: Backend Event Emission

#### 1.1 Add event emission to rotation
**File:** `imagedb.py`

`rotate_images()` is already a batch operation - it accepts a list of image IDs and processes them (potentially in parallel). After all rotations complete (image files rotated, thumbnails regenerated, face bboxes updated, face thumbnails regenerated), emit the event:

```python
if rotated:
    self.event_queue.emit('images_modified', {'image_ids': rotated})
```

This single event covers all rotated images in the batch. The frontend will receive it via polling and refresh everything related to those images.

#### 1.2 Add event emission to rescan/indexing
**File:** `imagedb.py`

Track modified image IDs during `_process_image()` when an existing image is re-processed (checksum changed). At the end of an indexing pass, emit batched event.

Need to identify where "end of indexing pass" is - likely in `_indexing_loop()` or similar. May need a collection to accumulate modified IDs across the pass.

#### 1.3 Define event constant
**File:** `imagedb.py`

```python
EVENT_IMAGES_MODIFIED = 'images_modified'
```

---

### Phase 2: Face Thumbnail Cache-Busting

#### 2.1 Create FaceThumbnails utility
**File:** `static/faceThumbnails.js` (new file)

```javascript
'use strict';

/**
 * Face Thumbnail URL Manager
 *
 * Manages cache-busting for face thumbnail URLs. When images are modified,
 * face thumbnails are regenerated on the backend. This utility ensures
 * the frontend fetches fresh versions instead of cached ones.
 */
const FaceThumbnails = {
    /**
     * Cache-bust timestamps. Map of faceId -> timestamp.
     * @type {Map<string, number>}
     */
    _cacheBust: new Map(),

    /**
     * Mark a face thumbnail as needing cache-bust.
     * @param {string} faceId
     */
    bustCache(faceId) {
        if (!faceId) return;
        this._cacheBust.set(faceId, Date.now());
    },

    /**
     * Mark all face thumbnails for given images as needing cache-bust.
     * Called when images are modified (rotation, rescan, etc.)
     * @param {string[]} imageIds
     */
    bustCacheForImages(imageIds) {
        for (const imageId of imageIds) {
            const faces = AppState.faces.getForImage(imageId);
            for (const face of faces) {
                this.bustCache(face.id);
            }
        }
    },

    /**
     * Get URL for a face thumbnail with cache-bust parameter if needed.
     * @param {string} faceId
     * @returns {string}
     */
    getUrl(faceId) {
        const ts = this._cacheBust.get(faceId);
        const base = `/api/faces/${faceId}/thumbnail`;
        return ts ? `${base}?t=${ts}` : base;
    },

    /**
     * Clear all cache-bust entries.
     * Called on full page reload or when cache is known fresh.
     */
    clear() {
        this._cacheBust.clear();
    }
};
```

#### 2.2 Include in index.html
**File:** `static/index.html`

Add script tag after `appstate/index.js` and before `faces.js`:
```html
<script src="faceThumbnails.js"></script>
```

#### 2.3 Update face thumbnail URL usage
**File:** `static/faces.js`

Find all places where face thumbnail URLs are constructed and replace with `FaceThumbnails.getUrl(faceId)`.

Search for patterns like:
- `/api/faces/${faceId}/thumbnail`
- `/api/faces/' + faceId + '/thumbnail`
- `App.faceThumbnailUrl(faceId)` if such a helper exists

---

### Phase 3: Frontend Event Handling

#### 3.1 Add event handler in AppState.events
**File:** `static/appstate/events.js`

```javascript
case 'images_modified':
    handleImagesModified(data.image_ids);
    break;
```

```javascript
/**
 * Handle images_modified event.
 *
 * Called when backend has modified one or more images (rotation, rescan, etc.)
 * Orchestrates cache invalidation and data refresh across all affected domains.
 *
 * @param {string[]} imageIds - IDs of modified images
 */
function handleImagesModified(imageIds) {
    if (!imageIds?.length) return;

    console.log('[AppState.events] Images modified:', imageIds.length, 'images');

    // 1. Cache-bust image thumbnails
    for (const imageId of imageIds) {
        ThumbnailLoader.bustCache(imageId);
    }

    // 2. Cache-bust face thumbnails for these images
    FaceThumbnails.bustCacheForImages(imageIds);

    // 3. Refresh image metadata
    AppState.images.refreshByIds(imageIds);

    // 4. Refresh faces for these images (cascades to people)
    AppState.faces.refreshForImages(imageIds);
}
```

#### 3.2 Add refreshByIds to AppState.images
**File:** `static/appstate/images.js`

```javascript
/**
 * Refresh metadata for specific images.
 * Fetches fresh data from backend and updates cache.
 * @param {string[]} ids - Image IDs to refresh
 */
async refreshByIds(ids) {
    if (!ids?.length) return;

    console.log('[AppState.images.refreshByIds]', ids.length, 'images');

    for (const id of ids) {
        try {
            const response = await App.apiGet(`/images/${id}`);
            if (_cache && response.data) {
                _cache.set(id, response.data);
            }
        } catch (err) {
            // Image may have been deleted - remove from cache
            if (err.message?.includes('404')) {
                _cache?.delete(id);
            } else {
                console.warn('[AppState.images.refreshByIds] Failed to refresh:', id, err);
            }
        }
    }

    _markDisplayListDirty();
    markDirty(domainRef);
}
```

#### 3.3 Add refreshForImages to AppState.faces
**File:** `static/appstate/identity.js`

```javascript
/**
 * Refresh faces for specific images.
 * Fetches fresh face data and reconciles people.
 * @param {string[]} imageIds - Image IDs whose faces to refresh
 */
async refreshForImages(imageIds) {
    if (!imageIds?.length) return;

    console.log('[AppState.faces.refreshForImages]', imageIds.length, 'images');

    // Remove old faces for these images from cache
    if (_cache) {
        for (const [faceId, face] of _cache) {
            if (imageIds.includes(face.image_id)) {
                _cache.delete(faceId);
            }
        }
    }
    invalidateDerived();

    // Fetch fresh faces for each image
    for (const imageId of imageIds) {
        try {
            await this.fetchForImage(imageId, { fresh: true });
        } catch (err) {
            console.warn('[AppState.faces.refreshForImages] Failed:', imageId, err);
        }
    }

    // Reconcile all people (face counts, deletions, etc.)
    // Some people may have lost all their faces
    AppState.people.load(true);  // Force reload to get accurate face counts

    markDirty(domainRef);
}
```

---

### Phase 4: Remove Legacy imageRotated Event

The frontend should NOT emit any events for image modifications. The backend is the single source of truth - it does the work, then emits `images_modified`. The frontend receives this via polling and refreshes.

#### 4.1 Remove emission from fullscreen.js
**File:** `static/fullscreen.js`

In `_rotateImage()`, remove:
```javascript
App.emit('imageRotated', imageId);
```

The backend's `images_modified` event will handle cache invalidation and refresh.

**Note:** Keep the local `rotateBoundingBoxes()` call for immediate UI feedback while viewing the rotated image in fullscreen. This is a local optimistic update for the overlay only - the event will trigger the full refresh when it arrives via polling.

#### 4.2 Remove emission from core.js
**File:** `static/core.js`

In `rotateSelected()`, remove the loop that emits `imageRotated` for each image.

#### 4.3 Update Gallery to use AppState subscription
**File:** `static/gallery.js`

Remove:
```javascript
App.on('imageRotated', (imageId) => this._onImageRotated(imageId));
```

The `AppState.images.onChanged()` subscription already exists and will handle re-rendering. The `ThumbnailLoader.bustCache()` is now called by the event handler.

**Verify:** Ensure `_onImagesChanged()` properly triggers grid refresh for modified images.

#### 4.4 Remove _onImageRotated method
**File:** `static/gallery.js`

Remove the `_onImageRotated(imageId)` method entirely - its functionality is now in the event handler.

---

### Phase 5: Testing

#### 5.1 Test rotation flow
1. Open fullscreen on an image
2. Rotate with Ctrl+R
3. Verify: fullscreen image updates immediately (local handling)
4. Verify: gallery thumbnail updates when returning to gallery
5. If in tagging mode, verify face overlays update correctly

#### 5.2 Test rotation with faces
1. Open image with detected faces in tagging mode
2. Rotate
3. Verify: face overlays update position immediately
4. Go to Faces screen
5. Verify: face thumbnails show rotated crops (not cached old versions)

#### 5.3 Test rescan with modified image
1. Externally modify an image file (rotate with external tool)
2. Click "Rescan all folders"
3. Verify: modified image's thumbnail updates
4. Verify: if faces changed, Faces screen reflects changes

#### 5.4 Test batch modifications
1. Select multiple images in gallery
2. Rotate all (Ctrl+R with selection)
3. Verify: all thumbnails update
4. Verify: single `images_modified` event received (check console)

---

## File Change Summary

| File | Changes |
|------|---------|
| `imagedb.py` | Add `images_modified` event emission to rotation and rescan |
| `static/faceThumbnails.js` | New file - face thumbnail URL manager |
| `static/index.html` | Include faceThumbnails.js |
| `static/faces.js` | Use `FaceThumbnails.getUrl()` for all face thumbnail URLs |
| `static/appstate/events.js` | Handle `images_modified` event |
| `static/appstate/images.js` | Add `refreshByIds()` method |
| `static/appstate/identity.js` | Add `refreshForImages()` method |
| `static/fullscreen.js` | Remove `App.emit('imageRotated')` |
| `static/core.js` | Remove `imageRotated` emission from `rotateSelected()` |
| `static/gallery.js` | Remove `imageRotated` listener and handler |

---

## Future Considerations

1. **Duplicates refresh:** If an image's perceptual hash changed, duplicate groups may be affected. May need `AppState.duplicates.invalidateForImages(imageIds)`.

2. **Fullscreen auto-reload:** If viewing a modified image in fullscreen when event arrives, could auto-reload. Currently handled by local rotation code, but rescan-triggered changes wouldn't show until navigation.

3. **Progress indication:** For long rescans, could emit periodic `images_modified` events rather than one huge batch at the end, to give progressive updates.

4. **Optimistic updates:** Rotation currently does local bbox rotation for immediate feedback. This pattern could extend to other operations if needed.
