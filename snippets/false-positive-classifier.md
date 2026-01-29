# False Positive Face Classifier

## Problem

MTCNN detects many false positives - patterns in grass, flowers, fabric, etc. that resemble faces. These pass confidence thresholds but aren't actual faces. Currently these must be manually suppressed.

## Available Training Data

From the existing database:

| Category | Count | Label |
|----------|-------|-------|
| Identified faces (person_id not null) | ~8,900 | Positive (real face) |
| Suppressed faces (box > 0) | ~34,000 | Negative (false positive) |
| Unknown faces | ~58,000 | Unlabeled |

The suppressed set includes both user-suppressed false positives and auto-suppressed small faces, providing a rich negative training set.

## Available Embeddings

Each face has two embeddings already computed:

### 1. Face Recognition Embedding (512D, InceptionResnetV1)

- Trained specifically for face recognition
- Optimized to cluster same-person faces together
- Real faces should form tighter clusters; false positives may be scattered or form their own clusters
- Stored in `faces.embedding`

### 2. Semantic Embedding (512D or 768D, OpenCLIP)

- Trained on image-text pairs for general visual understanding
- Knows what "a face" looks like vs "grass" or "flowers"
- May directly encode "faceness" as a semantic concept
- Stored in `faces.semantic_embedding`

**Hypothesis**: The semantic embedding may be more discriminative for this task since it has broader visual knowledge, while the face embedding is narrowly optimized for identity.

Real-world example: semantic search on unknown (as-yet untagged) face thumbnails for something like "flower blur" will return a lot of false-positive 'faces' spotted that are actually just flowers. But it also returns a lot of blurry pictures of faces where (for example) someone has flowers in their hair. So treat with caution!

## Implementation Approaches

### Approach 1: Simple Linear Classifier

Minimal complexity, good baseline.

```python
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
import numpy as np

# Load embeddings from database
real_faces = load_embeddings(where="person_id IS NOT NULL AND suppressed = 0")
fake_faces = load_embeddings(where="suppressed = 1 AND box_w > 0")

X = np.vstack([real_faces, fake_faces])
y = np.array([1] * len(real_faces) + [0] * len(fake_faces))

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)

# Train on face embedding
clf_face = LogisticRegression(max_iter=1000)
clf_face.fit(X_train[:, :512], y_train)  # face embedding

# Train on semantic embedding
clf_semantic = LogisticRegression(max_iter=1000)
clf_semantic.fit(X_train[:, 512:], y_train)  # semantic embedding

# Train on both concatenated
clf_combined = LogisticRegression(max_iter=1000)
clf_combined.fit(X_train, y_train)
```

### Approach 2: Small MLP

More capacity to learn non-linear decision boundaries.

```python
import torch
import torch.nn as nn

class FaceClassifier(nn.Module):
    def __init__(self, input_dim=512, hidden_dim=128):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(hidden_dim, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
            nn.Sigmoid()
        )

    def forward(self, x):
        return self.net(x)

# Could train three variants:
# - Face embedding only (512D input)
# - Semantic embedding only (512D input)
# - Both concatenated (1024D input)
```

### Approach 3: Ensemble

Use predictions from both embedding types.

```python
def predict_real_face(face_embedding, semantic_embedding):
    p_face = clf_face.predict_proba([face_embedding])[0, 1]
    p_semantic = clf_semantic.predict_proba([semantic_embedding])[0, 1]

    # Simple average, or learned weights
    return (p_face + p_semantic) / 2
```

### Approach 4: Additional Features

Beyond embeddings, other signals may help:

```python
features = [
    face_embedding,           # 512D
    semantic_embedding,       # 512D
    confidence,               # MTCNN confidence (scalar)
    box_aspect_ratio,         # width/height ratio
    box_area_normalized,      # box area relative to image
    laplacian_variance,       # sharpness/blur of face crop
]
```

False positives may correlate with:
- Lower MTCNN confidence
- Unusual aspect ratios
- Higher blur (patterns in out-of-focus areas)
- Smaller relative size

## Integration Points

### Option A: Flag for Review (Preferred)

Flag likely false positives during indexing, with UI to review and batch-suppress.

**Database Change:**
```sql
ALTER TABLE faces ADD COLUMN likely_false_positive INTEGER DEFAULT 0;
```

**During Indexing (imagedb.py):**
```python
# After face detection and embedding computation
for detected_face in faces:
    prob = classifier.predict_proba(detected_face.embedding)
    likely_fp = 1 if prob < threshold else 0
    save_face(detected_face, likely_false_positive=likely_fp)
```

**Faces Screen Toolbar (index.html):**
```html
<button id="btn-faces-likely-fp" class="toolbar-btn" title="Show likely false positives">
    <span class="material-symbols-outlined">flag</span>
</button>
```

**Filter Logic (faces.js):**
```javascript
let showLikelyFalsePositives = false;

function getDisplayedUnknownFaces() {
    let faces = allFaces.filter(f => !f.person_id && !f.suppressed);
    if (showLikelyFalsePositives) {
        faces = faces.filter(f => f.likely_false_positive);
    }
    return faces;
}
```

**User Workflow:**
1. Click flag button to filter to likely false positives
2. Scroll through, double-click any real faces to identify them (removes flag implicitly)
3. Ctrl+A to select all remaining
4. Delete to suppress the batch

**API Changes:**
- `GET /api/faces` returns `likely_false_positive` field
- `POST /api/faces/:id/unflag` - mark as not a false positive (clears flag)
- Or: identifying a face automatically clears the flag

**Pros:**
- Nothing auto-suppressed, human reviews everything
- Efficient batch workflow for clearing false positives
- Flag persists until explicitly cleared or face is identified
- Non-destructive, can re-run classifier with different threshold

**Cons:**
- Requires training classifier first
- Extra column in database

---

### Option B: Post-Detection Hard Filter

Run classifier after MTCNN, before saving to database.

```python
# In faces.py detect_faces()
for detected_face in mtcnn_results:
    embedding = compute_embedding(detected_face)
    if classifier.predict_proba(embedding) < threshold:
        continue  # Skip likely false positive
    save_face(detected_face)
```

**Pros**: Clean database, no false positives stored
**Cons**: Can't recover if classifier is wrong

### Option C: Auto-Suppress with Review Queue

Save all detections but auto-suppress low-confidence ones.

```python
# In faces.py detect_faces()
for detected_face in mtcnn_results:
    embedding = compute_embedding(detected_face)
    prob = classifier.predict_proba(embedding)

    save_face(detected_face,
              suppressed=(prob < threshold),
              classifier_score=prob)
```

**Pros**: Nothing lost, can review borderline cases
**Cons**: Still stores false positives

### Option D: Batch Cleanup Tool

Run periodically on unknown faces to suggest suppressions.

```python
# CLI command: python -m imaginary.cleanup_faces
unknown_faces = get_unknown_faces()
for face in unknown_faces:
    prob = classifier.predict_proba(face.embedding)
    if prob < 0.3:
        suppress_face(face.id)  # High confidence false positive
    elif prob < 0.7:
        flag_for_review(face.id)  # Uncertain, needs human
```

**Pros**: Non-invasive, can tune thresholds
**Cons**: Requires manual runs

## Evaluation Strategy

### Metrics

- **Precision**: Of faces we keep, how many are real?
- **Recall**: Of real faces, how many do we keep?
- **F1 Score**: Balance of precision/recall

For this use case, **high precision** is more important - we'd rather manually suppress a few false positives than accidentally suppress real faces.

### Cross-Validation

```python
from sklearn.model_selection import cross_val_score

scores = cross_val_score(clf, X, y, cv=5, scoring='precision')
print(f"Precision: {scores.mean():.3f} (+/- {scores.std():.3f})")
```

### Threshold Tuning

```python
from sklearn.metrics import precision_recall_curve

y_scores = clf.predict_proba(X_test)[:, 1]
precisions, recalls, thresholds = precision_recall_curve(y_test, y_scores)

# Find threshold for 99% precision (accept 1% false positive rate)
idx = np.argmax(precisions >= 0.99)
optimal_threshold = thresholds[idx]
```

## Quick Experiment Script

To test feasibility before full implementation:

```python
#!/usr/bin/env python
"""Quick experiment to test false positive classifier feasibility."""

import sqlite3
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

conn = sqlite3.connect('imaginary.db')

# Load real faces (identified)
cur = conn.execute('''
    SELECT embedding, semantic_embedding
    FROM faces
    WHERE person_id IS NOT NULL AND suppressed = 0
''')
real = [(np.frombuffer(r[0], dtype=np.float32),
         np.frombuffer(r[1], dtype=np.float32) if r[1] else None)
        for r in cur.fetchall()]

# Load fake faces (suppressed, non-sentinel)
cur = conn.execute('''
    SELECT embedding, semantic_embedding
    FROM faces
    WHERE suppressed = 1 AND box_w > 0
''')
fake = [(np.frombuffer(r[0], dtype=np.float32),
         np.frombuffer(r[1], dtype=np.float32) if r[1] else None)
        for r in cur.fetchall()]

# Filter to faces with both embeddings
real = [r for r in real if r[1] is not None]
fake = [f for f in fake if f[1] is not None]

print(f"Real faces: {len(real)}, Fake faces: {len(fake)}")

# Test each embedding type
for name, idx in [("Face", 0), ("Semantic", 1)]:
    X = np.vstack([r[idx] for r in real] + [f[idx] for f in fake])
    y = np.array([1] * len(real) + [0] * len(fake))

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    clf = LogisticRegression(max_iter=1000, class_weight='balanced')
    clf.fit(X_train, y_train)

    print(f"\n{name} Embedding:")
    print(classification_report(y_test, clf.predict(X_test)))

conn.close()
```

## Next Steps

1. Run quick experiment script to validate feasibility
2. Compare face vs semantic vs combined embeddings
3. If promising (e.g., >95% precision at >80% recall):
   a. Train final classifier, save model weights
   b. Add `likely_false_positive` column to faces table
   c. Integrate classifier into face detection pipeline
   d. Add toolbar filter button to Faces screen
   e. Backfill flag for existing unknown faces
4. Tune threshold based on user feedback
