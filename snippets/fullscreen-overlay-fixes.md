# Fullscreen Overlay & Face Tagging Fixes

**Status: IMPLEMENTED**

## Changes Made

### 1. Race Condition Fix (faces.js)

Added `currentOverlayImageId` to track which image's faces are being rendered:

```javascript
let currentOverlayImageId = null;

async function loadFacesForImage(imageId) {
    currentOverlayImageId = imageId;
    const faces = await AppState.faces.fetchForImage(imageId);

    // Skip if we've navigated away during the async call
    if (currentOverlayImageId !== imageId) return;

    renderFaces(faces || [], imageId);
}

function renderFaces(faces, forImageId) {
    // Skip if stale
    if (forImageId && currentOverlayImageId !== forImageId) return;

    // ... also checks after image load event
}
```

### 2. Navigation Context Fix (fullscreen.js, core.js, faces.js)

Updated `Fullscreen.open()` to accept custom image list:

```javascript
open(imageId, options = {}) {
    if (options.imageList && options.imageList.length > 0) {
        this.state.imageList = options.imageList;
        // ...
    } else {
        // Fall back to Gallery's list
    }
}
```

Updated callers in faces.js to pass their context:

```javascript
// Pick-preferred mode
const imageList = pickPreferredFaces.filter(f => f.image_id).map(f => ({ id: f.image_id }));
App.showFullscreen(face.image_id, { imageList });

// Unknown faces mode
const imageList = displayedFaces.filter(f => f.image_id).map(f => ({ id: f.image_id }));
App.showFullscreen(face.image_id, { imageList });
```

### 3. Face Preloading (fullscreen.js)

Added `_preloadAdjacentFaces()` called from `_preloadAdjacent()`:

```javascript
_preloadAdjacentFaces(prevIndex, nextIndex) {
    if (typeof Faces === 'undefined' || !Faces.isTaggingModeActive()) return;

    const { imageList } = this.state;
    if (imageList[prevIndex]) AppState.faces.fetchForImage(imageList[prevIndex].id);
    if (imageList[nextIndex]) AppState.faces.fetchForImage(imageList[nextIndex].id);
}
```

---

## Files Modified

| File | Changes |
|------|---------|
| `faces.js` | Added `currentOverlayImageId`, updated `loadFacesForImage`, `renderFaces`, `clearFaceOverlay`, `handlePickPreferredFaceActivated`, `handleFaceActivated` |
| `fullscreen.js` | Updated `open()` to accept options.imageList, added `_preloadAdjacentFaces()` |
| `core.js` | Updated `showFullscreen()` to pass options through |

---

## Testing Checklist

- [ ] Open fullscreen from Faces screen with face tagging enabled
- [ ] Navigate quickly through 5-10 images with arrow keys
- [ ] Verify face boxes update immediately and match the displayed image
- [ ] Verify prev/next navigates through the Faces context, not Gallery
- [ ] Open fullscreen from pick-preferred mode, verify navigation stays within person's images
