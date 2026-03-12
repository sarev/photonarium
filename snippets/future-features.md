# Future Feature Candidates

Features that competitors offer which Photonarium currently lacks, prioritised by user impact and alignment with Photonarium's local-first, privacy-first philosophy. Editing features (crop, filters, colour grading) are deliberately excluded — dedicated editors like darktable, Lightroom, and GIMP serve that need far better than a catalogue tool ever will.

Items marked **CROSS** have been ruled out (see inline reasons). Unmarked items remain candidates.

---

## Worthy concepts

### 1. Batch Metadata Editing

**Who has it:** PhotoPrism, digiKam, Lightroom

**What it is:** Select multiple photos and edit their metadata (description, rating, date, keywords) in one operation.

**Why compelling:** Currently each image's metadata must be edited individually. Batch operations are a core Photonarium principle for faces and groups — extending this to metadata is consistent.

**Complexity:** Medium-low. Backend already supports batch mutations; needs a UI for multi-select metadata editing.

**Notes:** This is 100% aligned with the UX intent of Photonarium: it's a no-brainer.

---

### 2. Map / Geolocation View

**Who has it:** PhotoPrism, Immich, digiKam, Lightroom, Apple, Google, Synology, Mylio — it's nearly universal.

**What it is:** Browse photos on a map using GPS coordinates from EXIF. Cluster markers at zoom levels. Click a cluster to see those photos.

**Why compelling:** A fundamentally different way to explore a library — "where was this?" is as natural a question as "when was this?". Photonarium already extracts EXIF data including GPS coordinates; the missing piece is the map renderer and clustering logic.

**Offline map approach — Natural Earth 1:10m:**

The key challenge is offline map tiles. The recommended approach is [Natural Earth](https://www.naturalearthdata.com/) at 1:10,000,000 scale:

- **Free, public domain** vector dataset (no licence concerns for Apache-2.0)
- **Coverage:** Country boundaries, coastlines, lakes, rivers, ~7,000 populated places (all world capitals, major cities, significant towns)
- **Size:** ~30-50MB as TopoJSON/GeoJSON — trivial alongside the ML models
- **Rendering:** Client-side with [Leaflet](https://leafletjs.com/) (lightweight, well-maintained, MIT-licensed) or D3
- **Detail level:** Continent → country → city zoom levels. No streets or terrain, but sufficient to answer "where was this taken?" and to browse by region
- **No tile server required** — vector data loaded directly into the browser

The implementation would involve:
1. Bundle Natural Earth data (countries, places, coastlines) as TopoJSON in `app/static/`
2. Add Leaflet as a frontend dependency (~40KB gzipped)
3. Extract GPS coordinates from EXIF (already parsed in `metadata.py`)
4. Store lat/lng in the database (new columns or reuse `exif_data` JSON)
5. Map screen with marker clustering (Leaflet.markercluster plugin, MIT-licensed)
6. Click a cluster/marker to filter the Gallery to those images
7. Optional: reverse geocoding from Natural Earth place data (nearest city/country label)

**Future upgrade path:** Could later support [Protomaps PMTiles](https://protomaps.com/) for higher-detail offline maps (~500MB-1GB for a boundaries+labels extract at zoom 0-10), or optional online tile fetching from OpenStreetMap when the user opts in.

**Complexity:** Medium.

---

### 3. Geo-Tagging (Manual)

**Who has it:** digiKam, Mylio, Lightroom

**What it is:** Drag photos onto a map to assign GPS coordinates. Useful for photos without GPS data (older cameras, scans).

**Why compelling:** Natural companion to the map view. Many older photos lack GPS — manual tagging is the only way to place them.

**Complexity:** Medium. Depends on having the map view (item 2) first.

**Notes:** if we do the map, then we also do this in the most elegant and natural way we can manage.

---

### 4. Export

**Who has it:** digiKam, Lightroom, Google, Mylio

**What it is:** Export selected images in specific formats/sizes, or design photo books/calendars.

**Why compelling:** Basic export (resize, format convert, zip download) is low-effort and useful. Photo books/calendars are high-effort and niche.

**Complexity:** Low for basic export; high for books/calendars.

**Notes:** Not overly bothered about printing support (other tools can do that better) but export makes a lot of sense, especially exporting with constraints (e.g. full-res, normal, compact). Not interested in bloating with designing photo books/calendars, etc. - there are websites for that.

---

### 5. XMP Sidecar Support

**Who has it:** digiKam, Lightroom, darktable

**What it is:** Read/write metadata (ratings, keywords, descriptions) to XMP sidecar files alongside images, not just in the database.

**Why compelling:** Makes Photonarium's metadata portable. If someone switches tools, their ratings and descriptions travel with the files. Enables round-tripping with Lightroom/digiKam. Critical for the "your data is yours" philosophy.

**Complexity:** Medium. Reading is easy; writing safely (especially for RAW files) needs care.

**Notes:** Sounds worth adding as an export tick-box, if we're doing exports.

---

### 6. Deduplication with Merge

**Who has it:** Mylio

**What it is:** When removing duplicates, merge metadata (ratings, tags, descriptions) from the duplicate into the keeper rather than losing it.

**Why compelling:** Currently, trashing a duplicate loses any metadata the user attached to it. Merging preserves that work.

**Complexity:** Low-medium. Logic to merge fields before trash.

**Notes:** Likely to need careful consideration of what gets merged, how it gets merged, how to resolve conflicts, etc.

---

### 7. Multi-User Support

**Who has it:** Immich, PhotoPrism (paid), Piwigo, Synology, QNAP, Google, Apple

**What it is:** Separate user accounts with private libraries, shared spaces, or role-based access.

**Why compelling:** The single biggest blocker for household adoption. Even a minimal version (2-3 users, each sees their own ratings/groups but shares the same photo pool) would be transformative. Also a prerequisite for sharing features.

**Complexity:** Very high. Touches auth, per-user state, API authorisation, and the entire frontend state model. Requires complete change of database technology (away from SQLite).

---

### 8. Pet Recognition

**Who has it:** Apple, Google

**What it is:** Extend face recognition to detect and recognise pets (dogs, cats).

**Why compelling:** Many people have more photos of their pets than of most humans they know. Would need a separate detection model since MTCNN is human-face-specific, but could piggyback on existing face infrastructure for the recognition/naming workflow.

**Complexity:** Medium-high. Separate detection model, different embedding approach.

**Notes:** If there's an obvious model that can help with this, I'm interested. Would probably merge with the Faces/People side of the app (People & Animals) rather than bolting an effectively duplicate screen into the app.

---

### 9. PWA / Installable Web App

**Who has it:** PhotoPrism

**What it is:** A `manifest.json` + service worker that lets browsers install the web UI as a standalone app with its own icon and window.

**Why compelling:** Near-zero effort for a significantly more polished feel, especially on mobile and tablets. No app store needed. The UI is already a full SPA — it's 90% of the way there.

**Complexity:** Low.

**Notes:** Don't really understand this one, but sounds interesting.

---

## Rejected concepts

### 10. Object / Scene Auto-Tagging **CROSS**

**Who has it:** PhotoPrism, Immich, digiKam, Apple, Google, Synology, Mylio

**What it is:** Automatically label images with keywords ("dog", "beach", "car", "food") as persistent, browsable tags. Beyond what CLIP search does — CLIP finds things *when you ask*, but auto-tags let you *discover* what's in your library without knowing what to search for.

**Why compelling:** "Show me all categories" is a browsing mode that CLIP search can't serve. Could potentially reuse existing CLIP embeddings with a classification head rather than adding a new model. digiKam's approach (adjustable confidence threshold per tag) is worth studying.

**Complexity:** Medium.

**Reject reason:** Unnecessary complexity. With a strong search and smart groups, this feature is just bloat. 

---

### 11. OCR / Text in Images **CROSS**

**Who has it:** Google Lens, Mylio, UGREEN

**What it is:** Detect and index text visible in photos (signs, documents, whiteboards). Searchable.

**Why compelling:** Niche but powerful. "Find the photo of that whiteboard from the meeting" or "the photo with the street sign".

**Complexity:** Medium. Tesseract or PaddleOCR, runs offline, stores extracted text for search.

**Reject reason:** CLIP seems to somehow do this in a basic way. More sophisticated OCR is too specialised a use case.

---

### 12. Calendar / Life Events Integration **CROSS**

**Who has it:** Mylio (unique)

**What it is:** Link photos to calendar events. Import from Google/iCloud/Outlook calendars to auto-populate event labels on the photo timeline.

**Why compelling:** "Photos from Sarah's birthday party" without manual tagging. Distinctive feature nobody else has copied.

**Complexity:** Medium-high. External calendar APIs (or ICS file import), event-to-photo matching heuristics.

**Reject reason:** Connetcing to online services falls outside the core values of this project.

---

### 13. Keyword / Tag Hierarchy **CROSS**

**Who has it:** digiKam, Lightroom

**What it is:** Structured tag trees (Animals > Dogs > Labrador) rather than flat tags. Searching for "Animals" returns everything in the subtree.

**Why compelling:** Scales better than flat tags for large, well-organised libraries. Natural fit with auto-tagging (item 2).

**Complexity:** Medium. Schema change, tree UI, inheritance logic.

**Reject reason:** Unnecessary UX complexity.

---

### 14. LLM-Powered Captioning (Local) **CROSS**

**Who has it:** PhotoPrism (via Ollama), Google (Gemini)

**What it is:** Use a local LLM (Ollama, llama.cpp) instead of or alongside BLIP for richer, more descriptive captions.

**Why compelling:** BLIP captions are short and formulaic ("a dog sitting on a beach"). A 7B-parameter local LLM can produce genuinely descriptive paragraphs that dramatically improve search recall. PhotoPrism's Ollama integration proves this works self-hosted. Fits the offline-first philosophy perfectly.

**Complexity:** Medium. Ollama integration is straightforward; the challenge is making it optional and handling the resource requirements gracefully.

**Reject reason:** Photonarium already offers a range of larger, more effective captioning models. I don't think the LLM is the solution that users will want to improving these; it's including information like where the photo was taken, what the event was about (if any), and who is doing what in the photo, etc. An LLM cannot magic that up. 

---

### 15. Sharing via Links **CROSS**

**Who has it:** PhotoPrism, Immich, Piwigo, Lychee, Synology

**What it is:** Generate a secret URL for an album/group that anyone can view without an account. Optional password, optional expiry.

**Why compelling:** "Look at these holiday photos" is one of the most common photo tasks. Currently impossible without giving someone full access to the Photonarium instance. Doesn't require full multi-user — just a read-only view behind a token.

**Complexity:** Medium-low. A new route serving a stripped-down gallery for a token-authenticated group.

**Reject reason:** Photonarium is not intended to be accessible outside the local network. Links like this won't work. 

---

### 16. RAW + JPEG Stacking **CROSS**

**Who has it:** digiKam, LibrePhotos, Lightroom

**What it is:** Automatically pair RAW and JPEG files of the same shot, treat as one item with variants. Show the JPEG by default, access the RAW when needed.

**Why compelling:** Serious photographers shoot RAW+JPEG. Without stacking, every shot appears twice in the library, polluting duplicates and cluttering the Gallery. A specialised, more useful form of duplicate detection.

**Complexity:** Medium. Matching logic (same basename, same timestamp) plus UI to show/switch variants.

**Reject reason:** Too duplicative of the various grouping levels we already have in the Groups screen. 
