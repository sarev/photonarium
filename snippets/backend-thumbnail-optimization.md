# Backend Thumbnail Optimization

## Current Bottlenecks

Each thumbnail request currently requires:
1. **DB query** - `get_image(conn, image_id)` to retrieve checksum from image_id
2. **Filesystem stat** - Check if cached thumbnail exists
3. **Filesystem read** - Read thumbnail JPEG bytes
4. **Lazy generation** - If thumbnail doesn't exist, generate from original image

With 6 concurrent requests and ~60,000 images, this creates significant I/O contention.

---

## Improvement 1: Pre-generate Thumbnails During Indexing

### Problem
Thumbnails are generated on first view, making initial browsing extremely slow.

### Solution
Generate thumbnails immediately after image is indexed in `_process_image()`.

### Design Decisions

**Two canonical sizes only: 200px and 400px**
- Frontend requests are snapped to nearest size (threshold: 300px)
- Frontend CSS resizes the thumbnail to exact display size
- Reduces disk space vs. generating all 7 possible sizes (100-400 step 50)
- Trade-off: slight blur from CSS resize, but much simpler caching

**Sharpening applied during generation**
- UnsharpMask filter (radius=1.0, percent=60, threshold=3) applied after LANCZOS resize
- Counteracts the blur introduced by downscaling
- One-time cost during generation, better quality thumbnails

### Implementation
In `imagedb.py`, after successful image processing:

```python
def _generate_thumbnails(self, source_path: Path, checksum: str) -> bool:
    """Generate thumbnails at 200px and 400px."""
    for size in (200, 400):
        cache_path = get_thumbnail_cache_path(checksum, size=size, ...)
        if not cache_path.exists():
            generate_thumbnail(source_path, cache_path, size=size, ...)
    return True
```

### Trade-offs
- Indexing takes longer (acceptable for background task)
- First-time browsing becomes instant
- Two thumbnails per image (~40KB total typical)

---

## Improvement 2: CLI Switch for Bulk Thumbnail Generation

### Problem
Existing databases have images without pre-generated thumbnails.

### Solution
Add command-line flag to generate all missing thumbnails and exit.

### Implementation
In `app.py`:

```python
def generate_missing_thumbnails():
    """Generate thumbnails for all images that don't have them cached."""
    db = get_db()
    images = db.get_images_for_thumbnail_generation()

    total = len(images)
    generated = 0
    skipped = 0

    logger.info(f'Checking {total} images for missing thumbnails...')

    for i, img in enumerate(images):
        cache_path = get_thumbnail_cache_path(
            img['checksum'], size=200, thumbnail_dir=db.thumbnail_dir
        )

        if cache_path.exists():
            skipped += 1
            continue

        source_path = Path(img['path'])
        if not source_path.exists():
            logger.warning(f'Source not found: {img["basename"]}')
            continue

        if generate_thumbnail(source_path, cache_path, size=200,
                              quality=db.config.thumbnail_quality):
            generated += 1

        # Progress every 100 generated (not every 100 checked)
        if generated > 0 and generated % 100 == 0:
            logger.info(f'Generated {generated} thumbnails...')

    logger.info(f'Done. Generated {generated}, skipped {skipped} existing.')
```

### Usage
```bash
python app.py --generate-thumbnails
```

Skips images that already have cached thumbnails. Uses logging for progress updates.

---

## Improvement 3: RAM Cache for image_id → checksum Mapping

### Problem
Every thumbnail request queries the database just to look up the checksum.

### Solution
Load all (image_id, checksum) pairs into a dictionary on startup. Update on image add/delete.

### Implementation

```python
class ImageDatabase:
    def __init__(self, ...):
        # ... existing init ...
        self._checksum_cache: dict[str, str] = {}  # image_id -> checksum
        self._load_checksum_cache()

    def _load_checksum_cache(self):
        """Load all image_id -> checksum mappings into RAM."""
        cursor = self.conn.execute('SELECT id, checksum FROM images WHERE checksum IS NOT NULL')
        self._checksum_cache = {row[0]: row[1] for row in cursor}

    def get_checksum(self, image_id: str) -> str | None:
        """Get checksum for image_id from RAM cache."""
        return self._checksum_cache.get(image_id)

    def _on_image_added(self, image_id: str, checksum: str):
        """Update cache when image is added."""
        self._checksum_cache[image_id] = checksum

    def _on_image_deleted(self, image_id: str):
        """Update cache when image is deleted."""
        self._checksum_cache.pop(image_id, None)
```

Update `get_or_create_thumbnail()` to use cached lookup:

```python
def get_or_create_thumbnail(db, image_id, size, ...):
    checksum = db.get_checksum(image_id)
    if not checksum:
        return None

    cache_path = get_thumbnail_cache_path(checksum, size, thumbnail_dir)
    # ... rest of function ...
```

### Memory Usage
- ~60,000 images × (36 bytes UUID + 64 bytes SHA256 + dict overhead) ≈ 10MB
- Negligible for modern systems

---

## Improvement 4: RAM Cache for Thumbnail Bytes

### Problem
Even with checksums cached, every request still requires filesystem read.

### Solution
LRU cache of thumbnail bytes in RAM, with configurable maximum size.

### Configuration

Add to `config.py` and YAML template:

```yaml
# Thumbnail caching configuration
thumbnails:
  # ... existing config ...

  # Maximum RAM cache size for thumbnail bytes (MB)
  # Set to 0 to disable RAM caching
  cache_size_mb: 100
```

### Implementation

```python
from functools import lru_cache
import threading

class ThumbnailCache:
    """Thread-safe LRU cache for thumbnail bytes."""

    def __init__(self, max_size_bytes: int):
        self._max_size = max_size_bytes
        self._cache: dict[tuple[str, int], bytes] = {}  # (checksum, size) -> bytes
        self._access_order: list[tuple[str, int]] = []  # LRU tracking
        self._current_size = 0
        self._lock = threading.Lock()

    def get(self, checksum: str, size: int) -> bytes | None:
        """Get thumbnail bytes from cache, or None if not cached."""
        key = (checksum, size)
        with self._lock:
            if key in self._cache:
                # Move to end (most recently used)
                self._access_order.remove(key)
                self._access_order.append(key)
                return self._cache[key]
        return None

    def put(self, checksum: str, size: int, data: bytes):
        """Add thumbnail bytes to cache, evicting LRU items if needed."""
        key = (checksum, size)
        data_size = len(data)

        if data_size > self._max_size:
            return  # Don't cache items larger than max cache size

        with self._lock:
            # Evict until we have room
            while self._current_size + data_size > self._max_size and self._access_order:
                evict_key = self._access_order.pop(0)
                evicted = self._cache.pop(evict_key, None)
                if evicted:
                    self._current_size -= len(evicted)

            # Add new item
            if key in self._cache:
                self._current_size -= len(self._cache[key])
                self._access_order.remove(key)

            self._cache[key] = data
            self._access_order.append(key)
            self._current_size += data_size

    def clear(self):
        """Clear the cache."""
        with self._lock:
            self._cache.clear()
            self._access_order.clear()
            self._current_size = 0
```

### Integration with Flask

```python
# In app.py
_thumbnail_cache: ThumbnailCache = None

def get_thumbnail_cache() -> ThumbnailCache:
    global _thumbnail_cache
    if _thumbnail_cache is None:
        config = get_db().config
        max_bytes = config.thumbnail_cache_size_mb * 1024 * 1024
        _thumbnail_cache = ThumbnailCache(max_bytes)
    return _thumbnail_cache

@app.route('/api/images/<image_id>/thumbnail', methods=['GET'])
def get_thumbnail(image_id):
    size = request.args.get('size', 200, type=int)
    size = max(50, min(800, size))

    db = get_db()
    checksum = db.get_checksum(image_id)
    if not checksum:
        abort(404)

    # Check RAM cache first
    cache = get_thumbnail_cache()
    cached_bytes = cache.get(checksum, size)
    if cached_bytes:
        return Response(cached_bytes, mimetype='image/jpeg')

    # Get from filesystem (or generate)
    thumbnail_path = get_or_create_thumbnail_by_checksum(db, checksum, size)
    if thumbnail_path is None:
        abort(404)

    # Read and cache
    with open(thumbnail_path, 'rb') as f:
        data = f.read()
    cache.put(checksum, size, data)

    return Response(data, mimetype='image/jpeg')
```

### Memory Usage
- 100MB cache with ~20KB average thumbnail ≈ 5,000 thumbnails
- Covers typical viewport churn during scroll-back-and-forth
- Configurable via YAML

---

## Execution Checklist

### Phase 1: Configuration
- [x] Add `thumbnail_cache_size_mb` to config in `config.py`
- [x] Add to YAML template with default value (100)
- [x] Add validation (0-1000 MB range)

### Phase 2: Checksum Cache
- [x] Add `_checksum_cache` dict to ImageDatabase
- [x] Add `_load_checksum_cache()` method (loads on startup)
- [x] Add `get_checksum()` method (returns from RAM)
- [x] Update `_process_image()` to add to cache on create/update
- [x] Update `delete_image()` to remove from cache
- [x] Update `get_image_thumbnail_info()` to use cached checksum

### Phase 3: Thumbnail Bytes Cache
- [x] Create `ThumbnailCache` class (LRU, thread-safe)
- [x] Add `get_image_thumbnail_info()` for lightweight checksum lookup
- [x] Integrate with Flask thumbnail endpoint
- [x] Add `/api/stats/cache` endpoint for debugging

### Phase 4: Pre-generate During Indexing
- [x] Add `_generate_thumbnails()` method (200px + 400px)
- [x] Call from `_process_image()` for new and changed images
- [x] Add UnsharpMask sharpening to `generate_thumbnail()`
- [x] Snap requested sizes to canonical 200/400 in endpoint
- [ ] Test with new image additions

### Phase 5: CLI Bulk Generation
- [x] Add `--generate-thumbnails` argument to `app.py`
- [x] Implement `generate_missing_thumbnails()` for both sizes
- [x] Add progress reporting (via logging)
- [ ] Test with existing database

### Phase 6: Testing
- [ ] Benchmark before/after with large image set
- [ ] Test cache eviction behavior
- [ ] Test memory usage under load
- [ ] Test image add/delete updates checksum cache
- [ ] Verify no memory leaks

---

## Expected Performance Gains

| Scenario | Before | After |
|----------|--------|-------|
| First thumbnail (not generated) | DB query + generate + write + read | Generate during index (amortized to zero) |
| First thumbnail (already on disk) | DB query + stat + read | Checksum lookup (RAM) + stat + read |
| Repeated thumbnail (cache hit) | DB query + stat + read | Checksum lookup (RAM) + RAM cache hit |
| Scroll back to viewed thumbnails | Same as first | RAM cache hit (no I/O) |

The RAM cache hit path is: dict lookup + dict lookup + return bytes. No database, no filesystem, no syscalls.
