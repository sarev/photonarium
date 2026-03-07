# Audit 14 — Never Use SSE

## Principle

> Never use SSE — use existing event polling (Waitress compatibility)

## Scope

- `app/app.py` — all Flask routes, response content types
- `app/static/*.js` — `EventSource`, `text/event-stream`
- `app/imagedb.py` — EventQueue implementation
- Event delivery mechanism

## Findings

1. **No `text/event-stream`**: Zero occurrences in any Python file.

2. **No `EventSource`**: Zero occurrences in any JavaScript file.

3. **No streaming responses**: No `yield`-based Flask responses or `Response(stream_with_context(...))` patterns.

4. **Event polling implemented correctly**:
   - Backend: `EventQueue` ring buffer (`imagedb.py:4900-4936`) with cursor-based retrieval and 200-event max
   - Frontend: Polls `/api/events` every 2 seconds with cursor parameter (`appstate/events.js`)
   - Events: `faces_reassessed`, `folder_added/removed`, `processing_complete`, `images_modified`, `nima_complete`, `faces_changed`, `people_changed`, `images_changed`, `groups_changed`

5. **Waitress compatible**: Waitress WSGI server does not support SSE; the polling approach is architecturally correct for this server.

## Status

**Compliant**

## Actions

None required.
