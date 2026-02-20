# Release Notes

## v1.0.9-beta.9

### Import into Catalogue

A new Import feature lets you copy images from external sources (SD cards, phone uploads, downloads) into a Photonarium-managed catalogue directory, organised by date. Set `catalogue_dir` in settings to enable it.

- **Date-based organisation:** Imported files are stored as `catalogue_dir/YYYY/YYYY-MM-DD/filename.jpg` using the EXIF timestamp (or file modification time as a fallback).
- **Desktop import:** Drag and drop files or folders onto the import drop zone on the Database screen, or use the Pick Folder / Pick Photos buttons. When dropping a folder, a choice dialog lets you reference it in place (Add Folder) or copy its contents into the catalogue (Import).
- **Mobile import:** Pick Photos opens the system photo picker (iOS Camera Roll, Android Files). On Android, a Pick Folder button is also available via the `webkitdirectory` API.
- **Preflight dedup:** Before uploading from a browser, file names and sizes are sent to the backend for a fast duplicate check against existing images. Only new files are transferred, saving bandwidth when re-importing folders that partially overlap with the existing library. The backend's SHA-256 dedup in the ImportWorker catches any edge cases that slip through.
- **Backend dedup:** The ImportWorker also checks checksums server-side (for the desktop path and as a safety net for uploads), skipping files already in the library.
- **ImportWorker:** Modeled on TrashWorker -- daemon thread, queue-fed with `ThreadPoolExecutor` for parallel file copying (`import_threads` setting, 1-16, default 4). Progress is shown on the Database screen. Unfinished items are persisted to `.pending_import.json` on shutdown and recovered on restart.
- **Auto-registration:** The catalogue directory is automatically registered as a watched folder. It is shown with a catalogue badge and cannot be removed from the folder list.
- **No new dependencies:** Uses stdlib `shutil.copy2` and existing `hashlib`.

### Refine Groups

The old Prune feature (levels 0-3 only, trash-only) has been replaced by a general-purpose Refine Groups tool available at all 6 group levels (including directories and custom groups).

- **Quality filtering:** Choose how many images to keep (or trash) per group - best only, top N, or top N%. Uses the same composite quality scoring as the Gallery Quality sort.
- **View in Gallery:** The primary action opens the selected subset (best or worst images) as a filtered Gallery view without changing anything. Works for all group levels including directories and custom/smart groups.
- **Trash:** An optional secondary action (levels 0-4) moves the non-kept images to the trash directory. Custom groups and smart groups are view-only since their membership is user-curated or dynamically evaluated.
- **Smart group support:** For smart groups, the refine dialog resolves filter criteria asynchronously (including live semantic search if needed) and caches the resulting image IDs to avoid double evaluation.

### Gallery Slideshow Buttons

The Gallery toolbar now has slideshow (play) and shuffle buttons, in addition to the existing fullscreen toolbar buttons. The buttons are selection-aware:

- **No selection:** Plays all images in the current sort order.
- **Single selection:** Starts the slideshow from that image.
- **Multiple selection:** Plays only the selected images.

### Apple MPS Device Support

PyTorch device selection now detects Apple Silicon GPU acceleration (MPS) in addition to NVIDIA CUDA. All four ML model sites (OpenCLIP embeddings, NIMA aesthetic scoring, face detection, and image captioning) use the priority order CUDA > MPS > CPU. The BLIP/BLIP-2 captioning models also use float16 precision on MPS (previously only on CUDA), improving performance on Apple Silicon.

### Repository Restructure

The project source tree has been reorganized into two top-level directories for cleaner separation of application code and development tooling:

- **`app/`**: Python backend (app.py, imagedb.py, faces.py, etc.) and the frontend (`app/static/`).
- **`tools/`**: Development and CI/CD tooling (ruff.toml, eslint.config.mjs, jsconfig.json, package.json, compat-test/, mktutorial/).

The application is now launched with `python app/app.py` instead of `python app.py`. Data files (database, thumbnails, models, trash) continue to live in the OS data directory, not the repository root. The installer scripts have been updated accordingly.

### Tutorial Automation

The tutorial generator (`tools/mktutorial/tutorial.py`) now supports a `--setup` mode that automates the initial data preparation -- replacing manual steps that previously had to be done before tutorial generation:

- **Automated setup:** `--setup` creates the tutorial config, downloads ML models, starts the server against an empty database, captures the Getting Started screenshots via Playwright, adds the example image folder, and waits for all image processing (indexing, embeddings, faces, duplicates, NIMA scoring) to complete.
- **Composite screenshots:** The OS folder picker dialog (which cannot be automated as a native widget) is composited from a manually-provided overlay onto an automatically-captured background screenshot using Pillow.
- **Deterministic face identification:** Section 6 (Faces) now uses stable selectors based on image filenames and face bounding-box positions rather than DOM order, making the face identification steps reproducible across different processing orders.

### Bug Fixes

- **People disappearing from Known People section:** When the originating client received its own mutation events back via event polling, `handleFacesChanged` called `people.invalidate()` which triggered a full cache reload - wiping the optimistic face counts that had just been set. Additionally, `autoUpsert` in the people handler overwrote the optimistic `face_count` with a stale backend snapshot (captured at person creation time, before faces were assigned). Fixed by replacing cache invalidation with incremental reconciliation and stripping the derived `face_count` field from backend event payloads.
- **Orphaned faces after trashing images:** Trashing images only soft-deleted them (`deleted = 1`) without cleaning up associated face records, because the CASCADE DELETE foreign key only fires on hard `DELETE` statements. Orphaned faces continued to appear on the Faces screen, in people's face lists, in auto-recognition, and in semantic search. Fixed by hard-deleting face records during trash, adding `deleted = 0` filters to all face queries, deleting orphaned face thumbnails, and emitting face/people change events for multi-client sync. When the trashed image contained a person's preferred face, the replacement is now chosen by embedding similarity to the old preferred rather than arbitrarily.
- **Quick Match popup jumping to top-left corner:** When a face reassessment event triggered a grid refresh while the Quick Match popup's async match fetch was in flight, the anchor element was detached from the DOM. The subsequent repositioning call read `getBoundingClientRect()` on the detached element (returning zeros), jumping the card to the top-left corner.
- **Stale person thumbnails in Quick Match results:** The Quick Match popup used a raw thumbnail URL without cache-busting, so after changing a person's preferred face, the popup continued to show the old face until the browser cache expired.
- **Face auto-matching never worked:** Three bugs prevented automatic face recognition from assigning detected faces to known people. First, detection-time matching used only the global threshold, ignoring per-person thresholds (so a person with a relaxed 70% threshold was still held to the global 92%). Second, both matching paths used a single-best-match approach -- if the closest known face belonged to a person whose threshold was not met, no fallback was tried, even when other people would have matched. Third, faces belonging to the ignored person ('-') could "steal" the best match from named people. Fixed by grouping matches by person, trying each in descending similarity order against their per-person threshold, and partitioning named people before ignored so named matches are always preferred.
- **Gallery not refreshing after import:** The Gallery only updated at `processing_complete`, which fires after face detection and duplicate grouping -- often 1-2 minutes after images were actually ready to view. Added an `images_indexed` event that fires as soon as embeddings complete, so imported or newly scanned images appear in the Gallery within seconds.

### Improvements

- **Quick Match similarity scores:** The Quick Match popup now shows match confidence as a percentage next to each person name (e.g. "Alice (83%)"), making it easier to judge match quality.
- **Faster face reassessment after scanning:** Face reassessment (auto-matching unknown faces to known people) now runs immediately after face detection, before the slower duplicate grouping phase. Previously it ran last, adding an unnecessary delay before newly scanned faces were identified.
- **Gallery trash button:** A toolbar button for moving selected images to trash, making deletion accessible on mobile devices where the Delete key is unavailable. The button is disabled when no images are selected.

## v1.0.8-beta.8

### Slideshow Mode

The full-screen viewer now supports slideshows with smooth cross-fade transitions between images.

- **Two playback modes:** Linear (in the current sort order) or shuffled (Fisher-Yates random).
- **Toolbar and keyboard:** Play/shuffle buttons in the full-screen toolbar, or press Space to start. Space pauses/resumes, Escape exits, arrow keys skip manually.
- **Configurable timing:** The hold duration defaults to 5 seconds and can be changed via the `slideshow_interval` setting in `photonarium.yml` (1-60 seconds).
- **Groups integration:** Hover over any group stack on the Groups screen to reveal play and shuffle badges that jump straight into a slideshow scoped to that group.
- **Preloading:** The next image is preloaded during the hold period so it is browser-cached before the cross-fade begins, eliminating flashes of black or stale images. In shuffle mode, the actual shuffle-next target is preloaded (not just the index-adjacent image).

### Smart Groups

Smart Groups are saved searches with dynamic membership. Instead of manually adding images to a group, you define filter criteria (text, date range, rating, people, metadata) and Photonarium evaluates them each time you open the group - so new photos that match your criteria appear automatically.

- **Create from Search:** Set up filters on the Search screen and click "Save as Smart Group". Enter a name and the group appears alongside your regular custom groups.
- **Dynamic evaluation:** Opening a Smart Group runs a fresh filter evaluation. If the filter includes a text search, this runs a live semantic search each time.
- **Edit in place:** An edit badge appears on hover. Click it to return to the Search screen with the saved criteria pre-loaded, modify them, and click "Update Smart Group".
- **Preview thumbnails:** Smart Group stacks show a representative thumbnail that updates automatically. Trashing the preview image triggers a fresh evaluation to pick a new one.
- **Group picker exclusion:** The Gallery's "Add to Group" dialog only shows regular groups, since adding static images to a dynamic group does not make sense.
- **Schema:** Adds `filter_json` and `preview_image_id` columns to `custom_groups` via migration. Existing custom groups are unaffected (both columns are NULL for them).

### Fullscreen Sync with External Deletions

The full-screen viewer now subscribes to image changes from AppState, so images trashed by other clients (or other browser tabs) are pruned from the navigation list in real time. If the currently-displayed image is trashed externally, fullscreen closes immediately. Slideshow state (position, shuffle order) is rebuilt after pruning.

### Search UX Improvements

- **People filter moved up:** The People section now sits directly below Description on the Search screen, making it harder to overlook.
- **Name-only warning:** A warning triangle appears inside the description input when it contains only recognised people names with no other descriptive text. This catches cases where the semantic search would receive an empty query after name extraction.

### On This Day Improvements

- **Full context in Gallery:** "View in Gallery" now shows all images matching the month and day across all years, not just the cherry-picked highlights from the album. This gives wider context around the photos you just saw.

### Damaged Smart Group Detection

Smart Groups that reference people in their filter criteria are now tracked for staleness. When a person is deleted (from any path - direct deletion, merge, dissolve, face cleanup, etc.), any Smart Group whose filter references that person is flagged as "damaged". A warning icon and amber label appear on the group's stack in the Groups screen, with a tooltip explaining the issue. Opening a damaged group still works (it just skips the missing person), and editing the filter clears the damage automatically since stale person references are pruned on load.

### Bug Fixes

- **Stale people in Search filter:** Deleting a person on one client left stale references in the Search screen's people chips, auto-added tracking set, and active filter on other clients. The people subscriber now prunes deleted people automatically.
- **Gallery not showing new images after rescan:** The Gallery skipped its delta sync when no explicit refresh was requested, so newly scanned images only appeared after a full page reload.
- **Face auto-recognition broken:** Async face reassessment crashed on every run with a Row attribute error, so automatic spread of face identities to other photos never worked. Additionally, sync reassessment during scanning emitted incomplete event payloads, so faces detected during a rescan were never auto-identified against known people.
- **Rotation left temp files in photo directories:** Image rotation used the photo's own directory for temporary files. Cloud sync services (Dropbox, OneDrive) would pick these up before the rename completed, creating orphaned 0-byte files. Temp files are now written to the system temp directory instead.
- **OpenCLIP thread-safety race:** Concurrent requests could hit a window where the CLIP model was loaded but the tokenizer was not yet initialised, causing smart group preview evaluation and search to crash. Fixed with double-checked locking.

## v1.0.7-beta.7

### "On This Day..." Photo Album

When you return to Photonarium after a long absence (8+ hours), it checks whether any photos in your library were taken on today's date across multiple years. If so, a nostalgic album overlay fades in - scattered photos on textured paper with a wire ring binder and coffee ring stain. You can dismiss it or click "View in Gallery" to see just those images as a filtered set. The album shows at most once per calendar day and can be disabled via the `on_this_day_enabled` config option.

### Offline-Safe Icons

Material Symbols icons now degrade gracefully when the Google font has not loaded (e.g. on a fresh install with no internet). A new `App.icon()` helper renders Unicode fallback glyphs that are upgraded to the real font icons once the stylesheet loads. This replaces 20+ sites that previously showed blank squares when offline.

### Consolidated Reveal Endpoint

The two separate "reveal in file manager" endpoints have been merged into a single `POST /api/reveal` that accepts a target parameter (`image`, `config`, or `trash`). The Database screen now shows a clickable "Trashed" stat that opens the trash directory directly.

### Bug Fixes

- **Selection lost when clearing a group filter:** Clearing a filter (e.g. leaving a duplicate group) was wiping the gallery selection. The selection is now preserved and the gallery scrolls back to the selected image.
- **Improved settings tooltips:** The vague "may require reconfiguration" warning on dangerous settings fields has been replaced with specific guidance - `data_dir` warns about losing the database and thumbnails, `server_port` notes that bookmarks and open tabs will need updating.

## v1.0.6-beta.6

### Async Trash System

Image trashing has been reworked from synchronous file moves to an asynchronous background worker with parallel I/O:

- **TrashWorker:** Images are soft-deleted immediately (removed from the UI) and file moves run in a background thread using a configurable thread pool (`trash_threads` setting, 1-32, default 8).
- **Live progress:** The Database screen shows a progress row while trash moves are in flight.
- **Crash recovery:** Unfinished queue items are persisted to `.pending_trash.json` on shutdown and recovered on next startup, so no files are lost.
- **Multi-client notifications:** Trashing now emits `groups_changed` events for every affected duplicate level, so other browser tabs see dissolved groups and updated counts immediately.

### Prune Dialog: Keep/Trash Toggle

The prune dialog (Groups screen) now supports both "keep the best" and "trash the worst" semantics:

- **Clickable legend:** The "Keep per group" heading is now a toggle button. Click it to switch to "Trash per group" mode and back.
- **Inverted labels:** In trash mode, "Best image only" becomes "Worst image only", "Top N" becomes "Bottom N", etc.
- **Always keeps one:** Trash mode never removes every image from a group - at least one is always kept.
- **API:** The backend accepts `trash_count`/`trash_percent` as alternatives to `keep_count`/`keep_percent`, with mutual exclusion validation.

### Smart People Detection in Search

Typing a known person's name in the search description field now automatically adds them as a People filter chip:

- **Greedy matching:** Multi-word names like "Mary Jane" are preferred over shorter overlapping matches like "Mary" + "Jane".
- **Non-destructive:** Detected names are stripped from the CLIP search query at apply time without altering the text you typed, so the full description is preserved when you return to the Search screen.
- **Manual override:** Auto-detected chips coexist with manually-picked people from the People Picker. Removing an auto-detected chip and re-typing the name won't re-add it.

### Internationalisation Tools

- **String extraction/injection:** New `extract_strings.py` and `apply_strings.py` scripts in `demo-seed/` support round-tripping translatable tutorial title/caption strings for localisation.
- **Translated tutorials:** Tutorial scripts for Spanish, French, and Japanese are now included.

### Bug Fixes

- **Trashing didn't update groups on other clients:** Previously, trashing images only emitted `images_changed` events. Other browser tabs would see the images disappear but group counts wouldn't update and dissolved groups would linger until a manual refresh. Now `groups_changed` is emitted for every affected duplicate level.
- **Folder removal didn't update groups:** Removing a folder invalidated duplicate groups internally but never emitted group change events, so other clients' Groups screens would show stale data.
- **Trash progress race condition:** The trash progress dict was read and written without a lock, which could cause inconsistent progress display under concurrent trash operations.
- **Landscape info panel:** On mobile in landscape orientation, the info panel is now restored to the right side of the gallery (instead of stacking below) where vertical space is scarce and width is ample.
- **Duplicate status on restart:** The duplicate detection status no longer shows stale "Waiting to compute..." when a prior epoch already exists.
- **Tutorial reliability:** Slider interactions in the tutorial now use real click events instead of programmatic dispatch, and unnecessary screen navigation detours have been removed.

## v1.0.5-beta.5

### Unicode Path Support

Photonarium now correctly handles image folders with non-ASCII names - Japanese, Chinese, Korean, Cyrillic, Arabic, accented European characters, emoji, etc. Previously, images inside folders like `Google Photos (Japanese)` would fail to index because OpenCV's C++ file I/O layer garbles Unicode paths on Windows.

- **OpenCV fix:** Image loading for Laplacian sharpness calculation now reads files through Python's Unicode-aware I/O layer instead of passing paths directly to OpenCV's C++ `imread()`, which silently corrupts non-ASCII characters.
- **Console encoding fix:** Python on Windows defaults console output to the system code page (often cp1252 on Western systems). Any log message containing a non-ASCII path would either print garbled text or crash with `UnicodeEncodeError`. The application now forces UTF-8 on stdout/stderr at startup.

### Code Quality

A linting, formatting, and static analysis toolchain has been added to catch bugs earlier and maintain consistent style:

- **Python:** [ruff](https://docs.astral.sh/ruff/) for linting (pyflakes, pycodestyle, bugbear, bandit security rules, and more) and formatting. [vulture](https://github.com/jendrikseipp/vulture) for cross-file dead code detection (on-demand).
- **JavaScript:** [ESLint 9](https://eslint.org/) with [@stylistic](https://eslint.style/) for linting and formatting. [TypeScript checkJs](https://www.typescriptlang.org/tsconfig/#checkJs) for IDE type inference on plain JS.
- **Git pre-commit hook:** Blocks commits with lint or formatting errors in staged files.

The initial pass caught three real bugs and removed ~2,700 lines of confirmed dead code:

- **Broken Delete key on Faces screen:** The keyboard handler referenced a function that had been renamed, so pressing Delete on unknown faces did nothing.
- **Duplicate method in Search:** Two `_fuzzyMatch` methods in the same object literal - the first was silently overwritten by the second.
- **Caption closure bug:** A loop variable captured by reference in image captioning could theoretically produce wrong substitutions.

### Bug Fixes

- **Tutorial screenshots:** The tutorial viewport has been widened to prevent the info panel from auto-collapsing, which was causing several tutorial steps to fail.

## v1.0.4-beta.4

### Installation Flexibility

The installer is now more accommodating of real-world setups - different Python versions, different NVIDIA drivers, and machines with no GPU at all.

- **Python 3.10+:** The minimum Python version has been lowered from 3.11 to 3.10. Ubuntu 22.04 LTS ships Python 3.10, and Python 3.13 (the latest release) is also fully supported. All combinations have been tested with a compatibility matrix covering Python 3.10, 3.11, and 3.13.
- **CUDA auto-detection:** The installer now runs `nvidia-smi` to detect your GPU's CUDA version and installs the matching PyTorch build automatically - CUDA 11.x gets `cu118`, CUDA 12.x+ gets `cu124`, and machines with no NVIDIA GPU get the CPU-only build. Previously it always tried `cu124` and fell back to CPU if that failed, missing users with CUDA 11.x entirely.
- **macOS MPS:** Documentation now recognises Apple MPS acceleration alongside NVIDIA CUDA. macOS installs use the default PyPI torch build, which includes MPS support on Apple Silicon.
- **Cleaner install output:** The `facenet-pytorch` package (which declares overly strict version bounds on torch, numpy, and Pillow) is now installed last with its pip warnings suppressed. Previously it produced four alarming-looking ERROR lines mid-install that were harmless but confusing.
- **Transformers unpinned:** The `transformers==4.44.*` version pin has been removed. It was originally added as a precaution against BLIP-2 API changes, but testing confirmed that current transformers (5.x) works fine with both BLIP and BLIP-2. More importantly, the pin actively broke Python 3.13 installs because the old `tokenizers` version it pulled in has no Python 3.13 wheel.

### Tutorial

- **Touch swipe navigation:** The interactive tutorial now supports swipe gestures - swipe left to advance, right to go back, up to return to the menu. Swiping right on the first slide returns to the menu.
- **Mobile-friendly help text:** On touch devices, the tutorial shows swipe instructions instead of keyboard shortcuts.
- **Improved readability:** The tutorial menu now has better contrast, a brand gradient on the heading, and a darker overlay background.
- **Updated screenshots:** Tutorial screenshots have been refreshed to reflect the current UI.
- **Mobile showcase:** The website now includes a section showing Photonarium on mobile devices.

## v1.0.3-beta.3

### Multi-Client Support

Multiple browser tabs or devices on the same network can now use Photonarium at the same time. Changes made on one client - naming a face, rating an image, creating a group, trashing a photo - are automatically pushed to every other open client within a couple of seconds.

- **Cursor-based event polling:** Each client tracks its own position in the event stream, so events are never lost when multiple clients are polling.
- **Mutation broadcasting:** User-initiated changes (face assignments, people edits, image ratings, group modifications) emit events that other clients pick up and apply incrementally - no full reload needed.
- **Stale client recovery:** If a client falls too far behind (e.g. a laptop lid was closed), it detects the gap and silently reloads all data to catch up.
- **Offline detection:** If the backend becomes unreachable, mutations are blocked with a warning message until the connection is restored, preventing changes from being silently lost.
- **Concurrency fix:** Custom group operations are now properly serialized, preventing data corruption when two clients modify groups at the same time.

### Mobile & Responsive

- **Hamburger menu:** On narrow screens (<=768px), the toolbar collapses into a compact bar showing the logo, screen title, theme toggle, and a hamburger button. Tapping the hamburger reveals the full toolbar controls as a vertical dropdown; tapping outside or navigating to a different screen closes it. The menu auto-closes when resizing back to desktop width.
- **Collapsible info panel:** A toggle button at the edge of the gallery info panel lets you collapse it to reclaim horizontal space. The panel auto-collapses when it would take more than 20% of the viewport width (e.g. on narrow windows or tablets). Once you explicitly toggle the panel, auto-collapse stops overriding your choice. The preference persists across sessions.
- **Dynamic viewport height:** The app and mobile info panel now use `dvh` units (with `vh` fallback) so they correctly resize when the mobile browser address bar appears or disappears - fixing the "info panel stuck at half height after rotation" bug.
- **Wider mobile scrollbars:** Scrollbar touch targets are wider (16px) on mobile for easier dragging. Firefox scrollbar styling is now also supported via the standard `scrollbar-width`/`scrollbar-color` properties.

### NAS / Network Folder Performance

Adding a folder on a NAS or network share (SMB) no longer freezes the UI. The folder is registered immediately and the filesystem scan runs in the background, so you can keep using Photonarium while a large network folder is being indexed.

### Bug Fixes

- **Blank Faces screen:** Fixed a bug where navigating away from the Faces screen while it was still loading would leave it permanently blank on return, with no error shown.
- **Windows installer:** The installer now correctly tells Command Prompt users to run `activate.bat` instead of the bash-only `activate` script.

## v1.0.2-beta.2

### LAN Access

Photonarium is now accessible from other devices on your local network. The server binds to all network interfaces (`0.0.0.0`) by default, so you can browse your photo library from a phone, tablet, or another computer on the same network.

To restrict access to the machine Photonarium is running on, set `server_host: 127.0.0.1` in your config file. Photonarium is designed for trusted home networks and should not be exposed to the public internet.

### Configuration Relocated to OS-Standard Location

The configuration file has moved from `.photonarium.yml` inside the data directory to the OS-standard location:

- **Windows:** `%LOCALAPPDATA%\Photonarium\photonarium.yml`
- **macOS:** `~/Library/Application Support/Photonarium/photonarium.yml`
- **Linux:** `~/.config/photonarium/photonarium.yml`

The config file now stores a `data_dir` setting, so after installation `python app.py` just works - no need to pass `--data-dir` every time.

**Existing users:** If Photonarium finds a `.photonarium.yml` in the current directory but no config at the new location, it will automatically migrate your settings and inject the correct `data_dir`. The old file is left in place but ignored.

### In-App Settings Editor

The **Edit Settings** button on the Database screen now opens an in-app settings editor. The editor works from any device on your network - no need for local file access.

- **Schema-driven:** The backend describes all fields, types, numeric constraints, and help text in a single API response. The frontend renders a generic form with zero hardcoded knowledge of individual settings.
- **Danger fields:** Settings that could break connectivity (`data_dir`, `server_host`, `server_port`) are highlighted with a red border and warning icon.
- **Validation:** Client-side range checking plus full backend validation on save, with clear error messages.
- **Restart required:** Saved changes are written to disk but don't take effect until Photonarium is restarted. The dialog shows the on-disk values, so re-opening after a save reflects what was saved.
- **Direct editing:** A link in the dialog header lets you reveal the YAML file in your file manager if you prefer editing it by hand.

### Installation Improvements

- The installer now creates the config file at the OS-standard location with `data_dir` pre-configured, so the final startup command is simply `python app.py`.
- New `--init-config <data-dir>` flag for scripted/automated installs.
- New `--config` / `-c` flag to use a config file at a custom location.

### Bug Fixes

- **Search filter button:** The Clear Filter button on the search screen now works correctly (was broken by a duplicate HTML element ID).
- **Touch panning:** One-finger panning now works when zoomed in full-screen view, matching the existing mouse drag behaviour.
- **Filter scroll position:** Applying a new filter or opening a group now scrolls the gallery to the top instead of staying at a stale position.
- **Histogram errors:** Requesting a histogram for an image with no checksum now returns a clean 404 instead of a 500 error.
- **Fullscreen performance:** The gallery info panel (including on-demand histogram generation) is deferred while full-screen view is open, reducing unnecessary work.
- **Group navigation:** A loading overlay is now shown when opening a group from the Groups screen or navigating between groups.
- **Error messages:** API errors from the backend now show clearer messages when the response is not valid JSON.
- **Accessibility:** Full-screen images now have alt text set to the filename.
- **Config alignment:** The default similarity threshold for level 2 duplicates now matches between the config template and the internal default (0.93).
