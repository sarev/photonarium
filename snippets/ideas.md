# Roadmap Ideas

Brainstormed 2026-02-15. Not prioritised yet.

## Notes to Self

### Tutorial Generator

- Add slideshow and smartgroups stuff
- Add optional CLI switch to regenerate the initial "manual" screenshots,
  using the `examples` images and a clean, disposable app instance.

### README

Needs to include slideshow and smart groups. - DONE


## Browsing & Discovery

### Slideshow Mode - DONE

Relatively easy -- builds on existing fullscreen viewer infrastructure.
Always deactivates face tagging mode on start.

Entry points:

1. **Fullscreen viewer overlay:** Two controls -- "Start slideshow" (linear)
   and "Start shuffled slideshow" (random order). Escape or upward swipe
   ends the slideshow and exits fullscreen. Configurable timing per slide,
   possibly quality-weighted (best shots linger longer).
2. **Groups screen:** onHover badges on each group stack thumbnail -- one for
   linear progression through the group, one for shuffled. Enters fullscreen
   in slideshow mode scoped to that group's images.

### Smart Albums - DONE

Relatively easy -- builds on existing filter + custom groups infrastructure.

- **Search screen:** New "Save as Smart Group" button that persists the
  current filter criteria as a named custom group (level 5). The group stores
  the filter definition rather than a static list of image IDs.
- **Groups screen:** Smart group stacks show the group name but NOT an image
  count below it -- just "Smart Group" since the count is dynamic. onHover
  "Edit" badge navigates to the Search screen with the saved filter criteria
  pre-loaded for refinement.
- **Backend:** Smart groups need a `filter_json` column (or similar) in
  custom_groups to store the filter definition. Display list is recomputed
  from the filter each time the group is opened, not cached.

### Timeline / Calendar View

Navigate photos by date on a visual timeline or calendar grid. The timestamp
data is already there -- this is a presentation layer on top of existing data.
Could show photo density per day/month/year as a heatmap.

### Map View

Plot photos with GPS EXIF data on an interactive map. Tricky because
Photonarium must run offline and never phone home. Options:

- **Pre-downloaded tiles:** download_models.py could fetch a tile set for the
  user's region, but map tiles are enormous (planet = 100GB+ at useful zoom
  levels). Even a single country is multi-GB. Not practical.
- **Offline vector maps:** Protomaps/PMTiles format -- a single .pmtiles file
  per region, rendered client-side. Smaller than raster tiles but still large.
  Could let the user choose their region during install.
- **No basemap:** Plot photo dots on a blank coordinate grid with
  country/region outlines only (Natural Earth shapefiles, ~10MB). Loses the
  visual context of a real map but stays fully offline and lightweight.
- **Optional online mode:** Use Leaflet + OSM tiles but only when the user
  explicitly opts in. Would need a clear "this fetches from the internet"
  consent. Goes against the offline-first principle but may be acceptable as
  an opt-in.

May not be worth the complexity vs. value. Revisit if users ask for it.


## Organisation

### Bulk Rating

The ratings field is free-text (effectively tags). The only missing piece is
bulk CRUD -- apply/remove a rating string to multiple selected images at once.

### Trash Browser

Preview and selectively restore trashed images from within the app, instead
of manually fishing through the trash directory. Show thumbnails (they still
exist on disk after trashing), allow selective restore back to original folder.

### Import Wizard

Pull photos from camera/SD card with date-based folder organisation.
Detect mounted removable media, preview contents, choose destination folder
structure (e.g. YYYY/MM/DD), copy with progress.


## Multi-Library

Switch between separate photo collections (personal vs work, different family
members). Each library has its own database, thumbnails, and settings. Could
be as simple as a library selector that sets data_dir and restarts.


## Installers / Packaging

Users currently need to install Python, clone the repo, create a venv, install
dependencies, download models, and manually start the app. Needs:

- Platform-specific installers (Windows MSI/NSIS, macOS DMG, Linux .deb/.rpm)
- Bundled Python runtime (PyInstaller, cx_Freeze, or similar)
- System service / auto-start on boot
- Tray icon with start/stop/open browser
- Auto-update mechanism
- First-run wizard (pick photo folders, download models with progress)


## Internationalisation

The big one. Photonarium's UI is entirely English. All button labels, tooltips,
status messages, error messages, and the tutorial are hardcoded strings. Needs:

- String extraction system (all user-visible text into a resource file)
- Language selection (config option or auto-detect from browser)
- RTL layout support (Arabic, Hebrew)
- Date/number formatting (locale-aware)
- Tutorial translations (the demo-seed scripts already have a start on this)

Backend messages also need translation -- warnings, errors, and status
messages from the Python backend are shown to the user in toasts and the
Database screen. Would need either: backend sends message keys and the
frontend resolves them to the active language, or backend sends pre-translated
strings using a Python i18n library (gettext/Babel).

ML models are also English-centric:

- OpenCLIP semantic search is trained primarily on English text-image pairs.
  Multilingual CLIP variants exist (e.g. XLM-Roberta-based M-CLIP,
  multilingual ViT-B/16) but are larger and may have different quality
  trade-offs. Would need to be a configurable model choice.
- BLIP/BLIP-2 captioning generates English text. Poor-man's fix: caption in
  English then translate via a lightweight translation model (e.g. Helsinki-NLP
  OPUS-MT, ~300MB per language pair, runs offline) or a local LLM. Not ideal
  but avoids waiting for mature multilingual captioning models.
- Face recognition (InceptionResnetV1) is language-independent -- no change
  needed.
- Switching models means re-embedding the entire library, which is expensive.
  Would need a migration path (re-index prompt, background re-embedding).


## Video Support

Even basic support would be valuable -- many photo libraries have MP4/MOV
files mixed in. Minimum viable:

- Thumbnail generation (extract a frame)
- Playback in the fullscreen viewer (HTML5 video)
- Duration/resolution in the info panel
- Exclude from CLIP embedding (or use video-capable CLIP)


## AI / ML

### AI Upscaling

Enhance old/small/low-resolution photos. Real-ESRGAN is lightweight and
MIT-licensed. Could offer as a right-click action in the fullscreen viewer.
Non-destructive (save upscaled version alongside original, or as a new file).

### Improved Auto-Captioning

Current BLIP captions are generic ("a woman by an archery target") -- not the
memory the user wanted. Possible improvements:

- Inject recognised face names into captions ("Alice by an archery target")
- Use location/date context ("Alice at the county fair, July 2023")
- Treat auto-captioning primarily as an accessibility feature (screen readers)
  rather than a memory/description feature
