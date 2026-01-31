# AppState API Compatibility Analysis

## Overview

This document identifies gaps between what AppState expects from the backend API and what currently exists. These must be addressed during the migration phase.

---

## Status Summary

| Domain | API Compatibility | Notes |
|--------|-------------------|-------|
| `view` | ✅ Complete | No API needed (localStorage) |
| `nav` | ✅ Complete | No API needed (memory) |
| `filter` | ✅ Complete | No API needed (memory) |
| `images` | ✅ Complete | Delta sync with epochs exists |
| `folders` | ⚠️ Partial | Endpoints exist, epoch reconciliation missing |
| `people` | ✅ Complete | All endpoints exist |
| `faces` | ⚠️ Minor fixes | Wrong endpoint name, search parameter |
| `duplicates` | ⚠️ Partial | No recompute endpoint |
| `selection` | ✅ Complete | No API needed (memory) |

---

## Detailed Analysis

### AppState.images ✅

**Expected by AppState:**
- `GET /images` → `{images: [], epoch}`
- `GET /images?since={epoch}` → `{updated: [], deleted_ids: [], epoch}`
- `POST /images/{id}` for updates
- `DELETE /images/{id}`
- `POST /images/rotate`

**Backend provides:** All of the above. Fully compatible.

---

### AppState.folders ⚠️

**Expected by AppState:**
- `GET /folders` → folder list ✅
- `POST /folders` with `{path, epoch}` → expects `{response_epoch, request_epoch}`
- `DELETE /folders/{path}?epoch={epoch}` → expects `{response_epoch, request_epoch}`
- `POST /rescan` with `{epoch}` → expects `{response_epoch, request_epoch}`

**Backend provides:**
- `GET /folders` → folder list ✅
- `POST /folders` → `{path}` only, returns `{success, data: folder}`
- `DELETE /folders/{path}` → returns `{success, message}`
- `POST /rescan` → returns `{success, message}`

**Gap:** Backend doesn't support epoch-based reconciliation for folder operations.

**Resolution options:**
1. **Simplify AppState** - Remove epoch reconciliation from folders domain (it's low-frequency, optimistic updates are overkill)
2. **Enhance backend** - Add epoch support to folder endpoints (more work, questionable value)

**Recommendation:** Option 1 - Simplify AppState. Folder operations are infrequent and don't benefit from optimistic updates.

---

### AppState.people ✅

**Expected by AppState:**
- `GET /people` → people list
- `POST /people` with `{name}` → person
- `PATCH /people/{id}` with `{name}` or `{threshold}`
- `DELETE /people/{id}`
- `POST /people/{id}/set-preferred` with `{face_id}`
- `GET /people/{id}/thumbnail`

**Backend provides:** All of the above. Fully compatible.

---

### AppState.faces ⚠️

**Expected by AppState:**
- `GET /faces` → face list ✅
- `POST /faces/batch-identify` ❌ (wrong name)
- `POST /faces/{id}/unidentify` ✅
- `POST /faces/{id}/suppress` ✅
- `GET /faces/search?q={query}` ❌ (wrong format)

**Backend provides:**
- `GET /faces` → face list ✅
- `POST /faces/identify-batch` ✅ (note: different name)
- `POST /faces/{id}/unidentify` ✅
- `POST /faces/{id}/suppress` ✅
- `GET /faces?search={query}` ✅ (note: query param, not separate endpoint)

**Gap:** AppState uses wrong endpoint names/formats.

**Resolution:** Fix AppState to use correct names:
- `/faces/batch-identify` → `/faces/identify-batch`
- `/faces/search?q=` → `/faces?search=`

---

### AppState.duplicates ⚠️

**Expected by AppState:**
- `GET /duplicates?level={level}` → groups with status/epoch ✅
- `POST /duplicates/recompute?level={level}` ❌

**Backend provides:**
- `GET /duplicates?level={level}` → `{groups, status, epoch}` ✅
- No recompute endpoint - duplicates computed during scanning

**Gap:** No way to manually trigger duplicate recomputation.

**Resolution options:**
1. **Remove from AppState** - Duplicates are computed during scan, UI shows status
2. **Add backend endpoint** - Allow manual recomputation trigger

**Recommendation:** Option 1 for now. The `recompute()` method in AppState can be removed or made to trigger a rescan. Manual recomputation is an edge case.

---

## Fixes Required

### Immediate (AppState code fixes)

1. **faces.js identify endpoint name**
   - Change: `/faces/batch-identify` → `/faces/identify-batch`

2. **faces.js search endpoint format**
   - Change: `/faces/search?q={query}` → `/faces?search={query}`

3. **folders.js epoch reconciliation**
   - Simplify: Remove epoch handling, use simple success/error

4. **duplicates.js recompute**
   - Remove or stub the `recompute()` method

### Future (backend enhancements, optional)

1. **Epoch-based folder operations** - Low priority, folder changes are rare
2. **Manual duplicate recomputation** - Low priority, computed during scan

---

## Migration Checklist

Before migrating each consumer to AppState:

- [ ] Fix `faces.identify()` endpoint name
- [ ] Fix `faces.search()` endpoint format
- [ ] Simplify `folders` to remove epoch reconciliation
- [ ] Remove or stub `duplicates.recompute()`
- [ ] Test each domain's API calls work correctly
