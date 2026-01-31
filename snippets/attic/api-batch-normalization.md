# API Batch Normalization Plan

## Principle

**Every mutation endpoint that could operate on multiple items should always accept an array, even if operating on one item.** This:
- Simplifies frontend code (always use the same API)
- Reduces API surface area (one endpoint, not two)
- Consolidates backend logic (one code path, not two)
- Enables future optimizations (parallel processing, single transaction)

---

## Current State Analysis

### Already Batch (Good) ✅

| Endpoint | Request Format |
|----------|----------------|
| `POST /images/rotate` | `{ids: [...], degrees}` |
| `POST /faces/identify-batch` | `{face_ids: [...], name}` |
| `POST /faces/unassign-batch` | `{face_ids: [...]}` |

### Singular + Batch Duplicates (Consolidate) ⚠️

| Singular | Batch | Action |
|----------|-------|--------|
| `POST /faces/{id}/identify` | `POST /faces/identify-batch` | Keep batch, deprecate singular |
| `POST /faces/{id}/unassign` | `POST /faces/unassign-batch` | Keep batch, deprecate singular |

### Singular Only (Need Batch) ❌

| Current Singular | Proposed Batch | Notes |
|------------------|----------------|-------|
| `DELETE /images/{id}` | `POST /images/delete` | `{ids: [...], delete_files: bool}` |
| `POST /images/{id}` (update) | `POST /images/update` | `{updates: [{id, rating?, description?}, ...]}` |
| `POST /faces/{id}/suppress` | `POST /faces/suppress` | `{ids: [...]}` |
| `POST /faces/{id}/unidentify` | `POST /faces/unidentify` | `{ids: [...]}` |
| `DELETE /faces/{id}` | `POST /faces/delete` | `{ids: [...]}` |
| `DELETE /people/{id}` | `POST /people/delete` | `{ids: [...]}` |

### Inherently Singular (No Change Needed) ✅

These are genuinely single-resource operations:

| Endpoint | Reason |
|----------|--------|
| `GET /images/{id}` | Fetch one image's metadata |
| `GET /images/{id}/thumbnail` | Fetch one thumbnail |
| `GET /images/{id}/full` | Fetch one full image |
| `GET /images/{id}/histogram` | Fetch one histogram |
| `POST /images/{id}/reveal` | Reveal one in explorer |
| `GET /people/{id}` | Fetch one person |
| `GET /people/{id}/thumbnail` | Fetch one thumbnail |
| `PATCH /people/{id}` | Update one person (rare) |
| `GET /faces/{id}/thumbnail` | Fetch one face thumbnail |
| `GET /similar/{id}` | Similarity is relative to one reference |

---

## Backend Code Duplication

### faces.py - Identify

**Current:** Two code paths with subtly different behavior:
- `identify_face()` - singular, no reassessment trigger
- `identify_faces_batch()` - batch, triggers reassessment

**Target:** Single `identify_faces(face_ids, name, ...)` function that always handles arrays.

### faces.py - Suppress

**Current:** Only singular `suppress_face()` with inline person cleanup logic.

**Target:** Extract cleanup to helper, create `suppress_faces(face_ids)` that:
1. Suppresses all faces
2. Collects affected person IDs
3. Runs cleanup once for each affected person

### imagedb.py - Delete

**Current:** Only singular `delete_image()`.

**Target:** `delete_images(image_ids, from_disk=False)` that:
1. Gets checksums for all
2. Deletes all in single transaction
3. Invalidates all from cache

---

## Migration Plan

### Phase 1: Add Batch Endpoints (Non-Breaking)

Add new batch endpoints alongside existing singular ones:

```python
# New endpoints
POST /images/delete      {ids: [...]}
POST /images/update      {updates: [...]}
POST /faces/suppress     {ids: [...]}
POST /faces/unidentify   {ids: [...]}
POST /faces/delete       {ids: [...]}
POST /people/delete      {ids: [...]}
```

### Phase 2: Update AppState

Update AppState to use batch endpoints exclusively:

```javascript
// Before
async delete(id) {
    await App.apiDelete(`/images/${id}`);
}

// After
async delete(ids) {
    ids = Array.isArray(ids) ? ids : [ids];
    await App.apiPost('/images/delete', { ids });
}
```

### Phase 3: Migrate Frontend

Update all frontend code to use AppState batch methods.

### Phase 4: Deprecate Singular Endpoints

Mark singular endpoints as deprecated in docs. Log warnings when used.

### Phase 5: Remove Singular Endpoints

Remove deprecated singular endpoints after migration period.

---

## Naming Convention

For mutations, use:
- `POST /resource/action` with `{ids: [...], ...params}`

Not:
- `POST /resource/{id}/action` (singular)
- `POST /resource/action-batch` (redundant suffix)

Examples:
```
POST /images/delete     {ids: [...], delete_files: bool}
POST /images/update     {updates: [{id, ...}, ...]}
POST /images/rotate     {ids: [...], degrees}
POST /faces/identify    {ids: [...], name, preferred_id?}
POST /faces/suppress    {ids: [...]}
POST /faces/unidentify  {ids: [...]}
POST /faces/unassign    {ids: [...]}
POST /people/delete     {ids: [...]}
```

---

## AppState Changes Required

```javascript
// AppState.images
async delete(ids) { ... }     // Always array
async update(updates) { ... } // Array of {id, ...changes}
async rotate(ids, degrees)    // Already batch

// AppState.faces
async identify(ids, name, opts) { ... }  // Always array
async suppress(ids) { ... }              // Always array
async unidentify(ids) { ... }            // Already array (unassign)
async unassign(ids) { ... }              // Already array

// AppState.people
async delete(ids) { ... }     // Always array (rare, but consistent)
```

---

## Transaction Benefits

Batch operations enable single-transaction semantics:

```python
def delete_images(conn, image_ids, from_disk=False):
    """Delete multiple images in a single transaction."""
    with conn:  # Single transaction
        for image_id in image_ids:
            # ... delete logic
        conn.commit()  # Atomic: all or nothing
```

vs current:
```python
# Each delete is a separate transaction
for id in ids:
    delete_image(id)  # Can partially fail
```

---

## Summary

| Category | Count | Action |
|----------|-------|--------|
| Already batch | 3 | None |
| Duplicate singular+batch | 2 | Deprecate singular |
| Singular only (need batch) | 6 | Add batch endpoints |
| Inherently singular | 10+ | No change |

Total new batch endpoints needed: **6**
Total singular endpoints to deprecate: **8**
