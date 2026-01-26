# Thumbnail Loading Redesign

## Problems with Current System

1. **Lazy generation** - Thumbnails created on first view, not during indexing. First-time browsing is extremely slow as each thumbnail must be generated from the original image.

2. **No frontend cache** - Blob URLs are discarded when elements leave the DOM (virtual scroll retain zone). Scrolling back requires re-fetching the same thumbnails.

3. **HTTP round-trip per thumbnail** - Even with browser cache (304 responses), each thumbnail requires a network round-trip.

4. **Backend overhead per request** - Every request requires: SQLite query (to get checksum from image ID) → filesystem stat → file read → HTTP response.

5. **Disk I/O contention** - 6 concurrent requests hammering storage with parallel reads.

6. **Overcomplicated priority system** - Three priority lanes (visible/buffer/background), sequence numbers for LIFO ordering, constant re-sorting, pruning logic, and re-request logic for pruned items. All to work around the fundamental inefficiency.

---

## New Design

### Core Principles

- **Simplicity**: No priority lanes, no sequence numbers, no listener sets
- **Real-time prioritization**: Sort/prune based on current scroll position when a fetch slot opens
- **One image = one grid square**: No need to track multiple listeners per thumbnail
- **Fail-safe**: Timeouts prevent stuck requests from jamming the pipeline
- **Deferred DOM creation**: DOM elements only created AFTER thumbnail blob URL is ready
- **Unified buffer zone**: Single zone (visible + extraRows) for both DOM and fetching

### Queue Structure

```javascript
_queue: [
    { imageId, index, onReady },
    ...
]
```

- `imageId`: For deduplication and URL construction
- `index`: Item's position in the data array (row computed on-the-fly)
- `onReady`: Callback function called with blob URL when fetch completes

### In-Flight Tracking

```javascript
_inFlight: Map<imageId, { controller: AbortController, index: number }>
```

Stores the abort controller (for timeout/scroll-abort) and index (to compute row for pruning).

### _processQueue Logic

Called when:
- Items added to queue (via `request()`)
- Any fetch ends (via `finally` block - covers success, error, timeout, abort)

```javascript
_processQueue() {
    const { itemsPerRow, visibleStartRow, visibleEndRow } = this._scrollState;
    const bufferStart = visibleStartRow - this._config.extraRows;
    const bufferEnd = visibleEndRow + this._config.extraRows;

    // Prune items outside buffer zone
    this._queue = this._queue.filter(item => {
        const row = Math.floor(item.index / itemsPerRow);
        return row >= bufferStart && row <= bufferEnd;
    });

    // Sort: visible rows first, then by distance from visible range
    this._queue.sort((a, b) => {
        const rowA = Math.floor(a.index / itemsPerRow);
        const rowB = Math.floor(b.index / itemsPerRow);
        const inVisibleA = rowA >= visibleStartRow && rowA <= visibleEndRow;
        const inVisibleB = rowB >= visibleStartRow && rowB <= visibleEndRow;

        if (inVisibleA && !inVisibleB) return -1;
        if (inVisibleB && !inVisibleA) return 1;

        // Both in same zone - sort by row
        return rowA - rowB;
    });

    // Fill available slots
    while (this._activeCount < this._config.maxConcurrent && this._queue.length > 0) {
        const item = this._queue.shift();
        this._loadThumbnail(item);
    }
}
```

### Scroll Handler

Throttled to a few times per second (configurable):

```javascript
_onScroll(scrollTop) {
    const now = Date.now();
    if (now - this._lastScrollProcess < this._config.scrollThrottleMs) return;
    this._lastScrollProcess = now;

    // Update scroll state
    this._updateScrollState(scrollTop);

    // Abort in-flight requests now outside buffer zone
    this._pruneInFlight();

    // Update visible items (adds new items to queue)
    this._updateVisibleItems(scrollTop);
}

_pruneInFlight() {
    const { itemsPerRow, visibleStartRow, visibleEndRow } = this._scrollState;
    const bufferStart = visibleStartRow - this._config.extraRows;
    const bufferEnd = visibleEndRow + this._config.extraRows;

    for (const [imageId, { controller, index }] of this._inFlight) {
        const row = Math.floor(index / itemsPerRow);
        if (row < bufferStart || row > bufferEnd) {
            controller.abort();
            // Don't delete here - finally block handles cleanup
        }
    }
}
```

### Fetch with Timeout

```javascript
async _loadThumbnail({ imageId, index, onReady }) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this._config.timeoutMs);

    this._inFlight.set(imageId, { controller, index });
    this._activeCount++;

    try {
        const response = await fetch(this._getThumbnailUrl(imageId), {
            signal: controller.signal
        });
        clearTimeout(timeoutId);

        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const blob = await response.blob();
        const blobUrl = URL.createObjectURL(blob);

        // Check if still in buffer zone before calling callback
        if (this._isInBuffer(this._getRow(index))) {
            onReady(blobUrl);
        } else {
            URL.revokeObjectURL(blobUrl);
        }
    } catch (error) {
        if (error.name !== 'AbortError') {
            console.error(`Thumbnail load failed for ${imageId}:`, error);
        }
        // Abort errors are expected (timeout or scroll-away)
    } finally {
        clearTimeout(timeoutId);
        this._inFlight.delete(imageId);
        this._activeCount--;
        this._processQueue();
    }
}
```

### Request Method

```javascript
request(imageId, index, onReady) {
    if (!imageId || !onReady) return;

    // Already fetching?
    if (this._inFlight.has(imageId)) return;

    // Already queued?
    if (this._queue.some(item => item.imageId === imageId)) return;

    this._queue.push({ imageId, index, onReady });
    this._processQueue();
}
```

### VirtualGrid Integration

VirtualGrid now uses a single buffer zone (visible + extraRows) and defers DOM creation:

```javascript
// In VirtualGrid._updateVisibleItems():
for (let i = bufferStart; i < bufferEnd; i++) {
    const item = items[i];
    const id = config.getItemId(item);

    // Skip if already rendered or pending
    if (state.renderedItems.has(id) || state.pendingItems.has(id)) continue;

    const thumbId = config.getThumbnailId(item);
    state.pendingItems.add(id);

    // Request thumbnail with callback that creates DOM element
    ThumbnailLoader.request(thumbId, i, (blobUrl) => {
        state.pendingItems.delete(id);

        // Verify still in buffer zone
        if (!inBufferZone(id)) {
            URL.revokeObjectURL(blobUrl);
            return;
        }

        // Create DOM element with blob URL already set
        const el = config.createItem(item, index, blobUrl);
        state.renderedItems.set(id, el);
        insertAtPosition(el, index);
    });
}

// Remove elements outside buffer zone
for (const [id, el] of state.renderedItems) {
    if (!bufferIds.has(id)) {
        el.remove();
        state.renderedItems.delete(id);
    }
}
```

---

## Configuration

Add to YAML config template:

```yaml
# Thumbnail loading configuration
thumbnails:
  # Maximum concurrent fetch requests
  concurrent_requests: 6

  # Extra rows above/below viewport to prefetch
  extra_rows: 5

  # Timeout for thumbnail fetch requests (milliseconds)
  timeout_ms: 10000

  # Scroll event throttle (milliseconds)
  scroll_throttle_ms: 250
```

These values need to be passed from backend to frontend (via an API endpoint or embedded in page).

---

## Future Improvements (Not in This Phase)

1. **Pre-generate thumbnails during indexing** - Generate thumbnails in `_process_image()` when images are first scanned.

2. **Frontend thumbnail cache** - Keep blob URLs in a Map, don't discard when elements leave DOM.

3. **Batch thumbnail requests** - Single HTTP request for multiple thumbnails.

4. **Embed thumbnails in image list** - Return small thumbnails as base64 in the image list API response.

---

## Execution Checklist

### Phase 1: Configuration

- [x] Add thumbnail config section to `config.py` and YAML template
- [x] Add API endpoint to expose config to frontend
- [x] Create frontend config accessor in `core.js`

### Phase 2: Rewrite ThumbnailLoader

- [x] Remove old ThumbnailLoader code from `thumbnails.js`
- [x] Implement new ThumbnailLoader with:
  - [x] Simple queue structure `{ imageId, index, imgElement }`
  - [x] `_inFlight` Map with `{ controller, index }`
  - [x] `_scrollState` tracking
  - [x] `request(imageId, index, imgElement)` method
  - [x] `_processQueue()` with prune/sort/fill logic
  - [x] `_loadThumbnail()` with timeout
  - [x] `_pruneInFlight()` for scroll-abort
  - [x] `clear()` for screen transitions

### Phase 3: Update VirtualGrid

- [x] Change scroll handler from RAF to timestamp throttle
- [x] Update `onItemVisible` callback signature to include index
- [x] Call ThumbnailLoader's scroll state update method
- [x] Remove `prioritize()` calls (no longer needed)

### Phase 4: Update Gallery

- [x] Update VirtualGrid config for deferred DOM creation
- [x] `createItem` now receives (item, index, blobUrl) and sets img.src immediately
- [x] Element created with 'loaded' class already applied
- [x] Remove bufferRows/retainRows config (now uses unified extraRows)
- [x] Update `_onImageRotated` to remove element and trigger refresh
- [ ] Test scroll performance with large image set

### Phase 5: Update Duplicates

- [x] Update VirtualGrid config for deferred DOM creation
- [x] `createItem` now receives (group, index, blobUrl) and sets img.src immediately
- [x] Element created with 'loaded' class already applied

### Phase 6: Cleanup

- [x] Remove dead code:
  - [x] Priority constants (removed in Phase 2)
  - [x] Sequence counter (removed in Phase 2)
  - [x] `prioritize()` method (removed in Phase 2)
  - [x] `flush()` method (removed in Phase 2)
  - [x] `cancel()` method (removed in Phase 2)
  - [x] Listeners Set handling (removed in Phase 2)
  - [x] RAF cleanup in destroy() (removed)
  - [x] VirtualGrid bufferRows/retainRows config options
  - [x] VirtualGrid onItemVisible/onItemRemoved callbacks
  - [x] Gallery _loadRetainRows() function
- [x] Update JSDoc comments (file header updated)
- [x] Unified architecture: DOM elements only created after thumbnail ready
- [ ] Test edge cases:
  - [ ] Rapid scrolling
  - [ ] Window resize
  - [ ] Backend timeout
  - [ ] Empty database
  - [ ] Screen transitions

### Phase 7: Testing

- [ ] Test with small image set (~100 images)
- [ ] Test with large image set (~60,000 images)
- [ ] Test scroll performance
- [ ] Test timeout behavior (simulate slow backend)
- [ ] Test screen transitions (Gallery ↔ Duplicates ↔ Fullscreen)
- [ ] Verify no memory leaks (blob URLs cleaned up)
