# Faces Screen Refresh Architecture

## Overview

Fix the fragmented refresh handling on the Faces screen by:
1. Backend push notification when reassessment completes (no polling)
2. Three distinct refresh handlers with proper state preservation
3. Mutual exclusivity between normal mode and preferred-picker mode
4. Selection pruning when items are removed by backend updates

---

## Part 1: Backend Push Notification

### Current Problem
- Backend runs `reassess_unknown_faces_async()` in a thread
- Frontend has no way to know when it completes except polling `/api/faces/reassess-status`
- Polling is wasteful and introduces latency

### Solution: Status Endpoint with Completion Flag

Extend the existing `/api/status` endpoint (already polled by database.js for indexing progress) to include face reassessment state:

```python
# In app.py get_status():
status = get_reassessment_status()
return {
    # ... existing fields ...
    'face_reassessment': {
        'in_progress': status['in_progress'],
        'completed': status['last_result'] is not None and not status['in_progress'],
        'matched_count': status['last_result'].get('matched_count') if status['last_result'] else None,
        'person_id': status['last_result'].get('person_id') if status['last_result'] else None,
    }
}
```

Then in `AppState.status` (per the migration plan):
```javascript
// When status changes and face_reassessment.completed is true:
if (newStatus.face_reassessment?.completed && !_lastStatus?.face_reassessment?.completed) {
    // Clear the completed flag on backend
    await App.apiPost('/faces/reassess-ack');
    // Trigger faces reload
    AppState.faces.load();
}
```

This piggybacks on existing status polling rather than adding a separate poll.

---

## Part 2: Three Refresh Handlers

### Architecture

```javascript
const FacesRefresh = {
    // Which handler set is active
    _activeMode: 'normal',  // 'normal' | 'pick-preferred'

    // --- State Capture (shared utilities) ---

    captureInputState(gridContainer) {
        // Find any active input in this grid
        const activeInput = gridContainer.querySelector('input:focus, textarea:focus');
        if (!activeInput) return null;

        const faceCard = activeInput.closest('[data-face-id]');
        if (!faceCard) return null;

        return {
            faceId: faceCard.dataset.faceId,
            inputSelector: activeInput.tagName.toLowerCase() +
                (activeInput.className ? '.' + activeInput.className.split(' ').join('.') : ''),
            value: activeInput.value,
            selectionStart: activeInput.selectionStart,
            selectionEnd: activeInput.selectionEnd,
            scrollTop: gridContainer.scrollTop,
        };
    },

    restoreInputState(gridContainer, state) {
        if (!state) return;

        // First restore scroll position
        gridContainer.scrollTop = state.scrollTop;

        // Find the face card (if it still exists)
        const faceCard = gridContainer.querySelector(`[data-face-id="${state.faceId}"]`);
        if (!faceCard) return;  // Face was removed by reassessment

        // Find the input
        const input = faceCard.querySelector(state.inputSelector);
        if (!input) return;

        // Restore value and selection
        input.value = state.value;
        input.focus();
        if (input.setSelectionRange) {
            input.setSelectionRange(state.selectionStart, state.selectionEnd);
        }

        // Ensure visible (VirtualGrid may have scrolled it out)
        faceCard.scrollIntoView({ block: 'nearest' });
    },

    // --- Handler Definitions ---

    people: {
        container: null,
        virtualGrid: null,
        selection: null,

        bind(container, virtualGrid, selection) {
            this.container = container;
            this.virtualGrid = virtualGrid;
            this.selection = selection;
        },

        refresh() {
            if (FacesRefresh._activeMode !== 'normal') return;

            // Capture state before refresh
            const scrollOffset = this.virtualGrid?.getScrollOffset();

            // Re-render grid (people grid doesn't have face-level inputs)
            renderPeopleGrid();

            // Restore scroll
            if (scrollOffset != null) {
                this.virtualGrid?.setScrollOffset(scrollOffset);
            }
        },
    },

    unknown: {
        container: null,
        virtualGrid: null,
        selection: null,
        searchText: '',

        bind(container, virtualGrid, selection) {
            this.container = container;
            this.virtualGrid = virtualGrid;
            this.selection = selection;
        },

        refresh() {
            if (FacesRefresh._activeMode !== 'normal') return;

            // Capture state
            const inputState = FacesRefresh.captureInputState(this.container);
            const scrollOffset = this.virtualGrid?.getScrollOffset();

            // Prune selection to only IDs that still exist
            if (this.selection) {
                this.selection.pruneToValidIds();
            }

            // Re-render grid
            renderUnknownGrid();

            // Restore state
            if (scrollOffset != null) {
                this.virtualGrid?.setScrollOffset(scrollOffset);
            }
            FacesRefresh.restoreInputState(this.container, inputState);
        },

        setSearchText(text) {
            this.searchText = text;
        },

        getSearchText() {
            return this.searchText;
        },
    },

    picker: {
        container: null,
        virtualGrid: null,
        selection: null,
        personId: null,
        personName: null,

        bind(container, virtualGrid, selection, personId, personName) {
            this.container = container;
            this.virtualGrid = virtualGrid;
            this.selection = selection;
            this.personId = personId;
            this.personName = personName;
        },

        refresh() {
            if (FacesRefresh._activeMode !== 'pick-preferred') return;

            // Capture input state (user may be typing a name on a face card)
            const inputState = FacesRefresh.captureInputState(this.container);
            const scrollOffset = this.virtualGrid?.getScrollOffset();

            // Prune selection to only IDs that still exist
            if (this.selection) {
                this.selection.pruneToValidIds();
            }

            // Re-render picker grid
            renderPickerGrid(this.personId);

            // Restore state
            if (scrollOffset != null) {
                this.virtualGrid?.setScrollOffset(scrollOffset);
            }
            FacesRefresh.restoreInputState(this.container, inputState);
        },
    },

    // --- Mode Switching ---

    enterPickerMode(personId, personName) {
        this._activeMode = 'pick-preferred';
        this.picker.personId = personId;
        this.picker.personName = personName;
        this.picker.refresh();
    },

    exitPickerMode() {
        this._activeMode = 'normal';
        this.picker.personId = null;
        this.picker.personName = null;
        // Refresh both normal grids
        this.people.refresh();
        this.unknown.refresh();
    },

    // --- AppState Integration ---

    onFacesChanged(event) {
        // Called by AppState.faces subscription
        if (this._activeMode === 'normal') {
            this.people.refresh();
            this.unknown.refresh();
        } else {
            this.picker.refresh();
        }
    },

    onPeopleChanged(event) {
        // Called by AppState.people subscription
        // People grid needs refresh when:
        // - Person added/removed
        // - Person renamed
        // - Preferred face changed
        if (this._activeMode === 'normal') {
            this.people.refresh();
        }
        // Note: picker mode header shows person name, but that's updated
        // separately via pickPreferredPersonName local variable
    },
};
```

---

## Part 3: Selection Pruning

### Current Problem

`GridSelection._selected` is a Set of IDs that persists across renders. When backend reassessment matches unknown faces to a person, those faces disappear from the unknown grid but their IDs remain in `_selected`. This causes:
- Stale selection count in UI
- Potential errors when operating on non-existent faces
- Visual gaps if selection state is used for styling

### Solution: Add `pruneToValidIds()` to GridSelection

```javascript
// In thumbnails.js GridSelection:

/**
 * Removes IDs from selection that no longer exist in the data.
 * Call this after data changes that may remove items.
 */
pruneToValidIds() {
    const items = this._config.getItems();
    const getItemId = this._config.getItemId;

    // Build set of valid IDs
    const validIds = new Set();
    for (const item of items) {
        validIds.add(String(getItemId(item)));
    }

    // Remove any selected IDs that are no longer valid
    let changed = false;
    for (const id of this._selected) {
        if (!validIds.has(id)) {
            this._selected.delete(id);
            changed = true;
        }
    }

    // Update anchor if it was pruned
    if (this._anchor && !validIds.has(this._anchor)) {
        this._anchor = null;
    }

    if (changed) {
        this.updateVisualState();
        this._notifySelectionChanged();
    }
}
```

---

## Part 4: VirtualGrid State Preservation

### Add Methods to VirtualGrid

```javascript
// In thumbnails.js VirtualGrid:

getScrollOffset() {
    return this._config.container?.scrollTop ?? 0;
},

setScrollOffset(offset) {
    if (this._config.container) {
        this._config.container.scrollTop = offset;
    }
},
```

---

## Part 5: Integration Points

### 5.1 AppState Subscriptions

```javascript
// In faces.js onEnter():
this._unsubFaces = AppState.faces.onChanged((event) => {
    FacesRefresh.onFacesChanged(event);
});

this._unsubPeople = AppState.people.onChanged((event) => {
    FacesRefresh.onPeopleChanged(event);
});
```

### 5.2 Preferred-Picker Entry/Exit

```javascript
// When entering pick-preferred mode:
FacesRefresh.enterPickerMode(personId, personName);

// When exiting (back button, escape, etc.):
FacesRefresh.exitPickerMode();
```

### 5.3 Unknown Faces Search

```javascript
// When search text changes:
FacesRefresh.unknown.setSearchText(searchInput.value);
// Existing search logic runs...

// On refresh, restore search text:
const searchInput = document.getElementById('unknown-faces-search');
if (searchInput) {
    searchInput.value = FacesRefresh.unknown.getSearchText();
}
```

### 5.4 Rename Modal (No Changes Needed)

The rename functionality uses `App.prompt()` which creates a modal dialog (`#dialog-prompt`) that is:
- Separate from the grid DOM
- Not affected by grid re-renders
- Uses a Promise that resolves when user confirms/cancels

A refresh triggered while the modal is open will not affect the modal. The user can continue typing and submit. The modal's confirm action will then call `AppState.people.rename()` which will trigger another people change event - but by then the modal is closed.

**Verified**: This "just works" - no special handling needed.

---

## Part 6: Backend Acknowledgment Endpoint

Add endpoint to clear the "completed" flag after frontend processes it:

```python
@app.route('/api/faces/reassess-ack', methods=['POST'])
def ack_reassessment():
    """Acknowledge reassessment completion, clearing the result."""
    global _reassess_result
    with _reassess_lock:
        _reassess_result = None
    return success_response({})
```

This prevents stale "completed" status on next status poll.

---

## Files to Modify

| File | Changes |
|------|---------|
| `faces.py` | Add `_reassess_lock` guard for clearing result |
| `app.py` | Add reassessment status to `/api/status`, add `/api/faces/reassess-ack` |
| `appstate.js` | Add status domain with face_reassessment handling |
| `thumbnails.js` | Add `getScrollOffset()`, `setScrollOffset()` to VirtualGrid; add `pruneToValidIds()` to GridSelection |
| `faces.js` | Replace fragmented refresh logic with `FacesRefresh` pattern; add AppState.people subscription |

---

## Testing Checklist

### Normal Mode
1. [ ] Scroll people grid, trigger reassessment, scroll position preserved
2. [ ] Scroll unknown grid, trigger reassessment, scroll position preserved
3. [ ] Type in unknown face label, reassessment completes, input preserved with cursor position
4. [ ] Search unknown faces, reassessment completes, search text preserved
5. [ ] Select multiple unknown faces, some get matched by reassessment, selection shrinks correctly (no gaps, no stale IDs)

### Pick-Preferred Mode
6. [ ] Active during reassessment, stays in picker mode (doesn't flip to normal)
7. [ ] Type in face label in picker, reassessment completes, input preserved
8. [ ] Select multiple faces in picker, one gets unassigned by another user/process, selection shrinks correctly
9. [ ] Exit to normal mode, both people and unknown grids refresh correctly

### People Grid Triggers
10. [ ] Set preferred face for a person, people grid thumbnail updates
11. [ ] Add new person (by identifying an unknown face), people grid shows new person
12. [ ] Delete last face from person, people grid removes that person
13. [ ] Rename person via picker modal, people grid shows new name (after exiting picker)

### Modal Safety
14. [ ] Open rename modal in picker, trigger reassessment, modal unaffected, can still type and submit

### Backend Integration
15. [ ] Status polling includes `face_reassessment` field
16. [ ] Completion flag triggers faces reload
17. [ ] Ack endpoint clears completion flag
