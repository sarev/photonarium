# Developer Guide

This document describes the source files that make up Photonarium, what each one
does, and how they fit together. It is intended as a map for developers working
on the codebase.

---

## Backend (Python, `app/`)

### `app/app.py` - Flask REST API

The HTTP layer. Receives requests from the frontend and delegates to the
backend modules for database operations and image processing. Uses the waitress
WSGI server in production.

**Routes (83):**

Mutation endpoints prefer batch format (arrays, not single items).

**API Design Principles:**
- Frontend generates all IDs using `crypto.randomUUID()`
- Requests send exact state to persist (not "find or create")
- Responses return success/error only (not computed state)
- Backend validates and stores, doesn't compute application logic
- Derived values (face_count) computed via SQL JOIN, not stored

#### Images (12 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/images` | List all images with metadata |
| GET | `/api/images/:id` | Get single image metadata |
| POST | `/api/images/:id` | Update image (description, rating) |
| GET | `/api/images/:id/exif` | Get EXIF metadata for an image |
| GET | `/api/images/:id/scenes` | Get scene list for a video `?query=` for scored results |
| POST | `/api/images/trash` | Move images to trash `{image_ids: []}` |
| GET | `/api/images/:id/thumbnail?size=N` | Get thumbnail (snapped to 200 or 400px) |
| GET | `/api/images/:id/full` | Get full-resolution image |
| GET | `/api/images/:id/histogram` | Get image histogram data |
| POST | `/api/images/:id/generate-caption` | Generate BLIP caption for image |
| POST | `/api/images/rotate` | Rotate images `{image_ids: [], direction}` |
| GET | `/api/images/people-names` | Get people names appearing in images |

#### Folders (4 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/folders` | List registered folders with image counts |
| POST | `/api/folders` | Add folder `{path: string}` |
| DELETE | `/api/folders/:path` | Remove folder and its images |
| POST | `/api/pick-folder` | Open native folder picker dialog |

#### Search & Similarity (3 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/search` | Semantic search `{text, threshold}` |
| POST | `/api/search/videos` | Scene-level video search `{query, threshold, limit}` |
| GET | `/api/similar/:id` | Get images similar to a given image |

#### Metadata (3 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/metadata-search` | Subsequence search on EXIF metadata `{criteria: {key: query}}` |
| GET | `/api/metadata-keys` | All distinct metadata keys in the database |
| GET | `/api/metadata-values?key=X` | Distinct values for a key (autocomplete) |

#### Status & Config (8 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Processing status `{status, indexing_queue, embedding_queue}` |
| POST | `/api/rescan` | Queue all folders for re-indexing |
| GET | `/api/config` | Get frontend-relevant configuration values |
| GET | `/api/config/schema` | Full config schema for the settings editor |
| POST | `/api/config/save` | Save config values `{values: {key: value}}` |
| GET | `/api/health` | Health check endpoint |
| POST | `/api/restart` | Restart the server process |
| GET | `/api/logs` | Get recent log entries from the database |

#### Duplicates (3 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/duplicates?level=N` | Get duplicate groups at level 0-5 |
| POST | `/api/duplicates/sort-semantic` | Sort duplicate groups by semantic similarity |
| POST | `/api/duplicates/prune` | Prune groups: keep best, trash rest `{level, keep_count}` |

#### Groups (7 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/groups/preview` | Preview keep/trash split for all groups at a level |
| POST | `/api/groups` | Create custom group `{group_hash, name, image_ids}` |
| PATCH | `/api/groups/:hash` | Rename custom group `{name}` |
| DELETE | `/api/groups/:hash` | Delete custom group |
| POST | `/api/groups/:hash/preview` | Preview smart-group membership changes |
| POST | `/api/groups/:hash/images` | Add images to group `{image_ids}` |
| POST | `/api/groups/:hash/images/remove` | Remove images from group `{image_ids}` |

#### Import (3 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/import` | Import from local paths `{paths: []}` |
| POST | `/api/import/preflight` | Check which files are new `{checksums: []}` |
| POST | `/api/import/upload` | Import via multipart file upload |

#### Stats (2 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/stats` | Get `{totalImages, totalFolders}` |
| GET | `/api/stats/cache` | Get thumbnail cache statistics |

#### Events (2 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/events` | Fetch and clear pending events |
| GET | `/api/events/count` | Get count of pending events (lightweight) |

#### Scenes (3 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/scenes/:id/thumbnail` | Get scene keyframe thumbnail |
| PUT | `/api/scenes/:id/transcription` | Update scene subtitle text `{transcription}` |
| PUT | `/api/images/:id/preferred-scene` | Set preferred scene for a video `{scene_id}` |

#### Utility (1 route)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/reveal` | Reveal a file or folder in the OS file explorer `{path}` |

#### People (10 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/people` | List all people (face_count via JOIN) |
| POST | `/api/people` | Create person `{id, name}` |
| GET | `/api/people/:id` | Get person details |
| PATCH | `/api/people/:id` | Update person `{name, preferred_face_id, threshold}` |
| DELETE | `/api/people/:id` | Delete person (faces become untagged) |
| POST | `/api/people/:id/merge` | Merge into another person `{into: target_id}` |
| POST | `/api/people/:id/dissolve` | Unidentify all faces and delete person |
| POST | `/api/people/:id/set-preferred` | Set preferred face for person |
| GET | `/api/people/:id/faces` | Get all faces for a person |
| GET | `/api/people/:id/thumbnail` | Get preferred face thumbnail |

#### Faces (21 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/faces` | List all faces |
| GET | `/api/faces/:id` | Get single face details |
| GET | `/api/faces/:id/thumbnail` | Get cropped face thumbnail |
| GET | `/api/faces/:id/matches` | Get closest matching people for a face |
| GET | `/api/images/:id/faces` | Get faces detected in an image |
| POST | `/api/faces/assign` | Assign faces to person `{face_ids: [], person_id}` |
| POST | `/api/faces/unassign` | Unassign single face |
| POST | `/api/faces/unassign-batch` | Unassign faces `{face_ids: []}` |
| POST | `/api/faces/:id/unassign` | Unassign a specific face |
| POST | `/api/faces/suppress` | Mark as false positives `{face_ids: []}` |
| POST | `/api/faces/:id/suppress` | Suppress a specific face |
| PATCH | `/api/faces` | Batch update properties `{face_ids: [], locked: bool}` |
| POST | `/api/faces/:id/identify` | Identify a specific face |
| POST | `/api/faces/identify-batch` | Batch identify faces |
| POST | `/api/faces/:id/unidentify` | Unidentify a specific face |
| POST | `/api/faces/:id/toggle-manual` | Toggle manual tagging flag |
| DELETE | `/api/faces/:id` | Delete a face detection |
| POST | `/api/faces/reassess` | Trigger background face reassessment |
| GET | `/api/faces/reassess-status` | Get reassessment progress |
| POST | `/api/faces/reassess-ack` | Acknowledge reassessment completion |
| GET | `/api/faces/group-status` | Get face grouping status |

### `app/imagedb.py` - Image Database Engine

The core backend module. Maintains a SQLite database of image files and their
metadata, discovers images by scanning user-registered folders, computes derived
properties (timestamps, checksums, perceptual hashes, embeddings), and runs
background processing in threads.

**Concepts:**

- **Images and metadata** - When ingesting an image the module extracts a "best"
  timestamp (preferring EXIF, then filename patterns, then filesystem), a
  content checksum, a perceptual hash, basic dimensions, and optional
  user-editable fields (description, rating).
- **OpenCLIP embeddings** - CLIP models map both images and text into the same
  vector space, enabling semantic search (text query vs image vectors) and
  visual similarity (image vs image). Description embeddings improve search
  recall. Cosine similarity reduces to a dot product because vectors are
  pre-normalised.
- **Duplicate detection levels** - Level 0 (exact checksum), Level 1
  (perceptual hash distance), Level 2 (high embedding similarity), Level 3
  (lower embedding similarity). Thresholds are configurable in
  `photonarium.yml`.
- **Thumbnails** - Generated on demand and cached on disk keyed by image
  checksum. Most thumbnail logic lives in `thumbnails.py`; only
  database-dependent stubs remain here.
- **Events** - A cursor-based event queue (`EventQueue`) supports multi-client
  polling. Backend processes and mutation routes emit events; each client polls
  with a `?since=T` cursor so events are never drained on read. If a client
  falls behind the 200-event ring buffer it receives a `stale` flag and
  performs a full state reload.

**Sections:**

1. Database schema and initialisation (WAL mode, tables, indexes, migrations)
2. Folder management and scanning
3. Image CRUD helpers
4. Metadata extraction (delegates to `metadata.py`)
5. `PipelineOrchestrator` — single worker thread running 7 sequential stages
6. Semantic search
7. Thumbnail generation stubs
8. Event queue (cursor-based, multi-client)
9. Import worker (copies files into catalogue directory, organised by date)
10. `ImageDatabase` public API wrapper
11. Graceful shutdown helpers

**Threading:** A single `PipelineOrchestrator` thread runs seven stages
sequentially in a loop: ingestion, thumbnails (images then video scenes),
embeddings, NIMA/LAION scoring, face detection, grouping (duplicates, directory
groups, face reassessment), and transcription. Each stage explicitly unloads its
model before the next begins, eliminating GPU contention. Stages query the
database for incomplete rows (e.g. `embedding IS NULL`) so a killed process
resumes where it left off. A scan timer triggers periodic rescans at
configurable intervals. The database connection is shared and protected by
`threading.RLock`.

### `app/faces.py` - Face Detection and Recognition

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

### `app/thumbnails.py` - Thumbnail Generation and Caching

Generates, caches, and manages image thumbnails. Also includes image rotation
utilities (which invalidate thumbnails).

**Components:**

- `generate_thumbnail()` - Generate a single thumbnail with sharpening
- `get_thumbnail_cache_path()` - Compute cache path for a thumbnail
- `generate_missing_thumbnails()` - Bulk generate thumbnails for many images
- `ThumbnailCache` - Thread-safe LRU RAM cache for thumbnail bytes (backed by
  `OrderedDict` for O(1) eviction)
- `rotate_image_file()` - Rotate an image and invalidate its thumbnails

Only two canonical sizes are stored on disk: **200px** and **400px**. The
frontend uses CSS to scale to the exact display size. Cache structure:
`<thumbnail_dir>/<size>/<first2chars>/<checksum>.jpg`.

### `app/duplicates.py` - Duplicate Detection

Finds and groups duplicate or similar images across 6 levels (4 auto-detected
plus 2 named group types):

| Level | Name | Method |
|-------|------|--------|
| 0 | Identical | Same SHA256 checksum |
| 1 | Near-identical | Perceptual hash within Hamming distance threshold |
| 2 | Similar | High OpenCLIP embedding cosine similarity |
| 3 | Related | Lower embedding similarity threshold |
| 4 | Directories | Auto-generated from filesystem folder structure (synced on scan) |
| 5 | Custom | User-curated groups/albums (overlap allowed, persist when empty) |

**Optimisation techniques:** multi-index hashing (LSH) for Level 1 to avoid
O(n^2) comparisons, chunked matrix multiplication for Levels 2-3 to manage
memory, union-find with path compression for efficient clustering, and
incremental updates for small batches of new/modified images.

### `app/caption.py` - Image Captioning

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

### `app/config.py` - Configuration

Loads, saves, and validates configuration from `photonarium.yml` stored at the
OS-appropriate location (Windows: `%LOCALAPPDATA%\Photonarium\`, macOS:
`~/Library/Application Support/Photonarium/`, Linux: `~/.config/photonarium/`).
If no config exists on first run, auto-migrates a legacy `.photonarium.yml` from
the working directory if found, otherwise creates a default with full comments.
The `data_dir` field tells the app where to find its database and thumbnails.

### `app/metadata.py` - EXIF Metadata and Timestamp Extraction

Extracts EXIF metadata and derives timestamps from images. A single-pass EXIF
read produces normalised, human-readable key-value pairs (e.g. "Camera":
"Nikon D850"). The same data is reused for timestamp derivation to avoid
opening the file twice during indexing.

Timestamp priority order:

1. EXIF `DateTimeOriginal` tag (when photo was taken)
2. EXIF `DateTime` tag (when file was last modified by software)
3. Parsed from filename/path (more reliable than filesystem dates)
4. Filesystem creation/modification time

### `app/rawimage.py` - Camera RAW Image Loading

Unified interface for loading both standard image formats (via Pillow) and
camera RAW formats (via rawpy/LibRaw). All image loading goes through
`open_image()` so that RAW files are handled transparently. Returns
fully-decoded PIL Images with EXIF orientation already applied, so callers
no longer need `ImageOps.exif_transpose()`. Also provides
`get_raw_dimensions()` for fast header-only dimension reads without full
demosaicing, and `extract_raw_exif()` using the pure-Python `exifread` library
for RAW EXIF timestamps.

### `app/trash.py` - Trash and Quality Scoring Utilities

Pure utility functions for the trash-based deletion workflow and the composite
quality scoring algorithm used by duplicate pruning. Has no dependency on
ImageDatabase, no threading, and no direct database access - callers pass in
paths and data as arguments. The quality scoring algorithm is a Python port of
the frontend `_computeQualityScores()` in `app/static/appstate/images.js`, ensuring
that the backend prune endpoint ranks images identically to the frontend Quality
sort.

### `app/nima.py` - NIMA Aesthetic Scoring

MobileNetV2-based NIMA (Neural IMage Assessment) model from Talebi & Milanfar
(2018). Predicts a probability distribution over aesthetic ratings 1-10; the
weighted mean serves as the aesthetic score. Uses the pretrained checkpoint from
truskovskiyk/nima.pytorch (MIT licence) trained on the AVA dataset (~255k
images). The MobileNetV2 backbone is lightweight (~9MB) and runs efficiently on
both GPU and CPU. Standalone implementation using only torch and torchvision
(already installed for OpenCLIP and facenet-pytorch).

### `app/video.py` - Video Processing

Video I/O and processing utilities using PyAV (`av`) and ffmpeg-binaries. All
video support flows through this module.

**Capabilities:**

- Video metadata extraction (duration, dimensions, codec, creation time)
- Scene boundary detection via ffmpeg's `select` filter with automatic
  subdivision of long scenes at configurable intervals
- Keyframe thumbnail generation (same sharpening/JPEG pipeline as images)
- Multi-frame extraction per scene for OpenCLIP embedding
- Audio segment extraction for speech-to-text (via faster-whisper)

All functions degrade gracefully if PyAV is not installed — the module-level
`is_video_supported()` check lets callers skip video processing cleanly.

### `download_models.py` - Model Downloader

Standalone script that downloads the required ML models (OpenCLIP, BLIP/BLIP-2,
FaceNet, LAION aesthetic head, NIMA) from HuggingFace and other sources. Run
before first use or after changing model settings.

**Two modes:**

- **Standard mode:** Queries `app/app.py --list-models` for the current
  configuration, then downloads the models specified in `photonarium.yml`.
- **Standalone mode (`--standalone`):** Uses hardcoded default model settings
  without querying app.py. Used for Docker builds where the app isn't available
  yet. The defaults (`ViT-B-32`, `blip-image-captioning-large`) are defined as
  constants at the top of the script and must be kept in sync with
  `app/config.py` if changed.

**Docker build workflow:**

```bash
# Pre-download models to docker/models/ (run once)
HF_HOME=docker/models/huggingface \
TORCH_HOME=docker/models/torch \
python download_models.py --standalone --data-dir docker/models

# Build image (models are COPYed from docker/models/)
make build
```

This decouples the model layer from pip installs and app code, so code changes
don't trigger multi-GB model re-downloads during Docker builds.

---

## Frontend - Screen Modules (`app/static/`)

All frontend files live in the `app/static/` folder. The application is a
single-page app (`index.html`) with screen modules that register with a global
`App` object.

### `core.js` - Application Framework

Central infrastructure that all screen modules depend on. Initialises first and
exposes the global `App` object.

**Responsibilities:**

- **State management** - Current screen, theme (light/dark with localStorage
  persistence), thumbnail size preferences, pub/sub event system for
  cross-module communication.
- **Screen navigation** - Transitions between screens via `data-screen`
  attribute, toolbar group visibility, navigation history for back-button,
  lifecycle hooks (`onEnter`, `onLeave`).
- **API communication** - Wrapper functions for all backend API calls,
  request/response serialisation, error handling.
- **Toolbar management** - Event listeners for common buttons, button state
  updates, per-screen toolbar visibility.
- **Dialog system** - Modal dialogs, confirmation dialogs with Promise-based
  responses, emoji picker.
- **Utilities** - DOM helpers (`App.$(id)`, `App.createElement()`), debounce
  and throttle, image URL builders, date and file size formatting.
- **Module registration** - `App.registerModule()` for screen modules,
  `App.ready()` for post-initialisation logic.

### `thumbnails.js` - Shared Grid Infrastructure

Reusable components for thumbnail grid management, used by Gallery, Duplicates,
and Faces screens.

**Components:**

- **ThumbnailLoader** - Fetches thumbnails with scroll-aware prioritisation.
  Real-time priority based on distance from visible area centre.
  Auto-pruning of requests outside buffer zone. Timeout and scroll-abort
  protection. API: `request()`, `cancel()`, `prioritize()`, `bustCache()`,
  `clear()`.
- **VirtualGrid** - Virtual scrolling with absolute positioning. Only renders
  visible items plus a buffer. Spacer-based layout for correct scroll height.
  RAF-throttled scroll handling. API: `render()`, `refresh()`, `scrollTo()`,
  `scrollToId()`, `bind()`, `unbind()`.
- **GridSelection** - Unified selection handling. Click (single, Ctrl, Shift,
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

1. **Unbind before destroying DOM** - Clearing `innerHTML` without `unbind()`
   first orphans scroll listeners. Always: unbind, destroy, then clear DOM.
2. **Hidden containers have zero dimensions** - Don't call `render()` on hidden
   containers. Set a `needsRefresh` flag instead.
3. **`bind()` triggers `_updateVisibleItems`** - Ensures thumbnails load when
   returning to a screen.
4. **Scroll container must be `config.container`** - VirtualGrid listens on
   this element. Nested scroll containers will cause mismatches.
5. **Blob URL memory leak** - Each thumbnail creates a blob URL that must be
   revoked when the item scrolls out of view or on destroy.
6. **Selection persists across unbind/bind** - Intentional. Call
   `selection.clear()` when recreating a grid with new data.
7. **Keyboard handler is on `document`** - Captures keys globally. Only the
   active grid's selection should be bound.
8. **O(n) lookups during scroll** - Fixed by building an `id -> index` Map once
   per scroll update. Maintain this if modifying `_updateVisibleItems`.

### `gallery.js` - Gallery Screen

Primary view for browsing the image catalogue.

- **Thumbnail grid** via VirtualGrid with dynamic size adjustment
- **Image selection** via GridSelection (single/Ctrl/Shift click, drag-box,
  keyboard navigation)
- **Info panel** with editable Description and Rating fields
- **Sorting** by date, rating, content similarity, or people
- **Filtering** applied from the Search screen
- **Navigation** - double-click/Enter opens fullscreen, scroll position
  preserved on return
- **Deletion** - Delete key with confirmation dialog

### `fullscreen.js` - Fullscreen Viewer

Modal overlay for full-screen image viewing with zoom, pan, and navigation. Not
part of the screen navigation system - it floats over whatever screen is active.

- **Image display** - Full-resolution with fit-to-screen scaling. Overlays
  (close button, filename) fade after 3 seconds and reappear on interaction.
- **Zoom** - Mouse scroll wheel centred on cursor, touch pinch centred on
  midpoint, double-tap toggles fit-to-screen vs 100%.
- **Pan** - Click-and-drag or touch drag when zoomed in, constrained to keep
  image edges visible.
- **Navigation** - Left/Right arrows (wrapping), swipe on touch devices.
- **API** - `open(imageId)`, `close()`, `isOpen()`.

### `database.js` - Database Management Screen

Manage image source folders, import images, and monitor processing status.
Shown by default when the database is empty.

- **Folder management** - List registered folders with image counts, add via
  native folder picker, remove with confirmation. The catalogue folder (if
  configured) is shown with a badge and cannot be removed.
- **Image import** - Drop zone for drag-and-drop import (desktop), file/folder
  picker buttons. Desktop imports send local paths to the backend; mobile
  imports use file upload with preflight name+size dedup to avoid transferring
  files the backend already has. A choice dialog lets desktop users choose
  between "Add Folder" (reference in place) and "Import" (copy into catalogue).
- **Processing status** - Polls backend for indexing, embedding, face
  detection, and import queue sizes.
- **Statistics** - Displays total image count.

### `settings.js` - In-App Configuration Editor

A standalone `Settings` object (not a screen module) that opens a modal dialog
for editing `photonarium.yml` from the browser. The form is entirely
schema-driven - the backend sends field types, constraints, and help text in
one `/api/config/schema` response, and the frontend renders a generic form.

- **Schema-driven** - zero hardcoded knowledge of individual settings.
- **Field types** - text, number, checkbox, textarea (for set-type fields).
- **Danger fields** - red border and warning icon for settings that require care.
- **Client + server validation** - range checks in the browser plus full
  validation on save via the Config constructor.

### `search.js` - Search and Filter Screen

Create filters to narrow down the gallery view.

- **Text search** - Semantic search via OpenCLIP similarity, not just keyword
  matching. Results ranked by relevance.
- **Date range** - Start and/or end date pickers filtering by image timestamp.
- **Rating filter** - Emoji-based filtering with an emoji picker dialog.
- **People filter** - Filter by people detected in images.
- **Validation** - Date range validation, input feedback.
- **Filter lifecycle** - Apply returns to Gallery, Clear resets all fields.
  Filter criteria preserved when navigating away.

### `duplicates.js` - Duplicates Screen

Find and manage duplicate or similar images.

- **Similarity slider** - 6 levels from Custom (user-curated) through
  Directories, Related, Similar, Near-identical, to Identical (strictest).
  Changing the slider immediately recomputes the display.
- **Stack display** - Duplicate groups shown as stacked thumbnail cards sorted
  by group size. The "best" image (highest resolution, best focus, lossless
  preferred) appears on top.
- **Interaction** - Click to select stacks, double-click to open the group in
  Gallery with the best image pre-selected. Keyboard navigation supported.
- **Performance** - Groups pre-computed on backend, cached on frontend for
  quick slider changes. Virtual scrolling via VirtualGrid.

### `faces.js` - Face Tagging

Handles two distinct UI contexts:

**1. Fullscreen tagging mode** - Overlay on the fullscreen image viewer.
Renders bounding boxes over detected faces with inline name input and
autocomplete. Suppress button (X) marks false positives.

**2. Faces screen** - Dedicated screen with three view modes:

| Mode | Description |
|------|-------------|
| `all` | Known people section (static grid of person cards) + unknown faces (VirtualGrid) |
| `unknowns` | Unknown faces only (toolbar toggle) |
| `pick-preferred` | Focus on one person's faces with star icons. Delete key unassigns faces instead of suppressing. |

**Data flow:** `AppState.faces` -> filter by search -> `displayedFaces[]` ->
VirtualGrid. All mutations go through AppState APIs. Refresh flags
(`needsRefresh`, `needsRerender`, `reloadPending`) coordinate updates without
full reloads.

### `videos.js` - Videos Screen

Dedicated screen for browsing and managing video content.

- **Video grid** via VirtualGrid with 16:9 aspect ratio cells, using preferred
  scene thumbnails
- **Scene timeline** with proportionally-sized keyframe thumbnails, timecodes,
  preferred scene stars, and heatmap overlays in search mode
- **Timeline minimap** with time ticks, draggable viewport indicator, and
  heatmap gradient for search results
- **Drag-to-scroll** on the timeline track
- **Sorting** by date, rating, or content similarity (browse mode); match score
  (search mode)
- **Transcriptions** displayed below the timeline when available

### `faceThumbnails.js` - Face Thumbnail Cache-Busting

Manages cache-busting for face thumbnail URLs. When images are modified
(rotation, rescan), face thumbnails are regenerated on the backend. This
utility ensures the frontend fetches fresh versions by appending a timestamp
query parameter.

### `onthisday.js` - "On This Day" Nostalgia Overlay

Standalone object (not a registered screen module, like Settings) that shows
a scattered-photo album overlay when the app starts, if there are photos taken
on today's month/day across multiple years. Triggers after 8+ hours of user
inactivity (screensaver pattern), shows at most once per calendar day
(localStorage gate), and can be disabled via the `on_this_day_enabled` config
option. The aesthetic is intentionally hardcoded (cream paper, sepia tint,
coffee rings, ring binder) and does not follow the light/dark theme toggle.

---

## Frontend - State Management (`app/static/appstate/`)

Central state management split into domain files. All domains attach to the
global `AppState` object created by `core.js`. GUI modules read from AppState,
subscribe to changes, and mutate via AppState methods - they never maintain
local copies of data.

### `core.js` - Transaction System and Utilities

Foundation for the AppState architecture.

- **Subscriber system** - `createSubscriberSystem()` returns `subscribe`,
  `broadcast`, and `notify` for reactive updates.
- **Transaction batching** - `transaction()` batches synchronous cache updates
  so subscribers fire once. `queueTransaction()` serialises async API calls.
- **localStorage helpers** - `storage.get()` / `storage.set()` with JSON
  serialisation.

### `index.js` - Domain Load Order

Documents the required script load order and verifies all domains are present
at startup.

**Load order:**

```
core.js -> view.js -> nav.js -> filter.js -> selection.js -> status.js ->
search.js -> folders.js -> duplicates.js -> identity.js -> images.js ->
videos.js -> loading.js -> events.js -> index.js
```

**Dependencies:**

- `core.js` is the foundation (no dependencies)
- `view`, `nav`, `filter`, `selection` are independent domains
- `identity.js` contains both `faces` and `people` (tightly coupled - a face
  operation may create/delete a person, renaming triggers merge/dissolve)
- `images.js` depends on `duplicates._internal` for cascade delete

### Domain Files

Business logic and core application state in the frontend is handled by `app/static/appstate`. This performs an 'optimistic cacheing' strategy to maintain RAM-based copies of state which may be rolled-back if something goes wrong in the backend that invalidates any optimistic assumptions. It provides mechanisms for subscribers (across the frontend) to be notified via events when key state domains are updated, so (for example) they can refresh.

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
| `videos.js` | videos | Memory | Video browse/search state: search results with per-scene scores, selected video, scene cache, preferred scene management |
| `events.js` | events | N/A | Cursor-based polling of `/api/events` every 2s with stale detection. Dispatches backend events (`faces_reassessed`, `folder_added/removed`, `processing_complete`, `image_ingested`, `nima_complete`, `images_modified`, `import_complete`, `video_processed`, `error`) and multi-client mutation events (`faces_changed`, `people_changed`, `images_changed`, `groups_changed`) to relevant domains via incremental cache updates |

---

## Code Quality

Automated linting, formatting, and static analysis for both Python and JavaScript.

### Tools

| Language | Tool | Config | Purpose |
|----------|------|--------|---------|
| Python | [ruff](https://docs.astral.sh/ruff/) | `tools/ruff.toml` | Linting + formatting |
| JS | [ESLint 9](https://eslint.org/) + [@stylistic](https://eslint.style/) | `tools/eslint.config.mjs` | Linting + formatting |
| JS | [TypeScript](https://www.typescriptlang.org/) `checkJs` | `tools/jsconfig.json` | Type inference on plain JS via JSDoc (IDE support) |

### Quick Reference

```bash
# Python - lint and format
ruff check --config tools/ruff.toml app/              # Check for errors
ruff check --config tools/ruff.toml --fix app/        # Auto-fix safe issues
ruff format --config tools/ruff.toml app/             # Format all Python files
ruff format --config tools/ruff.toml --check app/     # Verify formatting (no changes)

# JavaScript - lint and format
npx --prefix tools eslint --config tools/eslint.config.mjs app/static/        # Check for errors
npx --prefix tools eslint --config tools/eslint.config.mjs --fix app/static/  # Auto-fix safe issues

# TypeScript - type checking (informational, not enforced)
npx --prefix tools tsc --project tools/jsconfig.json --noEmit
```

### Python Rules (ruff)

The linter runs a curated set of rules beyond basic style:

| Rule set | What it catches |
|----------|-----------------|
| `E/W` | pycodestyle errors and warnings |
| `F` | Pyflakes: undefined names, unused imports, redefined variables |
| `I` | isort: import ordering |
| `B` | flake8-bugbear: mutable defaults, unused loop vars, closures over loop vars |
| `SIM` | flake8-simplify: dead code patterns, context managers |
| `UP` | pyupgrade: modernise to Python 3.10+ syntax |
| `S` | bandit: SQL injection, subprocess injection, hardcoded secrets |
| `PLE` | pylint errors: real bugs only |
| `RUF` | ruff-specific: catch-all for Python anti-patterns |

Per-file ignores (in `tools/ruff.toml`) suppress S608 (hardcoded SQL) in database modules
where string-formatted SQL is used for schema names with parameterised value binding.

### JavaScript Rules (ESLint)

- **Error detection**: `no-undef`, `no-dupe-keys`, `no-unreachable`, `valid-typeof`, etc.
- **Unused variables**: Warned (not errored), with `args: 'none'` and `_` prefix exemption.
- **Formatting** via `@stylistic`: 4-space indent, single quotes, semicolons, trailing commas
  in multiline, consistent spacing.

### Automation

A **git pre-commit hook** (`.git/hooks/pre-commit`) blocks commits if staged files have
lint or formatting errors. It prints fix commands on failure.

### Suppressing Rules

```python
# Python: suppress a specific rule on one line
x = eval(expr)  # noqa: S307

# Python: suppress in tools/ruff.toml for an entire file
[lint.per-file-ignores]
"tests/*.py" = ["S101"]
```

```javascript
// JavaScript: suppress a specific rule on the next line
// eslint-disable-next-line no-undef
const x = legacyGlobal;
```

---

## Tutorial Generation (`tools/mktutorial/`)

The interactive tutorial on the Photonarium website is generated from a
Playwright-automated script that drives a real instance of the app, capturing
screenshots at each step. This makes it a useful end-to-end smoke test of the
entire system - backend processing, frontend rendering, and face recognition
all have to work correctly for the tutorial to complete.

### Prerequisites

- A working Photonarium install (venv with all dependencies)
- Playwright for Python: `pip install playwright && playwright install chromium`
- A GPU is strongly recommended (the 499-image demo set takes a long time on CPU)
- The `tools/mktutorial/examples/` folder with the demo image set

### Two-phase workflow

**Phase 1 - Setup** (run once, or when the demo data changes):

```bash
python tools/mktutorial/tutorial.py --setup
```

This initialises the tutorial data directory (`tools/mktutorial/`):

1. Creates a `photonarium.yml` config pointing at the mktutorial directory
2. Downloads ML model weights (LAION aesthetic head, NIMA) into the directory
3. Starts a server against an empty database on port 5111
4. Captures the Getting Started screenshots (light/dark theme, folder picker
   composite, indexing, processing) into `tools/mktutorial/setup-cache/`
5. Adds the demo image folder and waits for the full processing pipeline
   (indexing, CLIP embeddings, face detection, NIMA scoring) to complete
6. Stops the server

The generated artifacts (DB, thumbnails, model files, setup screenshots) are
gitignored - they are large and machine-specific.

**Phase 2 - Capture** (run to regenerate tutorial screenshots):

```bash
python tools/mktutorial/tutorial.py
```

This starts a fresh server from the setup data, opens a headless Chromium
browser, and walks through every tutorial step - navigating screens, clicking
buttons, typing text, identifying faces - capturing a screenshot after each
action. Output goes to `generated/` at the project root:

- `generated/screenshots/` - all captured screenshots (0-1.png through 9-2.png)
- `generated/manual/` - mobile screenshots (copied, not captured)
- `generated/index.html` - the tutorial HTML page with embedded step text

Useful flags:

```bash
# Capture only specific sections (0-indexed)
python tools/mktutorial/tutorial.py --from-section 6 --to-section 6

# Continue from a specific section (skips earlier ones)
python tools/mktutorial/tutorial.py --from-section 4
```

### Publishing to the website

After a successful capture, copy the output into the website directory:

```bash
# Copy screenshots (replaces existing ones)
cp -r generated/screenshots/* www/tutorial/screenshots/

# Copy the tutorial page
cp generated/index.html www/tutorial/index.html
```

The `www/tutorial/manual/` directory contains mobile screenshots that are taken
by hand (not automated). These only need updating if the mobile UI changes.

### Deterministic face identification

The face tutorial section (section 6) uses deterministic helpers that identify
faces by their source image filename and bounding box position (left/right)
rather than grid position. This makes the tutorial resilient to changes in
ingestion order. The expected face detections are defined in `_FACE_IMAGES` at
the top of the step definitions.

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
15. Ensure key documents are kept up-to-date (`README.md` and `docs/develop.md`) and GUI elements have helpful, non-technical tooltips (`title` strings).
16. Schema changes need proper SQLite migrations so existing databases aren't broken on upgrade.
17. Avoid adding new dependencies without strong justification, prefer stdlib/existing dependencies.
18. Handle low-memory/OOM conditions gracefully. Model loads, batch inference, and large allocations must catch `MemoryError`/`RuntimeError` and degrade (retry with a smaller batch, skip, or disable the feature) rather than crash the processing thread.
