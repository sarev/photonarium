# Imaginary

![Imaginary Logo](logo.png)

A local image catalogue for people who want to organise their photo collection without uploading everything to the cloud.

## Why Imaginary?

Your photos are personal. They live on your hard drive, and that's where they should stay. Imaginary helps you browse, search, and manage your image collection entirely on your own computer—no subscriptions, no cloud uploads, no privacy concerns.

Imaginary is designed to run fast even with huge photo libraries. It uses a locally-running, AI-powered semantic search, so you can find photos by describing what's in them ("sunset over mountains", "birthday cake", "dog playing in snow") rather than relying on filenames or folders.

## Features

- **Smart Search** — Find images by describing their content, not just filenames
- **Face Recognition** — Automatically detect faces and tag people in your photos
- **Duplicate Detection** — Find identical, near-identical, similar, and related images across your collection
- **Fast Browsing** — Virtual scrolling handles collections of any size smoothly
- **Image Information** — View metadata, dimensions, histograms, and add your own descriptions and ratings
- **Fullscreen Viewer** — Zoom, pan, and navigate through your photos
- **Light & Dark Themes** — Easy on the eyes, day or night
- **Fully Offline** — Everything runs locally (AI models download once on first use)

## Installation

### Requirements

- Python 3.11 or later
- A GPU with CUDA support is recommended for faster AI processing, but not required

### Setup

1. **Create a virtual environment:**
   ```bash
   python -m venv env
   ```

2. **Activate it:**
   ```bash
   # Windows (Command Prompt)
   .\env\Scripts\activate

   # Windows (Git Bash / MinGW)
   . env/Scripts/Activate

   # Linux / macOS
   source env/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   # Upgrade pip
   python -m pip install --upgrade pip

   # PyTorch (with CUDA support for GPU acceleration)
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

   # Other dependencies (install facenet-pytorch with --no-deps to avoid replacing CUDA torch)
   pip install open_clip_torch
   pip install --no-deps facenet-pytorch
   pip install opencv-python imagehash flask waitress requests
   ```

   > **Note:** You may see pip warnings about facenet-pytorch version conflicts with numpy, Pillow, and torch. These can be safely ignored—the package works correctly with newer versions despite its conservative dependency declarations.

## Running Imaginary

```bash
python app.py
```

Then open your browser to **http://localhost:5000**

On first run, the AI model will download automatically (this only happens once).

### Command Line Options

```bash
python app.py --port 8080              # Use a different port
python app.py --generate-thumbnails    # Pre-generate thumbnails for all images
python app.py --detect-faces           # Run face detection on unprocessed images
```

## Getting Started

1. **Add folders** — Click the Database button and add folders containing your images
2. **Wait for scanning** — Imaginary will index your images and generate AI embeddings
3. **Browse** — Return to the Gallery to see your collection
4. **Search** — Click the Filter button to search by text, date, or rating

## Using the Gallery

The Gallery is where you'll spend most of your time. It shows your image collection as a grid of thumbnails, with an information panel on the right showing details about the selected image.

### Toolbar

The toolbar across the top gives you quick access to common actions:

**Left side (image actions):**
- **Thumbnail size** — Make thumbnails smaller or larger to see more images or more detail
- **Fullscreen** — Open the selected image in fullscreen view (or double-click a thumbnail)
- **Open folder** — Reveal the selected image in your file manager
- **Rotate** — Rotate images left or right (changes are saved to disk)
- **Face tagging** — Toggle face tagging mode for the fullscreen viewer

**Centre (navigation):**
- **Database** — Add or remove folders, see scanning progress
- **Duplicates** — Find duplicate and similar images
- **Faces** — Browse and manage detected faces
- **Filter** — Search and filter your collection

**Right side (sorting and selection):**
- **Sort buttons** — Sort by date, rating, content similarity, or people
- **Sort direction** — Toggle between ascending and descending
- **Select all / Clear** — Bulk selection controls
- **Theme** — Switch between light and dark modes

### Sorting by Visual Similarity

A particularly useful feature: select any image, then click the "Sort by content" button. The entire gallery will reorder to show the most visually similar images first. This is great for finding related photos, or discovering images you'd forgotten about.

### Viewing Images

Double-click any thumbnail (or press Enter) to open it in fullscreen view. In fullscreen you can:
- Navigate between images with arrow keys or swipe gestures
- Zoom with scroll wheel or pinch, pan by dragging
- Double-click to toggle between fit-to-screen and actual size

Press Escape or click the X button to return to the Gallery. Your selection and scroll position are preserved.

### Image Information Panel

When you select an image, the panel on the right shows:
- Filename and folder path
- Dimensions, file size, and date
- An RGB histogram of the image
- Editable description and rating fields

Add descriptions to help with searching later—the AI search understands natural language, so detailed descriptions make images easier to find.

See the [Controls](#controls) section for full details on mouse, touch, and keyboard navigation.

## Using the Filter Screen

The Filter screen lets you narrow down which images appear in the Gallery. You can combine multiple criteria:

**Text Search** — Enter a description of what you're looking for. This uses AI semantic search, so "people at a beach" will find beach photos even if you never described them that way. Results are ranked by relevance. When a text search is active, a similarity slider appears in the Gallery toolbar letting you adjust how strict the matching is—lower values return more results.

**Date Range** — Set a start date, end date, or both to filter by when photos were taken. Setting both dates to the same day shows only photos from that specific date.

**Rating Filter** — Enter emoji to find images you've rated. You can type emoji directly or use the picker. If you enter multiple emoji, images matching any of them will be shown.

Click **Apply Filter** to return to the Gallery with your filter active. The filter button in the toolbar will highlight to show a filter is active. Click it again to clear the filter.

## Using the Duplicates Screen

The Duplicates screen helps you find and manage duplicate or similar images in your collection.

**Similarity Levels** — Use the slider to control how strict the matching is:
- **Identical** (rightmost) — Exact file matches with the same checksum
- **Near-identical** — Same image at different sizes or compression levels
- **Similar** — Photos from the same sequence or with similar composition
- **Related** (leftmost) — Thematically related images

**Stacks** — Each stack represents a group of similar images. The count shows how many images are in the group. The image shown on top is automatically chosen as the "best" one based on resolution, focus quality, and whether it's losslessly compressed.

**Opening a Stack** — Double-click (or press Enter) to open a stack. This takes you to the Gallery filtered to show only that group, with the best image pre-selected. Use the toolbar buttons or Alt+Left/Right to move between groups without returning to the Duplicates screen.

**Sorting Stacks** — By default, stacks are sorted by size (largest groups first). Click the semantic sort button to sort by a text query instead—enter a description and stacks will be ordered by how well their best image matches your query. This is useful for finding specific duplicates in a large list.

**Minimum Group Size** — Use the dropdown to hide smaller groups and focus on stacks with more duplicates.

## Using Face Recognition

Imaginary can automatically detect faces in your photos and help you tag people. Once you've identified a few faces, the system learns to recognise those people in other photos.

### Tagging Faces

1. **Enable face tagging mode** — Click the face tagging button in the Gallery toolbar (or press the Faces button)
2. **Open an image** — Double-click to view it in fullscreen
3. **Tag faces** — Each detected face shows a coloured box:
   - **Red** = Unknown face (not yet identified)
   - **Green** = Known face (already tagged)
   - **Orange** = Currently editing
4. **Enter a name** — Click on an unknown face and type the person's name. As you type, matching names will appear for quick selection
5. **Press Tab** — Cycle through unknown faces to quickly tag multiple people in an image
6. **Auto-recognition** — Once you've tagged someone a few times, Imaginary will automatically recognise them in other photos

### The Faces Screen

Click the **Faces** button in the toolbar to browse all detected faces:

- **Known faces** appear first, grouped by person and sorted alphabetically
- **Unknown faces** appear below, ready to be identified
- Click any face to edit its name
- Use the "Only unknowns" toggle to focus on faces that need tagging
- Adjust thumbnail size with the +/- buttons

### Filtering by People

In the Filter screen, you can now filter your gallery by people:

1. Click the **person add** button next to the People field
2. Select one or more people from the picker
3. Click **Done** and apply the filter

The gallery will show only images containing all the selected people.

### Removing False Detections

Sometimes Imaginary detects something that isn't a face. In face tagging mode, click the **X** button on any bounding box to mark it as "not a face". It won't appear again.

## Controls

### Gallery View

| Action | Mouse | Touch | Keyboard |
|--------|-------|-------|----------|
| Select image | Click | Tap | Arrow keys |
| Add to selection | Ctrl+Click | — | Shift+Arrows |
| Toggle selection | Ctrl+Click | — | — |
| Select range | Shift+Click | — | Shift+Arrows |
| Select all | — | — | Ctrl+A |
| Clear selection | — | — | Escape |
| Select multiple | Drag box | — | — |
| Open fullscreen | Double-click | Double-tap | Enter |
| Delete selected | — | — | Delete |
| Previous/Next duplicate group | Toolbar buttons | Toolbar buttons | Alt+Left / Alt+Right |
| Jump to start/end | — | — | Ctrl+Up / Ctrl+Down |
| Page up/down | — | — | Page Up / Page Down |

### Fullscreen Viewer

| Action | Mouse | Touch | Keyboard |
|--------|-------|-------|----------|
| Exit | Click X button | Tap X button | Escape |
| Previous/Next image | Click arrows | Swipe left/right | Arrow keys |
| First/Last image | — | — | Home / End |
| Zoom in/out | Scroll wheel | Pinch | — |
| Toggle zoom (fit ↔ 100%) | Double-click | Double-tap | — |
| Pan (when zoomed) | Click and drag | Drag | — |

### Duplicates View

| Action | Mouse | Touch | Keyboard |
|--------|-------|-------|----------|
| Select stack | Click | Tap | Arrow keys |
| Add to selection | Ctrl+Click | — | Shift+Arrows |
| Select range | Shift+Click | — | Shift+Arrows |
| Select all | — | — | Ctrl+A |
| Clear selection | — | — | Escape |
| Select multiple | Drag box | — | — |
| Open stack in Gallery | Double-click | Double-tap | Enter |
| Jump to start/end | — | — | Ctrl+Up / Ctrl+Down |
| Page up/down | — | — | Page Up / Page Down |

### Global Shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+G | Go to Gallery |
| Ctrl+M | Go to Database Management |
| Ctrl+D | Go to Duplicates |
| Ctrl+F | Go to Search/Filter |
| Ctrl+P | Go to Faces (People) |

## Configuration

Settings are stored in `.imaginary.yml` (created automatically on first run). Key options:

| Setting | Default | Description |
|---------|---------|-------------|
| `thumbnail_quality` | 85 | JPEG quality for thumbnails (1-100) |
| `thumbnail_cache_size_mb` | 100 | RAM cache size for thumbnails |
| `indexing_threads` | 4 | Parallel threads for scanning |
| `face_detection_enabled` | true | Enable automatic face detection |
| `face_detection_min_confidence` | 0.95 | Detection confidence threshold |
| `face_recognition_threshold` | 0.65 | Similarity threshold for auto-recognition |

## Tips

- **Large collections** — Imaginary handles tens of thousands of images, but initial scanning takes time. Let it run in the background.
- **Duplicate detection** — Start with "Similar" or "Related" to see what's there, then move to stricter levels to find exact copies.
- **Descriptions & ratings** — Add personal notes and emoji ratings to help you find favourites later.
- **Semantic sorting** — In Duplicates view, use semantic sort with queries like "blurry" or "dark" to find low-quality duplicates to delete.
- **Face tagging** — Tag a person in 3-5 clear photos and Imaginary will start recognising them automatically in other images.

## License

Apache 2.0
