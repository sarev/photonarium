# Thumbnail Loading Refactor Plan

## Overview

Create a new `thumbnails.js` module that consolidates:
1. **ThumbnailLoader**: Centralized, priority-based thumbnail loading with LIFO queue
2. **VirtualGrid**: Reusable virtual scrolling infrastructure
3. **GridSelection**: Unified selection handling with keyboard navigation and drag-box

Both Gallery and Duplicates will become thinner, delegating common functionality to this shared module. Duplicates will gain selection support for future features.

---

## 1. ThumbnailLoader

### Responsibilities

- Maintain a LIFO priority queue of pending thumbnail requests
- Deduplicate requests: if the same `imageId` is requested multiple times, track all interested `<img>` elements and serve them from one fetch
- Limit concurrent requests (~4-6)
- Support cancellation via `AbortController`
- Re-prioritize queue on scroll updates
- Handle cache-busting for rotated images

### API

```javascript
ThumbnailLoader.request(imageId, imgElement, priority)
// Registers interest in a thumbnail. If already queued/in-flight, adds element to listeners.
// Priority: 'visible' | 'buffer' | 'background'

ThumbnailLoader.cancel(imageId, imgElement)
// Removes element's interest. If no listeners remain, cancels the request.

ThumbnailLoader.prioritize(visibleIds, bufferIds)
// Called on scroll: reorders queue so visibleIds are at front, bufferIds next.
// Items not in either list get demoted to background.

ThumbnailLoader.bustCache(imageId)
// Marks imageId as needing cache-bust (e.g., after rotation).
```

### Internal Structure

```javascript
{
  _queue: [],              // Array of {imageId, priority, listeners: Set<imgElement>}
  _inFlight: Map(),        // imageId -> {controller: AbortController, listeners: Set<imgElement>}
  _cacheBust: Map(),       // imageId -> timestamp (for rotated images)
  _maxConcurrent: 6,
  _activeCount: 0
}
```

### Deduplication Logic

- When `request(imageId, el)` is called:
  - If `imageId` is already in queue: add `el` to its listeners set, update priority if higher
  - If `imageId` is in-flight: add `el` to its listeners set (will receive the blob when ready)
  - Otherwise: create new queue entry
- When fetch completes: iterate all listeners and set their `src` to the blob URL
- When `cancel(imageId, el)` is called:
  - Remove `el` from listeners
  - If no listeners remain: remove from queue or abort in-flight request

---

## 2. VirtualGrid

### Responsibilities

- Spacer element management (top/bottom)
- Scroll event handling with RAF throttling
- Dimension calculations (items per row, item height, visible/buffer/retain zones)
- Core render loop: determining which indices need rendering, which need removal
- Calling back to the consumer for item creation/destruction
- Coordinating with GridSelection for selection state on rendered items

### Configuration (passed by consumer)

```javascript
{
  container: HTMLElement,      // Scroll container
  grid: HTMLElement,           // Grid element for items
  getItems: () => Array,       // Returns current data array
  getItemId: (item) => string, // Extracts unique ID from item
  createItem: (item, index) => HTMLElement,  // Creates DOM element
  onItemVisible: (item, element) => void,    // Called when item enters render zone
  onItemRemoved: (id) => void,               // Called when item leaves retain zone
  bufferRows: number,          // Default: 3
  retainRows: number,          // Default: 30
  getItemDimensions: (thumbSize) => {width, height, gap},  // For layout calculation
  itemSelector: string,        // CSS selector for items (e.g., '.gallery-item')
  selection: GridSelection     // Optional: selection manager instance
}
```

### API

```javascript
const grid = VirtualGrid.create(config)

grid.render()              // Full re-render (e.g., after data change)
grid.refresh()             // Recalculate dimensions and update visible items
grid.scrollTo(index)       // Scroll to show item at index
grid.scrollToId(id)        // Scroll to show item with given ID
grid.getVisibleRange()     // Returns {start, end} indices of visible items
grid.getItemsPerRow()      // Returns current items per row (for keyboard nav)
grid.destroy()             // Cleanup listeners
```

### What moves from Gallery/Duplicates

- `_virtualScroll` configuration object
- `_initVirtualScroll()`
- `_topSpacer`, `_bottomSpacer`
- `_attachScrollListener()`, `_detachScrollListener()`
- `_onScroll()` with RAF throttling
- `_calculateVirtualDimensions()`
- `_updateVisibleItems()` core logic
- `_insertItemAtPosition()`
- Resize handler

---

## 3. GridSelection

### Responsibilities

- Track selected item IDs (as a Set)
- Handle all selection interactions:
  - Single click: select one item (clear others)
  - Ctrl/Cmd+click: toggle item in selection
  - Shift+click: range selection from anchor
  - Right-click: toggle without clearing others
  - Long-press (touch): add to selection
- Drag-box selection:
  - Left-drag on empty space: select items in box
  - Right-drag on empty space: toggle items in box
  - Auto-scroll when dragging near container edges
- Keyboard navigation:
  - Arrow keys: move selection
  - Shift+Arrow: extend selection
  - Ctrl/Cmd+A: select all
  - Escape: clear selection
- Maintain selection anchor for shift-click ranges
- Update visual selection state on rendered items
- Coordinate with VirtualGrid for scrolling to selected items

### Configuration

```javascript
{
  grid: VirtualGrid,                    // The virtual grid instance
  getItems: () => Array,                // Returns current data array
  getItemId: (item) => string,          // Extracts unique ID from item
  itemSelector: string,                 // CSS selector for items
  selectedClass: string,                // Class to add to selected items (default: 'selected')
  onSelectionChanged: (ids: string[]) => void,  // Callback when selection changes
  onItemActivated: (id: string) => void,        // Callback for Enter/double-click
  onDeleteRequested: (ids: string[]) => void,   // Callback for Delete key (optional)
  enableKeyboard: boolean,              // Default: true
  enableDragBox: boolean,               // Default: true
  enableLongPress: boolean,             // Default: true
  longPressMs: number                   // Default: 500
}
```

### API

```javascript
const selection = GridSelection.create(config)

selection.select(id)                    // Select single item (clear others)
selection.toggle(id)                    // Toggle item selection
selection.selectRange(fromId, toId)     // Select range inclusive
selection.selectAll()                   // Select all items
selection.clear()                       // Clear selection
selection.getSelected()                 // Returns array of selected IDs
selection.isSelected(id)                // Check if item is selected
selection.setSelected(ids)              // Set selection to specific IDs
selection.updateVisualState()           // Refresh visual state on rendered items
selection.navigateRelative(delta)       // Move selection by delta items
selection.navigateVertical(delta)       // Move selection by delta rows
selection.bind()                        // Attach event listeners
selection.unbind()                      // Detach event listeners
selection.destroy()                     // Full cleanup
```

### Drag-Box Auto-Scroll

During drag-box selection:
- Detect mouse position relative to container edges
- When within ~50px of top/bottom edge, auto-scroll in that direction
- Continue updating drag box as scroll position changes
- Use `setInterval` (~60fps) for smooth scrolling during drag

### Integration with App State

For Gallery, selection syncs with `App.state.selectedImages`:
- `onSelectionChanged` callback updates `App.setSelectedImages()`
- Can also subscribe to `App.on('selectionChanged')` for external changes

For Duplicates, selection can use local state initially:
- `onSelectionChanged` stores in `Duplicates.state.selectedGroups`
- Future: could sync with App state if needed for cross-screen features

---

## 4. Changes to Gallery.js

### Removes

- All virtual scrolling infrastructure (delegates to VirtualGrid)
- Direct `img.src` assignment
- Selection click handlers (`_handleClick`, `_handleRightClick`, etc.)
- Long-press handling (`_handlePointerDown`, `_handlePointerUp`)
- Drag-box selection (`_handleDragStart`, `_handleDragMove`, `_handleDragEnd`)
- Auto-scroll during drag (`_updateAutoScroll`, `_performAutoScroll`, `_stopAutoScroll`)
- Keyboard navigation for selection (`_navigateSelection`, `_navigateSelectionVertical`)
- Selection anchor tracking (`_selectionAnchor`)

### Keeps

- `_createThumbnailItem()` - but modified to NOT set src, just create structure
- Info panel management
- Filter/sort logic
- Scroll overlay (date/rating indicator) - this is Gallery-specific UI
- Image rotation handling (cache bust integration)
- Delete confirmation dialog and API calls

### New integration

```javascript
init() {
  this._grid = VirtualGrid.create({
    container: this._els.grid,
    grid: this._els.grid,
    getItems: () => this.state.filteredImages,
    getItemId: (img) => img.id,
    createItem: (img, index) => this._createThumbnailItem(img),
    onItemVisible: (img, el) => {
      const imgEl = el.querySelector('img');
      ThumbnailLoader.request(img.id, imgEl, 'visible');
    },
    onItemRemoved: (id) => {
      ThumbnailLoader.cancel(id);
    },
    itemSelector: '.gallery-item',
    // ...
  });

  this._selection = GridSelection.create({
    grid: this._grid,
    getItems: () => this.state.filteredImages,
    getItemId: (img) => img.id,
    itemSelector: '.gallery-item',
    onSelectionChanged: (ids) => App.setSelectedImages(ids),
    onItemActivated: (id) => App.showFullscreen(id),
    onDeleteRequested: (ids) => this._deleteImages(ids),
  });

  // Sync external selection changes (e.g., from toolbar buttons)
  App.on('selectionChanged', (ids) => {
    this._selection.setSelected(ids);
    this._updateInfoPanel(ids);
  });
}

onEnter() {
  this._selection.bind();
  // ...
}

onLeave() {
  this._selection.unbind();
  // ...
}
```

---

## 5. Changes to Duplicates.js

### Removes

- All virtual scrolling infrastructure (delegates to VirtualGrid)
- `_loadStackThumbnail()` direct src assignment

### Keeps

- `_createStackElement()` - but modified to NOT set src
- Similarity level handling
- Sort mode (size/semantic)
- Min group size filter

### Adds (via GridSelection)

- Click to select stacks
- Multi-select with Ctrl/Shift
- Drag-box selection
- Keyboard navigation
- Future: bulk operations on selected groups

### New integration

```javascript
init() {
  this._grid = VirtualGrid.create({
    container: this._els.container,
    grid: this._els.grid,
    getItems: () => this.state.groups,
    getItemId: (group) => group.group_hash,
    createItem: (group, index) => this._createStackElement(group, index),
    onItemVisible: (group, el) => {
      const imgEl = el.querySelector('img');
      const imageId = group.best_image?.id;
      if (imageId) {
        ThumbnailLoader.request(imageId, imgEl, 'visible');
      }
    },
    onItemRemoved: (hash) => {
      const group = this.state.groups.find(g => g.group_hash === hash);
      if (group?.best_image?.id) {
        ThumbnailLoader.cancel(group.best_image.id);
      }
    },
    itemSelector: '.duplicate-stack',
    // ...
  });

  this._selection = GridSelection.create({
    grid: this._grid,
    getItems: () => this.state.groups,
    getItemId: (group) => group.group_hash,
    itemSelector: '.duplicate-stack',
    onSelectionChanged: (hashes) => {
      this.state.selectedGroups = hashes;
      this._updateSelectionUI();
    },
    onItemActivated: (hash) => this._openGroupInGallery(hash),
    // No delete handler for now - could add later
  });
}

onEnter() {
  this._selection.bind();
  // ...
}

onLeave() {
  this._selection.unbind();
  // ...
}
```

---

## 6. Script Load Order

Update `index.html`:

```html
<script src="core.js"></script>
<script src="thumbnails.js"></script>  <!-- NEW: ThumbnailLoader, VirtualGrid, GridSelection -->
<script src="gallery.js"></script>
<script src="fullscreen.js"></script>
<script src="database.js"></script>
<script src="search.js"></script>
<script src="duplicates.js"></script>
```

---

## 7. Priority Flow on Scroll

When VirtualGrid processes a scroll:

1. Calculate visible range and buffer range
2. Call `ThumbnailLoader.prioritize(visibleIds, bufferIds)`
3. For new items entering render zone: call `onItemVisible` -> `ThumbnailLoader.request()`
4. For items leaving retain zone: call `onItemRemoved` -> `ThumbnailLoader.cancel()`

ThumbnailLoader then:

1. Reorders queue: visible items first (LIFO within tier), then buffer, then background
2. Processes from front of queue
3. Cancels any in-flight requests for items no longer in any list

---

## 8. Summary of Benefits

1. **LIFO Queue**: Most recent requests (current viewport) processed first
2. **Deduplication**: Same thumbnail requested multiple times shares one fetch
3. **Cancellation**: Stale requests don't waste bandwidth or block fresh ones
4. **Priority Tiers**: Visible > Buffer > Background
5. **Code Deduplication**: Virtual scrolling and selection logic consolidated in one place
6. **Unified Selection**: Both screens get consistent selection behavior
7. **Future-Ready**: Duplicates screen gains selection for future bulk operations
8. **Easier Maintenance**: Gallery and Duplicates become simpler, focused on their unique concerns

---

## 9. Execution Checklist

### Phase 1: Create thumbnails.js with ThumbnailLoader

- [x] Create `thumbnails.js` file with module header/documentation
- [x] Implement `ThumbnailLoader` object:
  - [x] `_queue`, `_inFlight`, `_cacheBust`, `_maxConcurrent`, `_activeCount` state
  - [x] `request(imageId, imgElement, priority)` method
  - [x] `cancel(imageId, imgElement)` method
  - [x] `prioritize(visibleIds, bufferIds)` method
  - [x] `bustCache(imageId)` method
  - [x] `_processQueue()` internal method
  - [x] `_loadThumbnail(item)` internal method with fetch + AbortController
  - [x] Deduplication logic (multiple listeners per imageId)
- [x] Add `thumbnails.js` to `index.html` script load order
- [ ] Write basic tests / manual verification

### Phase 2: Implement VirtualGrid

- [x] Implement `VirtualGrid.create(config)` factory function
- [x] Port spacer element creation from Gallery
- [x] Port scroll handler with RAF throttling
- [x] Port dimension calculation (`_calculateVirtualDimensions`)
- [x] Port `_updateVisibleItems` core logic
- [x] Port `_insertItemAtPosition`
- [x] Port resize handler
- [x] Implement `render()`, `refresh()`, `scrollTo()`, `scrollToId()`, `getVisibleRange()`, `getItemsPerRow()`, `destroy()`
- [x] Add callback hooks: `onItemVisible`, `onItemRemoved`
- [x] Integrate ThumbnailLoader priority calls into scroll handling

### Phase 3: Implement GridSelection

- [ ] Implement `GridSelection.create(config)` factory function
- [ ] Selection state management (Set of IDs, anchor tracking)
- [ ] Click handlers:
  - [ ] Single click (select one)
  - [ ] Ctrl/Cmd+click (toggle)
  - [ ] Shift+click (range)
  - [ ] Right-click (toggle without clear)
- [ ] Long-press handling for touch devices
- [ ] Drag-box selection:
  - [ ] Mouse down on empty space starts drag
  - [ ] Mouse move updates box and calculates intersecting items
  - [ ] Auto-scroll when near edges
  - [ ] Mouse up finalizes selection
  - [ ] Left-drag = select, right-drag = toggle
- [ ] Keyboard navigation:
  - [ ] Arrow keys (left/right/up/down)
  - [ ] Shift+Arrow for extend selection
  - [ ] Ctrl/Cmd+A for select all
  - [ ] Escape for clear
  - [ ] Enter for activate
  - [ ] Delete for delete request
- [ ] `updateVisualState()` to sync DOM with selection state
- [ ] `bind()` and `unbind()` for lifecycle management

### Phase 4: Integrate into Gallery.js

- [ ] Remove old virtual scroll code from Gallery
- [ ] Remove old selection handling code from Gallery
- [ ] Remove old keyboard navigation code from Gallery
- [ ] Create VirtualGrid instance in `init()`
- [ ] Create GridSelection instance in `init()`
- [ ] Update `_createThumbnailItem()` to not set `img.src`
- [ ] Wire up `onItemVisible` to call `ThumbnailLoader.request()`
- [ ] Wire up `onItemRemoved` to call `ThumbnailLoader.cancel()`
- [ ] Wire up `onSelectionChanged` to `App.setSelectedImages()`
- [ ] Wire up `onItemActivated` to `App.showFullscreen()`
- [ ] Wire up `onDeleteRequested` to delete confirmation flow
- [ ] Update `onEnter()` to call `selection.bind()`
- [ ] Update `onLeave()` to call `selection.unbind()`
- [ ] Update `_onImageRotated()` to use `ThumbnailLoader.bustCache()`
- [ ] Preserve scroll overlay (date/rating indicator) functionality
- [ ] Test all Gallery functionality

### Phase 5: Integrate into Duplicates.js

- [ ] Remove old virtual scroll code from Duplicates
- [ ] Create VirtualGrid instance in `init()`
- [ ] Create GridSelection instance in `init()`
- [ ] Update `_createStackElement()` to not set `img.src`
- [ ] Wire up `onItemVisible` to call `ThumbnailLoader.request()`
- [ ] Wire up `onItemRemoved` to call `ThumbnailLoader.cancel()`
- [ ] Wire up `onSelectionChanged` to local state
- [ ] Wire up `onItemActivated` to open group in Gallery
- [ ] Update `onEnter()` to call `selection.bind()`
- [ ] Update `onLeave()` to call `selection.unbind()`
- [ ] Test all Duplicates functionality
- [ ] Verify new selection features work (click, multi-select, drag-box, keyboard)

### Phase 6: Cleanup and Polish

- [ ] Remove any dead code from Gallery.js and Duplicates.js
- [ ] Verify no duplicate functionality remains
- [ ] Test rapid scrolling behavior (LIFO queue working correctly)
- [ ] Test scroll up/down/up pattern (deduplication working)
- [ ] Test drag-box with auto-scroll
- [ ] Test keyboard navigation in both screens
- [ ] Test thumbnail size changes
- [ ] Test window resize
- [ ] Update CLAUDE.md if needed with new architecture notes
