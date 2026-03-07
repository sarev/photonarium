# Audit 15 — Keep Docs Up-to-Date; Add Tooltips

## Principle

> Keep `README.md`/`docs/develop.md` up-to-date; add tooltips to GUI elements

## Scope

- `README.md` — feature coverage, accuracy
- `docs/develop.md` — route count, API documentation, module coverage
- `docs/*.md` — per-screen documentation completeness
- `app/static/index.html` — `title=` tooltip attributes on buttons and controls

## Findings

### README.md — Current

- `README.md:43-65` — all major features documented including videos, transcriptions, scene detection
- `README.md:69-79` — getting started guide covers Videos screen
- `README.md:15-22` — links to per-screen documentation for all 7 screens

### docs/develop.md — Current

- `develop.md:17` — claims "Routes (82)" — **verified**: exactly 82 `@app.route` decorators in `app.py`
- `develop.md:28-178` — complete API documentation with 80+ documented routes
- `develop.md:367-382` — video processing module fully documented

### Per-Screen Documentation — Complete

All 7 main screens have dedicated guides:
- Gallery (`docs/gallery.md`)
- Full-screen viewer (`docs/fullscreen.md`)
- Search (`docs/search.md`)
- Videos (`docs/videos.md`) — comprehensive: scene timeline, heatmap, transcriptions, minimap, preferred scenes
- Groups (`docs/groups.md`)
- Faces (`docs/faces.md`)
- Database (`docs/database.md`)

### Tooltip Coverage — 94.7%

Out of ~75 buttons in `index.html`, **71 have `title=` attributes** with clear descriptions.

**Good tooltip examples:**
- Line 147: `title="View full-screen"`
- Line 344: `title="Similarity threshold (lower shows more results)"`
- Line 396: `title="Sort groups by similarity to a concept. Use -word to push matches down..."`
- Line 276: `title="Show only unknown faces"`

**Missing tooltips (4 buttons):**
- `btn-add-folder` — has visible text "Add Local Folder" (acceptable)
- `btn-rescan` — has visible text "Rescan Local Folders" (acceptable)
- `btn-clear-filter-action` — no visible text, **missing tooltip**
- `btn-apply-filter` — no visible text, **missing tooltip**

### Accessibility

- `aria-expanded="false"` on hamburger menu (`index.html:124`)
- Material Symbols icons with emoji fallbacks for cross-device accessibility
- Some icon-only buttons may benefit from `aria-label` attributes

## Status

**Compliant** (after fix)

## Actions

- ~~**P3**: Add `title=` tooltip to `btn-clear-filter-action` and `btn-apply-filter` buttons~~ — **FIXED**: tooltips added ("Clear all filter criteria", "Apply filter to gallery")
- **P3**: Consider adding `aria-label` attributes to icon-only buttons for screen reader accessibility
