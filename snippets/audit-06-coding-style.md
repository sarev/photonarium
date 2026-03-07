# Audit 06 — Respect Pre-existing Coding Styles

## Principle

> Respect pre-existing coding styles and formatting

## Scope

- `tools/ruff.toml` — Python linting/formatting config
- `tools/eslint.config.mjs` — JavaScript linting config
- `app/*.py` — Python formatting consistency
- `app/static/*.js` — JavaScript formatting consistency
- Focus on newer video-related code for style drift

## Findings

### Python (Ruff)

Config (`tools/ruff.toml:48`): single quotes preferred, standard ruff formatting rules.

**Files with formatting issues detected:**

1. **`app/video.py`** (multiple issues):
   - Lines 177-187 — list argument formatting (multi-line list not conforming to ruff's element-per-line rule)
   - Lines 408-414 — ffmpeg command list formatting
   - Lines 437-440 — long f-string collapsing

2. **`app/thumbnails.py`** (slice spacing):
   - Lines 328, 333, 336, 345, 352, 360, 367, 376, 377 — `data[pos + 2: pos + 4]` should be `data[pos + 2 : pos + 4]` (spaces around `:` in slices with arithmetic)

3. **`app/faces.py`**:
   - Line 2703 — `members[i: i + BATCH_SIZE]` should be `members[i : i + BATCH_SIZE]`

4. **`app/imagedb.py`** (multi-line strings):
   - Lines 4200-4201, 4230-4232, 8218, 8225, 8327, 8333 — SQL string concatenations and multi-line append calls that ruff wants collapsed

### JavaScript (ESLint)

Config (`tools/eslint.config.mjs`): 4-space indentation, single quotes, always semicolons, multiline comma dangle.

**All JS files pass ESLint validation.** No style inconsistencies detected in video-related JS code (`videos.js`, `appstate/videos.js`).

### Overall Consistency

- Naming conventions consistent: snake_case for Python, camelCase for JS
- New video code (`video.py`, `videos.js`) follows established patterns
- No significant style drift between old and new code

## Status

**Compliant** (after fix)

## Actions

- ~~**P2**: Run `ruff format` on affected files~~ — **FIXED**: `ruff format` and `ruff check --fix` applied to `video.py`, `faces.py`, `imagedb.py`, `thumbnails.py`, `nima.py`, `app.py`. All checks pass
