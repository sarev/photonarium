# Developer Guide

This document describes the source files that make up Photonarium, what each one
does, and how they fit together. It is intended as a map for developers working
on the codebase.

---

## Backend (Python)

### `app.py` — Flask REST API

The HTTP layer. Receives requests from the frontend and delegates to the
backend modules for database operations and image processing. Uses the waitress
WSGI server in production.

**Routes:**

| Prefix | Purpose |
|--------|---------|
| `/api/images` | Image listing, metadata updates, trash-based deletion |
| `/api/images/:id/thumbnail` | Thumbnail retrieval (snapped to 200 or 400px) |
| `/api/images/:id/full` | Full-resolution image serving |
| `/api/folders` | Folder registration and removal |
| `/api/status` | Processing status (indexing, embedding, face queues) |
| `/api/rescan` | Trigger folder rescan |
| `/api/duplicates` | Duplicate group retrieval and pruning by similarity level |
| `/api/stats` | Database and cache statistics |
| `/api/people` | People CRUD, merge, dissolve |
| `/api/people/:id/thumbnail` | Preferred face thumbnail for a person |
| `/api/faces` | Face listing, batch assign/unassign/suppress |
| `/api/faces/:id/thumbnail` | Cropped face thumbnail |
| `/api/events` | Backend event polling (faces_reassessed, etc.) |

### `imagedb.py` — Image Database Engine

The core backend module. Maintains a SQLite database of image files and their
metadata, discovers images by scanning user-registered folders, computes derived
properties (timestamps, checksums, perceptual hashes, embeddings), and runs
background processing in threads.

**Concepts:**

- **Images and metadata** — When ingesting an image the module extracts a "best"
  timestamp (preferring EXIF, then filename patterns, then filesystem), a
  content checksum, a perceptual hash, basic dimensions, and optional
  user-editable fields (description, rating).
- **OpenCLIP embeddings** — CLIP models map both images and text into the same
  vector space, enabling semantic search (text query vs image vectors) and
  visual similarity (image vs image). Description embeddings improve search
  recall. Cosine similarity reduces to a dot product because vectors are
  pre-normalised.
- **Duplicate detection levels** — Level 0 (exact checksum), Level 1
  (perceptual hash distance), Level 2 (high embedding similarity), Level 3
  (lower embedding similarity). Thresholds are configurable in
  `photonarium.yml`.
- **Thumbnails** — Generated on demand and cached on disk keyed by image
  checksum. Most thumbnail logic lives in `thumbnails.py`; only
  database-dependent stubs remain here.
- **Events** — A cursor-based event queue (`EventQueue`) supports multi-client
  polling. Backend processes and mutation routes emit events; each client polls
  with a `?since=T` cursor so events are never drained on read. If a client
  falls behind the 200-event ring buffer it receives a `stale` flag and
  performs a full state reload.

**Sections:**

1. Database schema and initialisation (WAL mode, tables, indexes, migrations)
2. Folder management and scanning
3. Image CRUD helpers
4. Metadata extraction (delegates timestamps to `timestamps.py`)
5. Ingestion thread (consumes file paths, extracts metadata, queues for
   embedding)
6. Embedding thread (batches image IDs, computes OpenCLIP embeddings, stores
   via single `executemany` + commit per batch)
7. Semantic search
8. Thumbnail generation stubs
9. Event queue (cursor-based, multi-client)
10. `ImageDatabase` public API wrapper
11. Graceful shutdown helpers

**Threading:** Three worker threads run by default (ingestion, embedding, face
detection). Work is coordinated through `queue.Queue` instances. The database
connection is shared and protected by `threading.RLock`. Embedding and face
detection threads yield the GIL periodically (10ms sleep between batches) to
prevent blocking Flask request handling.

### `faces.py` — Face Detection and Recognition

Face detection using MTCNN and face embeddings using InceptionResnetV1 from
facenet-pytorch.

**Responsibilities:**

1. Face detection in images with bounding boxes and confidence scores
2. 512D face embedding generation for recognition
3. Database schema and CRUD operations for people and faces
4. Auto-recognition by matching new faces against known people
5. Face thumbnail generation (200x200 crops from full images)
6. Background reassessment of unknown faces against known people (vectorised
   with numpy for GIL-friendly bulk matching)
7. Unknown face grouping by embedding similarity (union-find clustering)
8. Person face revalidation (ejecting faces that fall below threshold)

The face detection pipeline integrates with image indexing and runs as an
optional phase after OpenCLIP embedding generation. Background reassessment and
grouping run asynchronously and use optimistic locking (`updated_at`) to avoid
overwriting concurrent user edits.

### `thumbnails.py` — Thumbnail Generation and Caching

Generates, caches, and manages image thumbnails. Also includes image rotation
utilities (which invalidate thumbnails).

**Components:**

- `generate_thumbnail()` — Generate a single thumbnail with sharpening
- `get_thumbnail_cache_path()` — Compute cache path for a thumbnail
- `generate_missing_thumbnails()` — Bulk generate thumbnails for many images
- `ThumbnailCache` — Thread-safe LRU RAM cache for thumbnail bytes (backed by
  `OrderedDict` for O(1) eviction)
- `rotate_image_file()` — Rotate an image and invalidate its thumbnails

Only two canonical sizes are stored on disk: **200px** and **400px**. The
frontend uses CSS to scale to the exact display size. Cache structure:
`<thumbnail_dir>/<size>/<first2chars>/<checksum>.jpg`.

### `duplicates.py` — Duplicate Detection

Finds and groups duplicate or similar images across 4 similarity levels:

| Level | Name | Method |
|-------|------|--------|
| 0 | Identical | Same SHA256 checksum |
| 1 | Near-identical | Perceptual hash within Hamming distance threshold |
| 2 | Similar | High OpenCLIP embedding cosine similarity |
| 3 | Related | Lower embedding similarity threshold |

**Optimisation techniques:** multi-index hashing (LSH) for Level 1 to avoid
O(n^2) comparisons, chunked matrix multiplication for Levels 2-3 to manage
memory, union-find with path compression for efficient clustering, and
incremental updates for small batches of new/modified images.

### `caption.py` — Image Captioning

Automatic image description generation using BLIP/BLIP-2 models from
HuggingFace transformers.

Supported models (smallest to largest):

- `Salesforce/blip-image-captioning-base` (~1 GB)
- `Salesforce/blip-image-captioning-large` (~2 GB, default)
- `Salesforce/blip2-opt-2.7b` (~5 GB)
- `Salesforce/blip2-flan-t5-xl` (~8 GB)

The model is loaded lazily on first use to avoid startup delays. Runs in
offline mode (`HF_HUB_OFFLINE=1`); models must be pre-downloaded via
`download_models.py`.

### `config.py` — Configuration

Loads, saves, and validates configuration from `photonarium.yml` stored at the
OS-appropriate location (Windows: `%LOCALAPPDATA%\Photonarium\`, macOS:
`~/Library/Application Support/Photonarium/`, Linux: `~/.config/photonarium/`).
If no config exists on first run, auto-migrates a legacy `.photonarium.yml` from
the working directory if found, otherwise creates a default with full comments.
The `data_dir` field tells the app where to find its database and thumbnails.

### `timestamps.py` — Timestamp Extraction

Extracts and derives timestamps from images using multiple sources, in priority
order:

1. EXIF `DateTimeOriginal` tag (when photo was taken)
2. EXIF `DateTime` tag (when file was last modified by software)
3. Parsed from filename/path (more reliable than filesystem dates)
4. Filesystem creation/modification time

### `download_models.py` — Model Downloader

Standalone script that queries `app.py --list-models` for the current
configuration, then downloads the required OpenCLIP and BLIP/BLIP-2 models from
HuggingFace. Run before first use or after changing model settings.

---

## Frontend — Screen Modules (`static/`)

All frontend files live in the `static/` folder. The application is a
single-page app (`index.html`) with screen modules that register with a global
`App` object.

### `core.js` — Application Framework

Central infrastructure that all screen modules depend on. Initialises first and
exposes the global `App` object.

**Responsibilities:**

- **State management** — Current screen, theme (light/dark with localStorage
  persistence), thumbnail size preferences, pub/sub event system for
  cross-module communication.
- **Screen navigation** — Transitions between screens via `data-screen`
  attribute, toolbar group visibility, navigation history for back-button,
  lifecycle hooks (`onEnter`, `onLeave`).
- **API communication** — Wrapper functions for all backend API calls,
  request/response serialisation, error handling, mock mode for frontend
  development without the backend.
- **Toolbar management** — Event listeners for common buttons, button state
  updates, per-screen toolbar visibility.
- **Dialog system** — Modal dialogs, confirmation dialogs with Promise-based
  responses, emoji picker.
- **Utilities** — DOM helpers (`App.$(id)`, `App.createElement()`), debounce
  and throttle, image URL builders, date and file size formatting.
- **Module registration** — `App.registerModule()` for screen modules,
  `App.ready()` for post-initialisation logic.

### `thumbnails.js` — Shared Grid Infrastructure

Reusable components for thumbnail grid management, used by Gallery, Duplicates,
and Faces screens.

**Components:**

- **ThumbnailLoader** — Fetches thumbnails with scroll-aware prioritisation.
  Real-time priority based on distance from visible area centre.
  Auto-pruning of requests outside buffer zone. Timeout and scroll-abort
  protection. API: `request()`, `cancel()`, `prioritize()`, `bustCache()`,
  `clear()`.
- **VirtualGrid** — Virtual scrolling with absolute positioning. Only renders
  visible items plus a buffer. Spacer-based layout for correct scroll height.
  RAF-throttled scroll handling. API: `render()`, `refresh()`, `scrollTo()`,
  `scrollToId()`, `bind()`, `unbind()`.
- **GridSelection** — Unified selection handling. Click (single, Ctrl, Shift,
  right-click toggle), drag-box with auto-scroll, keyboard (arrows, Ctrl+A,
  Escape, Enter, Delete), long-press for touch. API: `select()`, `toggle()`,
  `selectRange()`, `selectAll()`, `clear()`, `bind()`, `unbind()`.

**Architecture:** DOM elements are only created after their thumbnail blob URL
is ready. Items are absolutely positioned based on index. A unified buffer zone
(visible rows +/- extra rows) determines what gets rendered and what gets
destroyed.

**Integration guide:**

1. Create VirtualGrid with config (container, getItems, createItem, etc.)
2. Call `render()` to populate the grid
3. Create GridSelection with the grid instance
4. Call `grid.bind()` and `selection.bind()` when screen becomes active
5. Call `grid.unbind()` and `selection.unbind()` when screen becomes inactive
6. Call `grid.destroy()` and `selection.destroy()` when recreating the grid

**Critical gotchas:**

1. **Unbind before destroying DOM** — Clearing `innerHTML` without `unbind()`
   first orphans scroll listeners. Always: unbind, destroy, then clear DOM.
2. **Hidden containers have zero dimensions** — Don't call `render()` on hidden
   containers. Set a `needsRefresh` flag instead.
3. **`bind()` triggers `_updateVisibleItems`** — Ensures thumbnails load when
   returning to a screen.
4. **Scroll container must be `config.container`** — VirtualGrid listens on
   this element. Nested scroll containers will cause mismatches.
5. **Blob URL memory leak** — Each thumbnail creates a blob URL that must be
   revoked when the item scrolls out of view or on destroy.
6. **Selection persists across unbind/bind** — Intentional. Call
   `selection.clear()` when recreating a grid with new data.
7. **Keyboard handler is on `document`** — Captures keys globally. Only the
   active grid's selection should be bound.
8. **O(n) lookups during scroll** — Fixed by building an `id -> index` Map once
   per scroll update. Maintain this if modifying `_updateVisibleItems`.

### `gallery.js` — Gallery Screen

Primary view for browsing the image catalogue.

- **Thumbnail grid** via VirtualGrid with dynamic size adjustment
- **Image selection** via GridSelection (single/Ctrl/Shift click, drag-box,
  keyboard navigation)
- **Info panel** with editable Description and Rating fields
- **Sorting** by date, rating, content similarity, or people
- **Filtering** applied from the Search screen
- **Navigation** — double-click/Enter opens fullscreen, scroll position
  preserved on return
- **Deletion** — Delete key with confirmation dialog

### `fullscreen.js` — Fullscreen Viewer

Modal overlay for full-screen image viewing with zoom, pan, and navigation. Not
part of the screen navigation system — it floats over whatever screen is active.

- **Image display** — Full-resolution with fit-to-screen scaling. Overlays
  (close button, filename) fade after 3 seconds and reappear on interaction.
- **Zoom** — Mouse scroll wheel centred on cursor, touch pinch centred on
  midpoint, double-tap toggles fit-to-screen vs 100%.
- **Pan** — Click-and-drag or touch drag when zoomed in, constrained to keep
  image edges visible.
- **Navigation** — Left/Right arrows (wrapping), swipe on touch devices.
- **API** — `open(imageId)`, `close()`, `isOpen()`.

### `database.js` — Database Management Screen

Manage image source folders and monitor processing status. Shown by default
when the database is empty.

- **Folder management** — List registered folders with image counts, add via
  native folder picker, remove with confirmation.
- **Processing status** — Polls backend for indexing, embedding, and face
  detection queue sizes.
- **Statistics** — Displays total image count.

### `settings.js` — In-App Configuration Editor

A standalone `Settings` object (not a screen module) that opens a modal dialog
for editing `photonarium.yml` from the browser. The form is entirely
schema-driven — the backend sends field types, constraints, and help text in
one `/api/config/schema` response, and the frontend renders a generic form.

- **Schema-driven** — zero hardcoded knowledge of individual settings.
- **Field types** — text, number, checkbox, textarea (for set-type fields).
- **Danger fields** — red border and warning icon for settings that require care.
- **Client + server validation** — range checks in the browser plus full
  validation on save via the Config constructor.

### `search.js` — Search and Filter Screen

Create filters to narrow down the gallery view.

- **Text search** — Semantic search via OpenCLIP similarity, not just keyword
  matching. Results ranked by relevance.
- **Date range** — Start and/or end date pickers filtering by image timestamp.
- **Rating filter** — Emoji-based filtering with an emoji picker dialog.
- **People filter** — Filter by people detected in images.
- **Validation** — Date range validation, input feedback.
- **Filter lifecycle** — Apply returns to Gallery, Clear resets all fields.
  Filter criteria preserved when navigating away.

### `duplicates.js` — Duplicates Screen

Find and manage duplicate or similar images.

- **Similarity slider** — 4 levels from Related (loose) to Identical (strict).
  Changing the slider immediately recomputes the display.
- **Stack display** — Duplicate groups shown as stacked thumbnail cards sorted
  by group size. The "best" image (highest resolution, best focus, lossless
  preferred) appears on top.
- **Interaction** — Click to select stacks, double-click to open the group in
  Gallery with the best image pre-selected. Keyboard navigation supported.
- **Performance** — Groups pre-computed on backend, cached on frontend for
  quick slider changes. Virtual scrolling via VirtualGrid.

### `faces.js` — Face Tagging

Handles two distinct UI contexts:

**1. Fullscreen tagging mode** — Overlay on the fullscreen image viewer.
Renders bounding boxes over detected faces with inline name input and
autocomplete. Suppress button (X) marks false positives.

**2. Faces screen** — Dedicated screen with three view modes:

| Mode | Description |
|------|-------------|
| `all` | Known people section (static grid of person cards) + unknown faces (VirtualGrid) |
| `unknowns` | Unknown faces only (toolbar toggle) |
| `pick-preferred` | Focus on one person's faces with star icons. Delete key unassigns faces instead of suppressing. |

**Data flow:** `AppState.faces` -> filter by search -> `displayedFaces[]` ->
VirtualGrid. All mutations go through AppState APIs. Refresh flags
(`needsRefresh`, `needsRerender`, `reloadPending`) coordinate updates without
full reloads.

### `faceThumbnails.js` — Face Thumbnail Cache-Busting

Manages cache-busting for face thumbnail URLs. When images are modified
(rotation, rescan), face thumbnails are regenerated on the backend. This
utility ensures the frontend fetches fresh versions by appending a timestamp
query parameter.

---

## Frontend — State Management (`static/appstate/`)

Central state management split into domain files. All domains attach to the
global `AppState` object created by `core.js`. GUI modules read from AppState,
subscribe to changes, and mutate via AppState methods — they never maintain
local copies of data.

### `core.js` — Transaction System and Utilities

Foundation for the AppState architecture.

- **Subscriber system** — `createSubscriberSystem()` returns `subscribe`,
  `broadcast`, and `notify` for reactive updates.
- **Transaction batching** — `transaction()` batches synchronous cache updates
  so subscribers fire once. `queueTransaction()` serialises async API calls.
- **localStorage helpers** — `storage.get()` / `storage.set()` with JSON
  serialisation.

### `index.js` — Domain Load Order

Documents the required script load order and verifies all domains are present
at startup.

**Load order:**

```
core.js → view.js → nav.js → filter.js → selection.js → status.js →
search.js → folders.js → duplicates.js → identity.js → images.js →
loading.js → events.js → index.js
```

**Dependencies:**

- `core.js` is the foundation (no dependencies)
- `view`, `nav`, `filter`, `selection` are independent domains
- `identity.js` contains both `faces` and `people` (tightly coupled — a face
  operation may create/delete a person, renaming triggers merge/dissolve)
- `images.js` depends on `duplicates._internal` for cascade delete

### Domain Files

Business logic and core application state in the frontend is handled by `static/appstate`. This performs an 'optimistic cacheing' strategy to maintain RAM-based copies of state which may be rolled-back if something goes wrong in the backend that invalidates any optimistic assumptions. It provides mechanisms for subscribers (across the frontend) to be notified via events when key state domains are updated, so (for example) they can refresh.

| File | Domain(s) | Persistence | Description |
|------|-----------|-------------|-------------|
| `view.js` | view | localStorage | Theme (light/dark), thumbnail size, sort settings |
| `nav.js` | nav | Memory | Current screen, history stack, fullscreen state, scroll positions |
| `filter.js` | filter | Memory | Search/filter criteria (text, date, rating, people, semantic, duplicates) |
| `selection.js` | selection | Memory | Per-context selection (`gallery`, `duplicates`, `faces`, `faces-pick`) with shift-click anchoring |
| `status.js` | status | Backend (polled) | Backend processing status: indexing, embedding, face detection queue sizes, reassessment state |
| `search.js` | search | Memory | Semantic search execution, results, loading state, last query |
| `folders.js` | folders | Backend | Registered folders, add/remove, rescan, statistics |
| `duplicates.js` | duplicates | Backend | Duplicate groups cached per similarity level, computation status |
| `identity.js` | faces, people | Backend | Face cache (full or partial), person identities, identification, assignment, merge, dissolve, revalidation |
| `images.js` | images | Backend | Image metadata cache with delta sync (epoch-based), display list (lazily recomputed from images + sort + filter) |
| `loading.js` | loading | Memory | Loading overlay with ownership tracking (only the current owner can hide it) |
| `events.js` | events | N/A | Cursor-based polling of `/api/events` every 2s with stale detection. Dispatches backend events (`faces_reassessed`, `folder_added/removed`, `processing_complete`, `image_ingested`, `nima_complete`, `images_modified`, `error`) and multi-client mutation events (`faces_changed`, `people_changed`, `images_changed`, `groups_changed`) to relevant domains via incremental cache updates |

---

## Key Principles For Developing Photonarium

The following rules apply to all submissions to the Photonarium codebase:

1. Must be compatible with the terms of the Apache 2.0 FOSS license.
2. Aside from `download_models.py` and the speculative downloading of the Google Material-Symbols fonts, Photonarium should be able to run offline indefinitely.
3. Photonarium should work correctly on (recent) Windows, Mac, and Linux machines.
4. Photonarium does not collect user/performance data to be sent anywhere for analysis.
5. While Photonarium benefits from GPU acceleration (NVIDIA CUDA or Apple MPS) for performance, it should still be able to function in a pure CPU environment.
6. Must respect the pre-existing Photonarium coding styles and formatting.
7. Must attempt to extend/adapt existing Photonarium code over re-inventing the wheel, duplicating.
8. The UI/UX design should be clean, elegant, obvious, non-technical, and themically/semantically consistent.
9. All frontend operations that act upon images/faces/people should be assumed to be batch operations to minimise frontend/backend round-trips and encourage parallelism.
10. Must be well commented with PEP (Python) and JDoc (JavaScript) comments, covering the *why* as well as the *what* and *how*.
11. Must try to parallelise computationally-intensive tasks, avoid poor scaling such as O(n*n) patterns or memory explosions with many images/faces/people.
12. Any 'thready' backend code must be correctly integrated with the 'graceful shutdown' code.
13. Care should be taken to avoid race conditions.
14. Never use SSE (server-side events) for passing info from backend to frontend, as they don't play well with Waitress. Use the existing event polling mechanism instead.
15. Ensure key documents are kept up-to-date (`README.md` and `DEVELOP.md`) and GUI elements have helpful, non-technical tooltips (`title` strings).
16. Schema changes need proper SQLite migrations so existing databases aren't broken on upgrade.
17. Avoid adding new dependencies without strong justification, prefer stdlib/existing dependencies.
