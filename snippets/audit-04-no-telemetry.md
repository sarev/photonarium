# Audit 04 — No Telemetry or Data Collection

## Principle

> No telemetry or data collection

## Scope

- `app/*.py` — all Python source for analytics, tracking, crash reporting, external service calls
- `app/static/*.js` — all JS for Google Analytics, Mixpanel, Segment, Sentry, beacons, pixels
- `app/static/index.html` — hidden tracking elements, external script tags
- `www/index.html` — marketing site (separate from app)

## Findings

1. **Zero tracking libraries**: No imports of analytics, telemetry, Sentry, Mixpanel, Segment, or Google Analytics anywhere in `app/` or `app/static/`.

2. **No external service calls at runtime**: All `fetch()` calls target local `/api` endpoints. No `requests.get/post()` in runtime Python code.

3. **No crash reporting**: Standard Python `logging` module used throughout — logs to console/stderr only, no external error reporting service.

4. **No usage statistics**: No counters, metrics, or usage data collected or transmitted.

5. **No version/update checking**: No background polling of GitHub releases or version endpoints.

6. **Local-only data storage**:
   - SQLite database at user-configured data directory
   - Config file at OS-standard config location
   - Thumbnail cache on disk
   - `localStorage` for UI theme only (`index.html:15`)

7. **Marketing site separation**: `www/index.html:405-411` contains Stripe donation links, but this is the marketing website — completely separate from the app. The app (`app/static/`) has no financial integrations.

8. **No CORS**: `app.py:73` comments that `flask_cors` is not needed since frontend is served from same origin — no cross-origin data sharing.

## Status

**Compliant**

## Actions

None required.
