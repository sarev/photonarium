# Audit 08 — Clean, Elegant, Obvious, Non-Technical UI/UX

## Principle

> Clean, elegant, obvious, non-technical UI/UX

## Scope

- `app/static/index.html` — all user-visible text (buttons, labels, status messages, dialogs)
- `app/static/*.js` — error messages, status text, dynamic UI content
- `app/static/styles.css` — visual design, theme support
- `app/static/duplicates.js` — similarity level labels
- Focus on technical jargon exposure to end users

## Findings

### 1. Technical Status Labels on Database Screen

`index.html:581-590` shows processing queue labels, some of which use internal terminology:

| Label | Line | Issue |
|-------|------|-------|
| "Face Search Index" | 590 | Highly technical — users don't know what a face search index is |
| "Grouping Faces" | 588 | Internal algorithm name — unclear to users what this does |
| "Matching Faces" | 589 | Vague — unclear it's reassessing unknown faces against known people |
| "Aesthetic" | 583 | Borderline — "visual quality scoring" would be clearer |

**Good labels** that work well: "Classifying" (581), "Face Detection" (582), "Indexing", "Video", "Importing", "Trashing"

### 2. Error Messages (Excellent)

All user-facing error messages are clear and non-technical:
- "Could not add folder." ✓
- "Failed to assign face" ✓
- "Search failed. Please try again." ✓
- "You appear to be offline. Changes cannot be saved." ✓

No exposure of SHA256, BLOB, UUID, perceptual hash, cosine similarity, MTCNN, or other internal terms in error messages.

### 3. Search Similarity Slider (Excellent)

`index.html:640-643` — uses "Loose" / "Strict" labels instead of technical "cosine similarity threshold" or numeric values.

### 4. Duplicate Similarity Levels (Excellent)

User-facing labels: "Identical", "Near-identical", "Similar", "Related", "Directories", "Custom" — all technical implementation details (SHA256, perceptual hash, OpenCLIP embedding, LSH) are hidden.

### 5. Face Management Terminology (Good)

- "Unknown Faces" (not "unidentified face embeddings")
- "Quick Match" with sparkle icon for top candidates
- "Locked" / "Unlocked" shown as padlock icons
- "Suppress" for marking false positives — borderline but acceptable in context

## Status

**Mostly Compliant**

## Actions

- **P2**: Replace "Face Search Index" (`index.html:590`) with "Preparing Faces" or "Building face database"
- **P2**: Replace "Grouping Faces" (`index.html:588`) with "Organizing unknown faces" or "Clustering faces"
- **P3**: Replace "Matching Faces" (`index.html:589`) with "Finding matches" or "Identifying faces"
- **P3**: Consider replacing "Aesthetic" (`index.html:583`) with "Quality scoring" or "Rating quality"
