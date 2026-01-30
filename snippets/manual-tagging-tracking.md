# Manual vs Auto Tagging Tracking

## Overview

Track whether faces were manually tagged by the user or auto-assigned by the recognition system. This allows:
- Using only manually-tagged faces as reference for auto-matching (prevents snowball effect)
- Visual distinction in UI (padlock icon for manual tags)
- Ability to toggle manual status in pick-preferred mode
- Threshold changes can prune auto-tagged faces below threshold

---

## Phase 1: Database Schema

### 1.1 Add column to faces table

**File: `faces.py`** (schema definition)

```sql
ALTER TABLE faces ADD COLUMN manually_tagged INTEGER DEFAULT 0;
```

- `0` = auto-tagged (or not yet tagged)
- `1` = manually tagged by user

### 1.2 Migration

**File: `faces.py`** (in `ensure_schema` or separate migration)

```sql
-- Set all existing named faces as manually tagged
UPDATE faces SET manually_tagged = 1 WHERE person_id IS NOT NULL;
```

This "padlocks" all existing named faces as the user requested.

---

## Phase 2: Backend Logic

### 2.1 Update `batch_identify_faces`

**File: `faces.py`**

When faces are identified via API (user action), set `manually_tagged = 1`:

```python
# In the UPDATE query that sets person_id
UPDATE faces SET person_id = ?, manually_tagged = 1 WHERE id = ?
```

### 2.2 Update `reassess_unknown_faces`

**File: `faces.py`**

Only use manually-tagged faces for matching:

```python
# Change query for known_embeddings to filter by manually_tagged
WHERE person_id IS NOT NULL AND suppressed = 0 AND manually_tagged = 1
```

When auto-matching succeeds, set `manually_tagged = 0`:

```python
# In the UPDATE that assigns the face to a person
UPDATE faces SET person_id = ?, manually_tagged = 0 WHERE id = ?
```

### 2.3 Add toggle endpoint

**File: `app.py`**

```python
@app.route('/api/faces/<face_id>/toggle-manual', methods=['POST'])
def toggle_face_manual_tag(face_id):
    """Toggle the manually_tagged flag for a face."""
    # Toggle the flag and return new state
```

### 2.4 Update face data in API responses

**Files: `app.py`, `faces.py`**

Include `manually_tagged` in all face responses:
- `GET /api/faces`
- `GET /api/people/:id/faces`
- `GET /api/images/:id/faces`

### 2.5 Optional: Threshold-based pruning

**File: `faces.py`**

When `update_person` changes `recognition_threshold`, optionally remove auto-tagged faces below new threshold:

```python
def prune_auto_tagged_faces(conn, person_id, threshold):
    """Remove auto-tagged faces that fall below the new threshold."""
    # Get person's manually-tagged face embeddings
    # Compare all auto-tagged faces against them
    # Unassign any below threshold
```

This could be opt-in via API parameter or always-on.

### 2.6 Addition to face name assignment/unassignment

- Setting a face name to a non-empty string (assigning it) sets the manual flag.
- Setting a face name to an empty string (unassigning it) clears the manual flag.

---

## Phase 3: Frontend AppState

### 3.1 Update faces domain

**File: `static/appstate.js`**

- Face objects now include `manually_tagged` boolean
- Add method:

```javascript
toggleManualTag(faceId) {
    return queueTransaction(async () => {
        const face = _cache?.get(faceId);
        const newValue = !face?.manually_tagged;

        // Optimistic update
        if (face) face.manually_tagged = newValue;

        // API call
        await App.apiPost(`/faces/${faceId}/toggle-manual`);

        return newValue;
    });
}
```

### 3.2 Update AppState application logic

- Setting a face name to a non-empty string (assigning it) sets the manual flag.
- Setting a face name to an empty string (unassigning it) clears the manual flag.

---

## Phase 4: Frontend GUI

### 4.1 Pick-preferred face cards

**File: `static/faces.js`** - `createPickPreferredFaceCard`

Add padlock overlay (similar to star for preferred):

```javascript
// Add padlock overlay for manually-tagged faces
if (face.manually_tagged) {
    const padlock = document.createElement('div');
    padlock.className = 'face-card-padlock';
    padlock.innerHTML = '<span class="material-symbols-outlined">lock</span>';
    padlock.title = 'Manually tagged - used for recognition';
    card.appendChild(padlock);
} else {
    const unlocked = document.createElement('div');
    unlocked.className = 'face-card-padlock unlocked';
    unlocked.innerHTML = '<span class="material-symbols-outlined">lock_open</span>';
    unlocked.title = 'Auto-tagged - not used for recognition';
    card.appendChild(unlocked);
}
```

### 4.2 Click handler for padlock toggle

**File: `static/faces.js`**

```javascript
padlock.addEventListener('click', async (e) => {
    e.stopPropagation();
    const newValue = await AppState.faces.toggleManualTag(face.id);
    face.manually_tagged = newValue;
    // Update icon
    updatePadlockIcon(padlock, newValue);
});
```

### 4.3 CSS for padlock overlay

**File: `static/styles.css`**

```css
.face-card-padlock {
    position: absolute;
    top: 4px;
    left: 4px;
    /* Similar styling to .face-card-star */
}

.face-card-padlock.unlocked {
    opacity: 0.5;
}
```

---

## File Summary

| File | Changes |
|------|---------|
| `faces.py` | Schema change, migration, update identify/reassess logic, add toggle function |
| `app.py` | Add toggle endpoint, include `manually_tagged` in responses |
| `static/appstate.js` | Add `toggleManualTag` method to faces domain |
| `static/faces.js` | Padlock overlay in pick-preferred cards, click handler |
| `static/styles.css` | Padlock styling |

---

## Testing Checklist

1. [ ] Migration sets existing named faces to manually_tagged=1
2. [ ] New manual identifications set manually_tagged=1
3. [ ] Auto-reassessment only uses manually_tagged=1 faces for matching
4. [ ] Auto-matched faces have manually_tagged=0
5. [ ] Pick-preferred shows padlock (locked) for manual, (unlocked) for auto
6. [ ] Clicking padlock toggles the flag
7. [ ] After toggling to unlocked, face is no longer used for matching
8. [ ] After toggling to locked, face is used for matching
