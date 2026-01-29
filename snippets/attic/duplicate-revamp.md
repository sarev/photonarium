  Core Issues with Current Approach

  1. Binary choice problem: Full rebuild vs one-at-a-time incremental is the wrong granularity. You need batch incremental.
  2. Database round-trips: If each image comparison hits the DB, a batch of 500 images at 4 similarity levels could be thousands of queries.
  3. Group merging complexity: When adding images incrementally, you might need to merge existing groups (image A matches group 1, image B matches group 2, but A and B
  also match each other → merge groups 1 and 2).

  Suggested Architecture

  1. Use Union-Find for Group Management

  Union-Find (disjoint set) is the natural data structure for this problem:
  - Nearly O(1) per operation with path compression
  - Handles group merges elegantly
  - Works great for batch operations

  For a batch of dirty images:
  1. Load existing groups into Union-Find
  2. For each dirty image, find all matches (existing + other dirty)
  3. Union the dirty image with each match
  4. Extract final groups from Union-Find

  This naturally handles the case where dirty image A matches existing group 1, dirty image B matches existing group 2, and A matches B → all three end up in one merged
  group.

  2. In-Memory Data Strategy

  Load once, query many:
  ┌───────┬───────────────────────────────────────────────────┬────────────────────────────┐
  │ Level │                   Data to Cache                   │ Size Estimate (10k images) │
  ├───────┼───────────────────────────────────────────────────┼────────────────────────────┤
  │ 0     │ checksum → [image_ids] dict                       │ ~1 MB                      │
  ├───────┼───────────────────────────────────────────────────┼────────────────────────────┤
  │ 1     │ phash → [image_ids] dict + raw hashes for hamming │ ~1 MB                      │
  ├───────┼───────────────────────────────────────────────────┼────────────────────────────┤
  │ 2-3   │ Embedding matrix (numpy array)                    │ ~20 MB (512-dim floats)    │
  └───────┴───────────────────────────────────────────────────┴────────────────────────────┘
  For levels 0-1, a simple dict lookup is O(1). For levels 2-3, you could:
  - Use numpy broadcasting for batch cosine similarity (very fast)
  - Or use FAISS for approximate nearest neighbor if you scale to 100k+ images

  3. Batch Incremental Algorithm

  batch_update_duplicates(dirty_ids, level):
      # Load everything into memory ONCE
      all_data = load_image_data_for_level(level)  # checksums, hashes, or embeddings
      existing_groups = load_groups(level)

      # Initialize Union-Find with existing groups
      uf = UnionFind(all_image_ids)
      for group in existing_groups:
          uf.union_all(group.image_ids)

      # Find matches for all dirty images (batch operation)
      if level in (0, 1):
          matches = find_hash_matches_batch(dirty_ids, all_data, level)
      else:
          matches = find_embedding_matches_batch(dirty_ids, all_data, threshold)

      # Union dirty images with their matches
      for dirty_id, match_ids in matches.items():
          for match_id in match_ids:
              uf.union(dirty_id, match_id)

      # Extract and save new groups (single transaction)
      new_groups = uf.extract_groups()
      save_groups(new_groups, level)

  4. Level-Specific Optimizations

  Level 0 (Identical): Trivial - just dict[checksum] → [image_ids]. O(1) lookup per dirty image.

  Level 1 (Near-identical):
  - Option A: Bucket by hash prefix, only compare within buckets
  - Option B: Precompute hamming distance matrix for small datasets
  - Option C: Use a BK-tree for efficient hamming distance queries

  Levels 2-3 (Embedding similarity):
  # Batch cosine similarity with numpy
  dirty_embeddings = embeddings[dirty_indices]  # shape: (batch, 512)
  all_embeddings = embeddings  # shape: (n, 512)

  # Normalize once
  dirty_norm = dirty_embeddings / np.linalg.norm(dirty_embeddings, axis=1, keepdims=True)
  all_norm = all_embeddings / np.linalg.norm(all_embeddings, axis=1, keepdims=True)

  # All pairwise similarities in one operation
  similarities = dirty_norm @ all_norm.T  # shape: (batch, n)
  matches = np.where(similarities > threshold)

  This computes 500 × 10,000 = 5 million similarity scores in milliseconds.

  5. Threshold Strategy

  Instead of "n dirty images → full rebuild", consider:
  ┌────────────────────────────────────┬───────────────────┐
  │             Condition              │     Strategy      │
  ├────────────────────────────────────┼───────────────────┤
  │ dirty < 100                        │ Batch incremental │
  ├────────────────────────────────────┼───────────────────┤
  │ 100 ≤ dirty < 20% of total         │ Batch incremental │
  ├────────────────────────────────────┼───────────────────┤
  │ dirty ≥ 20% of total               │ Full rebuild      │
  ├────────────────────────────────────┼───────────────────┤
  │ User requests "Rebuild duplicates" │ Full rebuild      │
  └────────────────────────────────────┴───────────────────┘
  The percentage-based threshold accounts for database size.

  6. Caching Layer

  Add a DuplicateGroupCache that:
  - Loads all group data on startup (or lazily on first access)
  - Provides fast in-memory queries
  - Batches writes to DB (flush on idle or after N changes)
  - Invalidates gracefully when images are deleted

  class DuplicateGroupCache:
      _groups: dict[level, dict[group_hash, set[image_id]]]
      _image_to_group: dict[level, dict[image_id, group_hash]]
      _dirty: bool

      def get_group_for_image(level, image_id) -> group_hash | None
      def get_images_in_group(level, group_hash) -> set[image_id]
      def update_groups(level, new_groups: list[set[image_id]])
      def flush_to_db()

  7. Handling Image Deletion/Modification

  When an image is deleted or modified:
  - Remove from its current group(s)
  - If group becomes singleton, dissolve it
  - If modified (not deleted), add to dirty set for re-grouping

  This is also cleaner with Union-Find - you rebuild affected groups rather than trying to patch them.

  Summary

  The key changes:
  1. Union-Find for group management (handles merges naturally)
  2. In-memory caching of checksums, hashes, embeddings, and group assignments
  3. Batch operations - process all dirty images together, not one at a time
  4. Numpy vectorization for embedding similarity (orders of magnitude faster)
  5. Smarter threshold based on percentage, not absolute count

---

## Implementation Plan

The duplicate detection code has been extracted into `duplicates.py` with a
`DuplicateManager` class. The following phases build on that foundation.

### Phase 1: In-Memory Group Cache

Add `DuplicateGroupCache` to avoid repeated DB queries for group lookups.

**Changes:**
- Add `_groups` dict (level → group_hash → set of image_ids)
- Add `_image_to_group` reverse index (level → image_id → group_hash)
- Load on startup or lazily on first access
- Invalidate entries when images are deleted/modified
- Batch flush to DB (on idle or after N changes)

**Files:** `duplicates.py`

### Phase 2: Union-Find for Group Management

Replace ad-hoc group merging with proper Union-Find data structure.

**Changes:**
- Add `UnionFind` class with path compression and union-by-rank
- Load existing groups into Union-Find before processing dirty batch
- Use `union()` for all matches, let data structure handle merges
- Extract final groups with `extract_groups()` at the end

**Files:** `duplicates.py`

### Phase 3: Level 1 Multi-Index Hashing (LSH)

Current level 1 already uses LSH bands. Improve with:

**Changes:**
- Tune band count based on threshold (currently threshold + 1)
- Add metrics logging for comparison reduction percentage
- Consider BK-tree as alternative for small datasets

**Files:** `duplicates.py` (`_compute_duplicates_level1`)

### Phase 4: Vectorized Embedding Similarity

Improve levels 2-3 with better numpy operations.

**Changes:**
- Pre-normalize all embeddings once at load time (store normalized)
- Use chunked matrix multiplication for memory efficiency (already done)
- Add option for FAISS approximate NN for very large datasets (100k+)
- Return sparse similarity results (only pairs above threshold)

**Files:** `duplicates.py` (`_compute_embedding_duplicates_chunked`)

### Phase 5: Percentage-Based Threshold

Replace fixed dirty count threshold with percentage-based logic.

**Changes:**
- Add `incremental_threshold_percent` config option (default: 20%)
- Compute: `use_incremental = dirty_count < (total_count * threshold_percent)`
- Keep minimum absolute threshold to avoid full rebuild for tiny batches
- Add "force rebuild" option for manual trigger

**Files:** `duplicates.py`, `config.py`

### Phase 6: Deletion/Modification Handling

Clean up groups when images are removed or changed.

**Changes:**
- On delete: remove image from group, dissolve if singleton
- On modify: remove from group, add to dirty set
- Batch the cleanup (don't rebuild groups on every delete)
- Add `invalidate_image(image_id)` method to DuplicateManager

**Files:** `duplicates.py`, `imagedb.py` (delete_image hook)

---

### Priority Order

1. **Phase 2 (Union-Find)** - Foundational, simplifies everything else
2. **Phase 1 (Cache)** - Big performance win for repeated queries
3. **Phase 5 (Threshold)** - Quick config change, immediate benefit
4. **Phase 6 (Deletion)** - Important for correctness
5. **Phase 4 (Embeddings)** - Already decent, optimize later
6. **Phase 3 (LSH)** - Already working, tune if needed
