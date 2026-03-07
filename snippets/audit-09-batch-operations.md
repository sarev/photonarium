# Audit 09 — Batch Operations

## Principle

> Batch operations for all image/face/people mutations (minimise round-trips)

## Scope

- `app/app.py` — all 82 Flask API routes, focus on POST/PUT/DELETE/PATCH mutation endpoints
- `app/faces.py` — face/people CRUD operations
- `app/imagedb.py` — image mutation operations
- Patterns: per-item loops with individual commits vs `executemany()` / batched transactions

## Findings

### Per-Item Loop Mutations (Should Be Batched)

1. **`/api/faces/unassign`** (`app.py:3418-3424`):
   Loops over `face_ids` calling `update_face_person(db.conn, face_id, None, ...)` individually. Each call executes `conn.execute()` + `conn.commit()`. Should be a single `executemany()` + one commit.

2. **`/api/faces/suppress`** (`app.py:3470-3487`):
   Loops over `face_ids` calling `suppress_face(db.conn, face_id)` individually. Multiple lock acquisitions and commits per face.

3. **`/api/faces/<face_id>/toggle-manual`** (`app.py:3953-3975`):
   Loops over `face_ids` with individual function calls per face.

4. **`/api/faces` PATCH** (`app.py:3519-3530`):
   Per-item `db.conn.execute("UPDATE faces SET manually_tagged=...")` in a loop. Uses a single commit at end, but could use `executemany()`.

5. **`/api/faces/unassign-batch`** (`app.py:4184-4240`):
   Despite the name, uses loop + individual calls to `update_face_person()` rather than one `executemany()`.

6. **`/api/faces/identify-batch`** (`app.py:3629-3750`):
   Individual person creation + face updates in a loop. Complex logic makes batching harder but per-item commits are unnecessary.

### Database Lock Contention Pattern

When endpoints like `/api/faces/unassign` loop with `update_face_person()`, each call:
1. Executes `conn.execute(...UPDATE faces...)`
2. Calls `conn.commit()` (releases WAL lock)
3. Lock reacquired for next iteration

Under the outer `with db._db_lock`, all statements should execute then single commit.

### Good Batch Patterns to Emulate

These endpoints do it right:

| Endpoint | Location | Pattern |
|----------|----------|---------|
| `/api/faces` embedding update | `app.py:3162-3164` | `executemany()` for batch updates |
| Trash enqueue | `app.py:694-750` | `db.enqueue_trash(image_ids)` batches internally |
| Image rating update | `app.py:1136+` | Batch `executemany()` |
| NIMA batch writes | `imagedb.py:3978` | Single `executemany()` call |
| LAION batch writes | `imagedb.py:6009` | Single `executemany()` call |
| VideoProcessingThread | `imagedb.py:4239-4248` | `executemany()` for scene inserts (recently fixed) |

## Status

**Issues Found**

## Actions

- **P2**: Refactor `/api/faces/unassign` (`app.py:3418-3424`) to batch all face updates into a single `executemany()` + one commit
- **P2**: Refactor `/api/faces/suppress` (`app.py:3470-3487`) similarly
- **P2**: Refactor `/api/faces/unassign-batch` (`app.py:4184-4240`) to use actual batch operations matching its name
- **P3**: Refactor `/api/faces/<face_id>/toggle-manual` to batch when multiple face IDs provided
- **P3**: Audit `update_face_person()` in `faces.py` — consider adding a batch variant that accepts a list of `(face_id, person_id)` tuples
