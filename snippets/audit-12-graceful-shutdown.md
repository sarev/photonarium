# Audit 12 — Threaded Code Must Integrate with Graceful Shutdown

## Principle

> Threaded code must integrate with graceful shutdown

## Scope

- All `threading.Thread` subclasses in `app/imagedb.py`
- Signal handlers: `imagedb.py:8612-8654`
- `atexit` handlers: `imagedb.py:8628-8632`, `app.py:375-386`
- `ImageDatabase.close()`: `imagedb.py:6805-6803`
- Queue draining and state persistence: TrashWorker, ImportWorker
- Executor shutdown: `thumbnails.py`, TrashWorker, ImportWorker

## Findings

### Signal & Exit Handlers

1. **Signal handler** (`imagedb.py:8612-8625`): `_signal_handler(signum, frame)` handles SIGINT/SIGTERM, calls `_active_database.close()`.
2. **atexit handler** (`imagedb.py:8628-8632`): `_atexit_handler()` — fallback cleanup on normal exit.
3. **Registration** (`imagedb.py:8635-8654`): `register_signal_handlers(db)` registers both.
4. **App-level shutdown** (`app.py:375-386`): `shutdown_db()` + `atexit.register(shutdown_db)`.

### Thread Lifecycle — All 8 Workers

All worker threads follow a consistent pattern:

| Thread | Init | daemon | stop_event check | join(timeout) |
|--------|------|--------|-------------------|---------------|
| IngestionThread | `imagedb.py:1875-1945` | `True` (1924) | `1994` | `6764` (30s) |
| EmbeddingThread | `imagedb.py:2923-2980` | `True` (2947) | `3056` | `6769` (30s) |
| FaceDetectionThread | `imagedb.py:3243-3268` | `True` (3263) | `3318` | `6774` (30s) |
| NimaThread | `imagedb.py:3745-3769` | `True` (3765) | `3838,3848` | `6779` (30s) |
| VideoProcessingThread | `imagedb.py:4036-4062` | `True` (4048) | `4070` | `6784` (30s) |
| TrashWorker | `imagedb.py:5043-5070` | `True` (5062) | `5087` | `6789` (30s) |
| ImportWorker | `imagedb.py:5209-5255` | `True` (5243) | `5276` | `6794` (30s) |
| ScanTimerThread | `imagedb.py:5611-5625` | `True` (5623) | `5640` | `6799` (30s) |

### Shutdown Sequence (`ImageDatabase.close()`)

`imagedb.py:6761-6801`:
1. Waits for in-flight rotation operations (`6806-6820`)
2. Sets `_stop_event` to signal all threads (`6761`)
3. Joins each thread sequentially with 30s timeout
4. Logs warning if thread doesn't stop in time
5. Closes database connection

### Queue Draining on Shutdown

Workers persist pending work for recovery on next startup:

| Worker | Drain | Persist | Recovery |
|--------|-------|---------|----------|
| TrashWorker | `5108-5115` | `<trash_dir>/.pending_trash.json` | `7174-7200` |
| ImportWorker | `5304-5315` | `<catalogue_dir>/.pending_import.json` | `7282-7312` |

### Executor Shutdown

- TrashWorker: `imagedb.py:5081-5084` created once, `shutdown(wait=False, cancel_futures=True)` at `5113`
- ImportWorker: `imagedb.py:5270-5273` created once, `shutdown(wait=False, cancel_futures=True)` at `5315`
- Thumbnails: `thumbnails.py:819` → `executor.shutdown(wait=not interrupted, cancel_futures=interrupted)`

### Additional Daemon Threads

- Dialog thread: `app.py:1278` → `daemon=True`, `join(timeout=300)`
- Restart thread: `app.py:1540` → `daemon=True`

## Status

**Compliant**

## Actions

None required. All threads integrate with graceful shutdown via `stop_event`, daemon flags, and join timeouts. Work persistence ensures no data loss on shutdown.
