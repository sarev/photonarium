# API Reference

Complete list of REST API endpoints in `app.py` (71 routes).
For design principles and conventions, see the API section in `CLAUDE.md`.

## Images (11 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/images` | List all images with metadata |
| GET | `/api/images/:id` | Get single image metadata |
| POST | `/api/images/:id` | Update image (description, rating) |
| GET | `/api/images/:id/exif` | Get EXIF metadata for an image |
| POST | `/api/images/trash` | Move images to trash `{image_ids: []}` |
| GET | `/api/images/:id/thumbnail?size=N` | Get thumbnail (snapped to 200 or 400px) |
| GET | `/api/images/:id/full` | Get full-resolution image |
| GET | `/api/images/:id/histogram` | Get image histogram data |
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

## Metadata (3 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/metadata-search` | Subsequence search on EXIF metadata `{criteria: {key: query}}` |
| GET | `/api/metadata-keys` | All distinct metadata keys in the database |
| GET | `/api/metadata-values?key=X` | Distinct values for a key (autocomplete) |

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
| GET | `/api/duplicates?level=N` | Get duplicate groups at level 0-5 |
| POST | `/api/duplicates/sort-semantic` | Sort duplicate groups by semantic similarity |
| POST | `/api/duplicates/prune` | Prune groups: keep best, trash rest `{level, keep_count}` |

## Groups (7 routes)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/groups/preview` | Preview keep/trash split for all groups at a level |
| POST | `/api/groups` | Create custom group `{group_hash, name, image_ids}` |
| PATCH | `/api/groups/:hash` | Rename custom group `{name}` |
| DELETE | `/api/groups/:hash` | Delete custom group |
| POST | `/api/groups/:hash/preview` | Preview smart-group membership changes |
| POST | `/api/groups/:hash/images` | Add images to group `{image_ids}` |
| POST | `/api/groups/:hash/images/remove` | Remove images from group `{image_ids}` |

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

## Utility (1 route)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/reveal` | Reveal a file or folder in the OS file explorer `{path}` |

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

## Faces (21 routes)

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
