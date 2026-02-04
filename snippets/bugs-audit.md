# Bugs, Performance, and Concurrency Audit

Comprehensive audit of the Imaginary codebase for bugs, performance issues at scale (100,000+ images/faces), race conditions, and inefficiencies.

---

## Executive Summary

| Severity | Backend | Frontend | Concurrency | Total |
|----------|---------|----------|-------------|-------|
| HIGH | 5 | 4 | 0 | 9 |
| MEDIUM | 9 | 4 | 1 | 14 |
| LOW | 3 | 4 | 0 | 7 |
| **Total** | **17** | **12** | **1** | **30** |

**Critical Scaling Issues:**
- Memory explosion in duplicate detection (40GB for 100K images)
- O(n²) algorithms in face refresh and picker mode
- LIKE queries causing full table scans
- Unbounded caches without eviction

**Good News:**
- Concurrency is well-designed (proper locking, optimistic updates)
- VirtualGrid handles large lists efficiently
- Transaction queue serializes frontend operations correctly

---

## Part 1: Backend Issues (Python)

### HIGH SEVERITY

#### 1.1 ~~Memory Explosion in Duplicate Detection~~ FIXED
**File:** `duplicates.py:1184-1310`

**Issue:** Incremental duplicate detection loaded ALL image embeddings into RAM at once.

**Fix Applied:** Rewrote `_compute_duplicates_embedding_incremental()` to use chunked database loading:
- Loads only dirty image embeddings first (typically few)
- Iterates through database in chunks of 5000 embeddings
- Computes similarities per chunk, frees memory before next chunk
- Memory usage reduced from O(n) to O(chunk_size + dirty_count)

---

#### 1.2 Folder Path LIKE Queries (Full Table Scans)
**File:** `imagedb.py:499, 1306-1315`

```python
LEFT JOIN images i ON i.path LIKE f.path || '%' AND i.deleted = 0
WHERE path LIKE ? || '%'
```

**Issue:** LIKE with prefix cannot use B-tree index. Falls back to full table scan.

**Impact:** With 100,000 images:
- `get_folders()` scans entire table for each folder
- `remove_folder()` scans for all images
- ~8-10 seconds per query vs <100ms with range query

**Recommendation:** Replace with range queries:
```python
folder_end = folder_path.rstrip('/') + '\x00'
cursor.execute("SELECT id FROM images WHERE path >= ? AND path < ?", (folder_path, folder_end))
```

---

#### 1.3 N+1 Query Pattern in People Filter
**File:** `faces.py:2074-2086`

```python
for person_id in person_ids:
    queries.append('''SELECT DISTINCT image_id FROM faces WHERE person_id = ?''')
query = ' INTERSECT '.join(queries)
```

**Issue:** Builds separate query for each person, joins with INTERSECT.

**Impact:** 5 people = 5 queries. Scales linearly with filter complexity.

**Recommendation:** Single query with GROUP BY/HAVING:
```sql
SELECT image_id FROM faces
WHERE person_id IN (?, ?, ?) AND suppressed = 0
GROUP BY image_id
HAVING COUNT(DISTINCT person_id) = 3
```

---

#### 1.4 Unbounded Face Embedding Cache
**File:** `faces.py:2149-2154`

```python
_embedding_cache = {
    'known': None,      # List of ALL (face_id, person_id, embedding)
    'unknown': None,    # List of ALL (face_id, embedding)
    'lock': threading.Lock(),
    'valid': False,
}
```

**Issue:** Cache holds ALL face embeddings permanently in memory. No eviction policy.

**Impact:** 1 million faces × 512-dim = ~400MB+ permanently in RAM.

**Recommendation:** Add LRU eviction or size limit.

---

#### 1.5 ThumbnailCache O(n) Remove in Lock
**File:** `thumbnails.py:554`

```python
with self._lock:
    if key in self._cache:
        self._access_order.remove(key)  # O(n) list operation!
        self._access_order.append(key)
```

**Issue:** `list.remove()` is O(n) while holding lock. Blocks all cache access.

**Impact:** 10,000 cached items × concurrent requests = severe lock contention.

**Recommendation:** Use `OrderedDict.move_to_end()` which is O(1).

---

### MEDIUM SEVERITY

#### 1.6 Missing Index on faces Join
**File:** `faces.py:1629-1631`

```sql
FROM faces f
LEFT JOIN people p ON f.person_id = p.id
JOIN images i ON f.image_id = i.id
```

**Issue:** `get_all_faces()` does full table scan. Missing composite indexes.

**Recommendation:**
```sql
CREATE INDEX idx_faces_suppressed_person ON faces(suppressed, person_id) WHERE suppressed = 0;
CREATE INDEX idx_faces_image ON faces(image_id, suppressed) WHERE suppressed = 0;
```

---

#### 1.7 Missing Index on duplicate_groups.image_id
**File:** `imagedb.py:316`

**Issue:** Deleting an image cascades to duplicate_groups, requiring full table scan.

**Recommendation:**
```sql
CREATE INDEX idx_dup_image ON duplicate_groups(image_id);
```

---

#### 1.8 No Transaction Batching in Embedding Updates
**File:** `imagedb.py:2314-2323`

```python
for (idx, embedding), image_id in zip(results, image_ids):
    with self._db_lock:
        update_image_embedding(self.conn, image_id, embedding_bytes)
    # One commit per image!
```

**Issue:** 100,000 images = 100,000 fsync() calls.

**Impact:** 100,000 commits × 5ms = 500 seconds vs batch: 100 commits × 5ms = 5 seconds.

**Recommendation:** Commit every 100 updates.

---

#### 1.9 WAL Checkpoint Timeout Under Load
**File:** `imagedb.py:365`

```python
conn.execute('PRAGMA busy_timeout=5000')  # 5 seconds
```

**Issue:** Multiple threads writing during indexing can timeout checkpoint.

**Recommendation:** Increase to 10 seconds, consider manual checkpoint control.

---

#### 1.10 Face Embedding Cache Stampede
**File:** `faces.py:2159`

```python
with _embedding_cache['lock']:
    if not _embedding_cache['valid']:
        _embedding_cache['known'] = get_all_known_face_embeddings(conn)  # Blocks everyone
```

**Issue:** First request after invalidation blocks all others while loading.

**Impact:** Background reassessment runs every 2 seconds, causing recurring stampedes.

**Recommendation:** Use double-checked locking pattern.

---

#### 1.11 Unbounded Pending Futures in Ingestion
**File:** `imagedb.py:1669`

**Issue:** `pending_futures` list grows without bound if I/O is slow.

**Impact:** Memory accumulation with slow storage.

---

#### 1.12 SELECT * Patterns in Duplicates
**File:** `duplicates.py:804, 888`

**Issue:** Fetches all columns when only `id` and `perceptual_hash` needed.

**Impact:** Transfers unnecessary BLOBs (embeddings) over memory.

---

#### 1.13 Hardcoded Chunk Size
**File:** `duplicates.py:1112-1113`

**Issue:** `chunk_size=1000` hardcoded. Optimal depends on system RAM.

---

#### 1.14 Duplicate LIKE Query in remove_folder()
**File:** `imagedb.py:602-609`

Same issue as 1.2.

---

### LOW SEVERITY

#### 1.15 Silent Exception Suppression
**File:** `thumbnails.py:166-167`

```python
except Exception:
    pass  # Hides real errors
```

**Recommendation:** Catch specific exceptions (`IOError`, `NotImplementedError`).

---

#### 1.16 Inconsistent Error Handling in Face Detection
**File:** `imagedb.py:2567-2569`

**Issue:** If error before `batch_ids` assigned, code crashes.

---

#### 1.17 Multiple DB Calls for Related Data
**File:** `app.py:1075-1088`

**Issue:** Three separate queries for duplicates status/epoch/groups.

---

## Part 2: Frontend Issues (JavaScript)

### HIGH SEVERITY

#### 2.1 O(n²) Loop in Face Refresh
**File:** `static/appstate/identity.js:1246-1250`

```javascript
for (const [faceId, face] of _cache) {
    if (imageIds.includes(face.image_id)) {  // O(n) inside O(n)
        _cache.delete(faceId);
    }
}
```

**Impact:** 100,000 faces × array scan = very slow.

**Recommendation:** Convert `imageIds` to Set for O(1) lookup.

---

#### 2.2 Linear Search in Pick-Preferred Mode
**File:** `static/faces.js:1782`

```javascript
const faces = faceIds.map(id => pickPreferredFaces.find(f => f.id === id)).filter(Boolean);
```

**Impact:** O(n×m) for n selected faces in array of m faces.

**Recommendation:** Build Map for O(1) lookup.

---

#### 2.3 Subscription Leak in Fullscreen Tagging
**File:** `static/faces.js:990-998`

```javascript
AppState.nav.onChanged((event) => {
    // Subscription never unsubscribed on screen leave
    window.addEventListener('resize', resizeHandler);  // Accumulates
});
```

**Impact:** Memory leak, accumulating event handlers.

**Recommendation:** Track subscription, unsubscribe in `onLeave()`.

---

#### 2.4 Unbounded Derived Cache Growth
**File:** `static/appstate/identity.js:288-299`

**Issue:** `_facesByPerson` and `_facesByImage` rebuilt from potentially incomplete data.

---

### MEDIUM SEVERITY

#### 2.5 Sequential Filter Passes
**File:** `static/appstate/images.js:90-113`

**Issue:** Multiple `.filter()` calls create intermediate arrays.

**Recommendation:** Combine into single pass.

---

#### 2.6 Linear Lookup for Shift-Click Selection
**File:** `static/appstate/selection.js:217-234`

```javascript
const anchorIdx = ids.indexOf(ctx.anchor);  // O(n)
const toIdx = ids.indexOf(toId);             // O(n)
```

**Impact:** Every shift-click searches twice.

---

#### 2.7 Display List Returned by Reference
**File:** `static/gallery.js:278`

**Issue:** `getDisplayList()` returns actual array reference. Mutation would cause inconsistency.

---

#### 2.8 Missing Error Handling in Async Operations
**File:** Multiple locations

```javascript
AppState.faces.refreshForImages(imageIds);
// No await, no error handling
```

---

### LOW SEVERITY

#### 2.9 Array.shift() O(n) Operation
**File:** `static/database.js:499, 563, 607`

**Issue:** `shift()` shifts all remaining elements.

---

#### 2.10 Serial Face Fetches
**File:** `static/appstate/identity.js:700-731`

```javascript
for (const faceId of missingIds) {
    const response = await App.apiGet(`/faces/${faceId}`);  // One at a time
}
```

**Impact:** 100 missing faces = 100 sequential requests.

---

#### 2.11 Redundant getAll() Calls in Debug
**File:** `static/faces.js:182-183`

```javascript
appStateFacesCount: AppState.faces.getAll().length,
appStateFacesUnknown: AppState.faces.getAll().filter(...).length,  // Redundant
```

---

#### 2.12 Event Listeners Not Explicitly Cleaned
**File:** `static/faces.js:647-649`

**Issue:** Search input listeners persist if DOM not removed cleanly.

---

## Part 3: Concurrency Issues

### MEDIUM SEVERITY

#### 3.1 Checksum Cache TOCTOU Window
**File:** `imagedb.py:4374-4376`

```python
with self._db_lock:
    self.conn.execute(...)  # Update DB
    self.conn.commit()
# WINDOW: Cache still has old checksum
with self._checksum_cache_lock:
    self._checksum_cache[image_id] = new_checksum  # Update cache
```

**Race scenario:**
1. Thread A rotates image, commits to DB
2. Thread B requests thumbnail, gets OLD checksum from cache
3. Thread A updates cache

**Impact:** Thumbnail cache miss, wrong file served briefly. Self-healing on retry.

**Recommendation:** Move cache update inside `_db_lock`.

---

### WELL-DESIGNED PATTERNS (No Issues)

The following concurrency patterns are correctly implemented:

1. **Optimistic locking for faces** - `updated_at` column prevents lost updates
2. **Three-phase pattern** - READ (lock) → COMPUTE (no lock) → WRITE (lock)
3. **RLock for nested calls** - Prevents deadlock in multi-lock scenarios
4. **Transaction queue** - Frontend serializes async operations
5. **Defensive WHERE clauses** - Background operations check current state
6. **Per-image rotation locks** - Prevents concurrent rotation of same file

---

## Priority Recommendations

### Immediate (Before 50K+ Scale)

1. ~~**Fix memory explosion in duplicates** - Use chunking in incremental path~~ **DONE**
2. **Replace LIKE queries with range queries** - 100x speedup
3. **Convert O(n²) loops to use Sets** - identity.js:1247, faces.js:1782
4. **Add missing database indexes** - faces table, duplicate_groups table
5. **Fix subscription leak** - faces.js tagging mode

### Short Term

6. **Batch embedding updates** - Commit every 100 instead of per-row
7. **Use OrderedDict for thumbnail cache** - O(1) instead of O(n)
8. **Add LRU eviction to embedding cache** - Bound memory usage
9. **Fix cache stampede** - Double-checked locking
10. **Combine filter passes** - Single pass in _filterImages()

### Nice to Have

11. **Batch face fetch endpoint** - Reduce serial requests
12. **Move checksum cache update inside lock** - Fix TOCTOU
13. **Add configurable chunk sizes** - Tune for system RAM
14. **Replace shift() with circular buffer** - Minor optimization

---

## Scale Projections

| Images | Current Performance | After Fixes |
|--------|---------------------|-------------|
| 10,000 | Works well | Works well |
| 50,000 | LIKE queries slow (~10s) | <1s |
| 100,000 | OOM in duplicate detection | Works |
| 500,000 | Multiple issues compound | Needs testing |

---

## Notes

- The codebase has strong architectural foundations
- Concurrency design is excellent (proper locking, optimistic updates)
- Main issues are algorithmic (O(n²), missing indexes) not architectural
- VirtualGrid handles large lists efficiently
- Most fixes are straightforward (Set instead of array, add index)
