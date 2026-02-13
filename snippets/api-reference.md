# API Reference

Complete list of REST API endpoints in `app.py` (54 routes).
For design principles and conventions, see the API section in `CLAUDE.md`.

## Images (11 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/images` | List all images with metadata |
| GET | `/api/images/:id` | Get single image metadata |
| POST | `/api/images/:id` | Update image (description, rating) |
| POST | `/api/images/trash` | Move images to trash `{image_ids: []}` |
| GET | `/api/images/:id/thumbnail?size=N` | Get thumbnail (snapped to 200 or 400px) |
| GET | `/api/images/:id/full` | Get full-resolution image |
| GET | `/api/images/:id/histogram` | Get image histogram data |
| POST | `/api/images/:id/reveal` | Reveal image in OS file explorer |
| POST | `/api/images/:id/generate-caption` | Generate BLIP caption for image |
| POST | `/api/images/rotate` | Rotate images `{image_ids: [], direction}` |
| GET | `/api/images/people-names` | Get people names appearing in images |

## Folders (4 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/folders` | List registered folders with image counts |
| POST | `/api/folders` | Add folder `{path: string}` |
| DELETE | `/api/folders/:path` | Remove folder and its images |
| POST | `/api/pick-folder` | Open native folder picker dialog |

## Search & Similarity (2 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/search` | Semantic search `{text, threshold}` |
| GET | `/api/similar/:id` | Get images similar to a given image |

## Status & Config (5 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Processing status `{status, indexing_queue, embedding_queue}` |
| POST | `/api/rescan` | Queue all folders for re-indexing |
| GET | `/api/config` | Get frontend-relevant configuration values |
| GET | `/api/config/schema` | Full config schema for the settings editor |
| POST | `/api/config/save` | Save config values `{values: {key: value}}` |

## Duplicates (3 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/duplicates?level=N` | Get duplicate groups at level 0-3 |
| POST | `/api/duplicates/sort-semantic` | Sort duplicate groups by semantic similarity |
| POST | `/api/duplicates/prune` | Prune groups: keep best, trash rest `{level, keep_count}` |

## Stats (2 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/stats` | Get `{totalImages, totalFolders}` |
| GET | `/api/stats/cache` | Get thumbnail cache statistics |

## Events (2 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/events` | Fetch and clear pending events |
| GET | `/api/events/count` | Get count of pending events (lightweight) |

## People (10 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/people` | List all people (face_count via JOIN) |
| POST | `/api/people` | Create person `{id, name}` |
| GET | `/api/people/:id` | Get person details |
| PATCH | `/api/people/:id` | Update person `{name, preferred_face_id, threshold}` |
| DELETE | `/api/people/:id` | Delete person (faces become untagged) |
| POST | `/api/people/:id/merge` | Merge into another person `{into: target_id}` |
| POST | `/api/people/:id/dissolve` | Unidentify all faces and delete person |
| POST | `/api/people/:id/set-preferred` | Set preferred face for person |
| GET | `/api/people/:id/faces` | Get all faces for a person |
| GET | `/api/people/:id/thumbnail` | Get preferred face thumbnail |

## Faces (20 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/faces` | List all faces |
| GET | `/api/faces/:id` | Get single face details |
| GET | `/api/faces/:id/thumbnail` | Get cropped face thumbnail |
| GET | `/api/faces/:id/matches` | Get closest matching people for a face |
| GET | `/api/images/:id/faces` | Get faces detected in an image |
| POST | `/api/faces/assign` | Assign faces to person `{face_ids: [], person_id}` |
| POST | `/api/faces/unassign` | Unassign single face |
| POST | `/api/faces/unassign-batch` | Unassign faces `{face_ids: []}` |
| POST | `/api/faces/:id/unassign` | Unassign a specific face |
| POST | `/api/faces/suppress` | Mark as false positives `{face_ids: []}` |
| POST | `/api/faces/:id/suppress` | Suppress a specific face |
| PATCH | `/api/faces` | Batch update properties `{face_ids: [], locked: bool}` |
| POST | `/api/faces/:id/identify` | Identify a specific face |
| POST | `/api/faces/identify-batch` | Batch identify faces |
| POST | `/api/faces/:id/unidentify` | Unidentify a specific face |
| POST | `/api/faces/:id/toggle-manual` | Toggle manual tagging flag |
| DELETE | `/api/faces/:id` | Delete a face detection |
| POST | `/api/faces/reassess` | Trigger background face reassessment |
| GET | `/api/faces/reassess-status` | Get reassessment progress |
| POST | `/api/faces/reassess-ack` | Acknowledge reassessment completion |
| GET | `/api/faces/group-status` | Get face grouping status |
