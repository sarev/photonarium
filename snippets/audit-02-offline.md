# Audit 02 — Runs Offline

## Principle

> Runs offline indefinitely (except `download_models.py` and Material-Symbols font fetch)

## Scope

- `app/*.py` — all Python source for `requests`, `urllib`, `http.client`, hardcoded external URLs
- `app/static/*.js` — all JS for `fetch()` calls to external origins, `WebSocket`, `EventSource`
- `app/static/index.html` — CDN links, external stylesheets/scripts
- `download_models.py` — verified as download-time-only script
- `HF_HUB_OFFLINE` enforcement

## Findings

1. **HuggingFace Hub offline mode enforced**:
   - `imagedb.py:105` — `os.environ['HF_HUB_OFFLINE'] = '1'`
   - `caption.py:37` — `os.environ['HF_HUB_OFFLINE'] = '1'`
   - Prevents any HuggingFace model downloads at runtime.

2. **Zero runtime network calls in Python**: No `requests.get()`, `requests.post()`, `urllib.request`, or `http.client` usage anywhere in `app/`. The `requests` library is imported but only used by `download_models.py`.

3. **Frontend API calls are all local**: `core.js:871` sets `apiBase = '/api'` (relative path). All `fetch()` calls at `core.js:891` use `this.apiBase + endpoint`, always targeting the local Flask server.

4. **External CDN links** (documented exceptions):
   - `index.html:52` — Material Symbols font from Google Fonts CDN
   - `index.html:87-89` — Noto Sans and Urbanist from Google Fonts CDN

5. **Graceful offline fallback for fonts**:
   - `index.html:55-83` — Material Symbols: detects load failure via `document.fonts.load()`, falls back to Unicode emoji (🏷️, ☰, ⛶, ☆, etc.)
   - `styles.css:298` — Font family chain: `"Noto Sans", Urbanist, Arial, sans-serif` — degrades to system fonts when offline.

6. **No WebSocket/SSE connections**: Zero `ws://`, `wss://`, `EventSource`, or `text/event-stream` usage.

7. **`download_models.py`** (`lines 40, 68-174`): One-time download script using `urllib.request` and HuggingFace Hub. Runs separately before first use. Does not set `HF_HUB_OFFLINE`.

8. **Localhost-only logging**: `app.py:4912` logs `Open http://{host}:{port}` — informational only, no network call.

## Status

**Compliant**

## Actions

None required.
