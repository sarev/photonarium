# Plan: Semantic Search for Unknown Faces

## My Rules

- Reuse and extend, over reinvent and duplicate.
- Ensure database locking in imagedb.py is respected.
- Consider concurrency, parallelization and cacheing opportunities, where sensible.
- Ensure thread safety and correct interaction with the existing graceful shutdown code.
- Comment well with JDoc and PEP styles.
- Include logging to aid the inevitable debugging phase.

## Database

Add semantic_embedding BLOB column to faces table (OpenCLIP embedding, distinct from the 512D face recognition embedding).

## Embedding Generation

During face detection, after generating the face thumbnail, encode it with OpenCLIP (same model used for images). Store the resulting embedding in `semantic_embedding`.

## Backend API

Extend `GET /api/faces`:
- Add optional `?search=<text>` query parameter
- When present: encode text with OpenCLIP, compute cosine similarity against all unknown face semantic_embeddings, return sorted by similarity DESC (ignore groups)
- When absent: use default group-based sort
- Try to reuse/extend existing code for OpenCLIP embedding - much time and testing has gone into ensuring it's right and performant

## Frontend

- Add <input type="text" placeholder="Search faces..."> to the right of the "Unknown (N)" heading
- Debounce input (300ms)
- Pressing Enter blurs
- On blur: call `/api/faces?search=<text>`` and re-render unknown faces grid (if no <text>, use default sort)
- On clear: fetch without search param, restore default sort
- Ensure we have a suitable splash screen (if none already) while the semantic sort is 'thinking' (as per the "Loading faces..." splash), from the moment the user commits a search term, or clears the search.

## Migration

- Backfill `semantic_embedding` for existing faces via CLI flag and background task (similar to --generate-thumbnails).
- Background task hooked into full reindex that can be triggered from "Rescan all folders" button on Database screen.
- Don't recreate face thumbnail embeddings if they already exist.
