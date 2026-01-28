# Imaginary

![Imaginary Logo](logo.png)

A local image catalogue for people who want to organise their photo collection without uploading everything to the cloud.

## Why Imaginary?

Your photos are personal. They live on your hard drive, and that's where they should stay. Imaginary helps you browse, search, and manage your image collection entirely on your own computer. No subscriptions, no cloud uploads, no privacy concerns.

Imaginary is designed to run fast even with huge photo libraries. It uses locally-running AI models for semantic search and face recognition, so you can find photos by describing what's in them ("sunset over mountains", "birthday cake") or by who's in them ("photos of Alice and Bob together"), all without sending your images anywhere.

## Features

- **Smart Search**: Find images by describing their content, not just filenames
- **Face Recognition**: Automatically detect and recognise people in your photos
- **Duplicate Detection**: Find identical, near-identical, similar, and related images
- **Fast Browsing**: Virtual scrolling handles collections of any size smoothly
- **Image Information**: View metadata, dimensions, histograms, and add your own descriptions and ratings
- **Fullscreen Viewer**: Zoom, pan, navigate, and tag faces in your photos
- **Light & Dark Themes**: Easy on the eyes, day or night
- **Fully Offline**: Everything runs locally (AI models download once on first use)

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

On first run, the AI models will download automatically (this only happens once).

### Command Line Options

```bash
python app.py --port 8080              # Use a different port
python app.py --generate-thumbnails    # Pre-generate thumbnails for all images
python app.py --scan                   # Run folder scan on startup
python app.py --detect-faces           # Run face detection on startup
python app.py --group-faces            # Run unknown face grouping on startup
python app.py --scan --detect-faces    # Combine flags as needed
```

By default, no processing runs at startup—add flags to opt in to specific phases.

## Getting Started

1. **Add folders**: Click the Database button and add folders containing your images
2. **Wait for scanning**: Imaginary will index your images, generate AI embeddings, and detect faces
3. **Browse**: Return to the Gallery to see your collection
4. **Search**: Click the Filter button to search by text, date, rating, or people

Note: when you import a folder of images, the indexing process - face recognition in particular - can take a long time! For example, on the developers' machine, it takes about fifteen minutes to ingest 1000 images... With large image libraries, you'll need to be patient during importing before the functionality of the app becomes fully available.

## Using the Gallery

The Gallery is where you'll spend most of your time. It shows your image collection as a grid of thumbnails, with an information panel on the right showing details about the selected image.

### Toolbar

The toolbar across the top gives you quick access to common actions:

**Left side (image actions):**
- **Thumbnail size**: Make thumbnails smaller or larger
- **Fullscreen**: Open the selected image in fullscreen view (or double-click a thumbnail)
- **Open folder**: Reveal the selected image in your file manager
- **Rotate**: Rotate images left or right (changes are saved to disk)
- **Face tagging**: Toggle face tagging mode for the fullscreen viewer

**Centre (navigation):**
- **Database**: Add or remove folders, see scanning progress
- **Duplicates**: Find duplicate and similar images
- **Faces**: Browse and manage detected faces
- **Filter**: Search and filter your collection

**Right side (sorting and selection):**
- **Sort buttons**: Sort by date, rating, content similarity, or people
- **Sort direction**: Toggle between ascending and descending
- **Select all / Clear**: Bulk selection controls
- **Theme**: Switch between light and dark modes

### Sorting Options

Select any image, then click "Sort by content" to reorder the gallery by visual similarity—great for finding related photos. "Sort by people" groups images by who appears in them.

### Image Information Panel

When you select an image, the panel on the right shows:
- Filename and folder path
- Dimensions, file size, and date
- An RGB histogram of the image
- Editable description and rating fields

Add descriptions to help with searching later—the AI search understands natural language, so detailed descriptions make images easier to find.

## Fullscreen Viewer

Double-click any thumbnail (or press Enter) to open it in fullscreen view:

- **Navigate**: Arrow keys, swipe gestures, or click the on-screen arrows
- **Zoom**: Scroll wheel or pinch; double-click toggles between fit-to-screen and actual size
- **Pan**: Click and drag when zoomed in

Press Escape or click the X button to return to the Gallery.

### Tagging Faces

When face tagging mode is enabled (via the toolbar button), detected faces appear as coloured boxes:

- **Red**: Unknown face, not yet identified
- **Green**: Known face, already tagged
- **Orange**: Currently editing

Click any face box to enter a name. As you type, matching names appear for quick selection—the search uses fuzzy matching, so "sro" finds "Steve Rose". Press **Tab** to cycle through unknown faces and tag multiple people quickly.

Once you've tagged someone in a few photos, Imaginary learns to recognise them automatically in other images.

To remove a false detection (something that isn't actually a face), click the **X** button on its bounding box.

## Using the Filter Screen

The Filter screen lets you narrow down which images appear in the Gallery. Combine multiple criteria:

**Text Search**: Enter a description of what you're looking for. This uses AI semantic search, so "people at a beach" will find beach photos even if you never described them that way. When a text search is active, a similarity slider appears in the Gallery toolbar letting you adjust how strict the matching is.

**People**: Click the add button to select one or more people from those you've tagged. The gallery will show images containing all the selected people.

**Date Range**: Set a start date, end date, or both to filter by when photos were taken.

**Rating Filter**: Enter emoji to find images you've rated. If you enter multiple emoji, images matching any of them will be shown.

Click **Apply Filter** to return to the Gallery with your filter active. The filter button highlights when a filter is active; click it again to clear.

## Using the Duplicates Screen

The Duplicates screen helps you find and manage duplicate or similar images.

**Similarity Levels**: Use the slider to control how strict the matching is:
- **Identical** (rightmost) — Exact file matches with the same checksum
- **Near-identical**: Same image at different sizes or compression levels
- **Similar**: Photos from the same sequence or with similar composition
- **Related** (leftmost) — Thematically related images

**Stacks**: Each stack represents a group of similar images. The image shown on top is automatically chosen as the "best" one based on resolution, focus quality, and whether it's losslessly compressed.

**Opening a Stack**: Double-click to open a stack in the Gallery, filtered to show only that group. Use Alt+Left/Right to move between groups.

**Sorting Stacks**: By default, stacks are sorted by size (largest groups first). Use semantic sort to order by a text query—useful for finding specific duplicates like "blurry" or "dark".

## Using the Faces Screen

The Faces screen (Ctrl+P) shows all detected faces across your collection:

- **Known faces** appear first, grouped by person and sorted alphabetically
- **Unknown faces** appear below, grouped by visual similarity (largest groups first)
- Click any face to edit its name or correct a mistaken identification
- Use "Only unknowns" to focus on faces that need tagging
- Adjust thumbnail size with the +/- buttons

**Fuzzy name search**: When typing a name, the autocomplete uses subsequence matching. Type "sro" to find "Steve Rose", or "jd" to find "John Doe".

**Batch tagging**: Select multiple unknown faces (Ctrl+Click or drag-select), then type a name on any selected face. All selected faces will be tagged at once.

### Pick-Preferred Mode

Double-click any known person to enter pick-preferred mode, where you can:

- **Set the avatar**: Click the star on any face to make it the person's representative thumbnail
- **Rename the person**: Click the rename button in the header
- **Remove mistakes**: Select incorrectly-tagged faces and press Delete to return them to the unknown pool
- **Adjust recognition sensitivity**: Use the threshold slider to control how strictly faces are matched to this person. Higher values require closer matches; lower values are more permissive. Faces that no longer meet a raised threshold are automatically returned to the unknown pool.
- **View in context**: Double-click any face to open it in fullscreen view

Press Escape or click the back arrow to return to the main faces view.

## Using the Database Status Screen

This screen allows folders to be added to and removed from the app's image library. Folders are scanned recursively for all compatible image types ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp') to add them to the app's database. You may include subdirectories as distinct folders - the app will only catalogue the contents once. Removing the parent directory from those that the app is watching won't affect any registered subdirectories or their images.

The database screen also shows the status of any ongoing indexing processes, along with an estimated time until completion, where possible.

Clicking the "Rescan all folders" button will start a complete check of all registerd folders to see if any of the images within have been added, removed, or modified since the last time they were indexed. Indexing typically runs automatically every time the app (backend) is started.

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
| Previous/Next group | Toolbar buttons | Toolbar buttons | Alt+Left / Alt+Right |
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
| Next unknown face | — | — | Tab |

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

### Faces View

| Action | Mouse | Touch | Keyboard |
|--------|-------|-------|----------|
| Select face | Click | Tap | Arrow keys |
| Add to selection | Ctrl+Click | — | Shift+Arrows |
| Select range | Shift+Click | — | Shift+Arrows |
| Select all unknowns | — | — | Ctrl+A |
| Clear selection | — | — | Escape |
| Select multiple | Drag box | — | — |
| Enter pick-preferred | Double-click known person | Double-tap | Enter |
| Exit pick-preferred | Click back arrow | Tap back arrow | Escape |
| Open face in fullscreen | Double-click (in pick-preferred) | Double-tap | Enter |
| Delete / unassign face | — | — | Delete |

### Global Shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+G | Go to Gallery |
| Ctrl+M | Go to Database Management |
| Ctrl+D | Go to Duplicates |
| Ctrl+F | Go to Search/Filter |
| Ctrl+P | Go to Faces (People) |

## Configuration

Settings are stored in `.imaginary.yml` (created automatically on first run):

| Setting | Default | Description |
|---------|---------|-------------|
| `thumbnail_quality` | 85 | JPEG quality for thumbnails (1-100) |
| `thumbnail_cache_size_mb` | 100 | RAM cache size for thumbnails |
| `indexing_threads` | 4 | Parallel threads for scanning |
| `face_detection_enabled` | true | Enable automatic face detection |
| `face_detection_min_confidence` | 0.95 | Detection confidence threshold |
| `face_recognition_threshold` | 0.65 | Default similarity threshold for auto-recognition (can be overridden per person in pick-preferred mode) |

## Tips

- **Large collections**: Imaginary handles tens of thousands of images, but initial scanning takes time. Let it run in the background.
- **Face tagging**: Tag a person in 3-5 clear photos and Imaginary will start recognising them automatically.
- **Tuning recognition**: If someone is being confused with another person, increase their recognition threshold in pick-preferred mode. If they're not being recognised in enough photos, lower it.
- **Finding people**: Use the People filter or "Sort by people" to quickly find photos of specific individuals.
- **Duplicate detection**: Start with "Similar" or "Related" to see what's there, then move to stricter levels to find exact copies.
- **Semantic sorting**: In Duplicates view, use semantic sort with queries like "blurry" to find low-quality duplicates to delete.
- **Descriptions & ratings**: Add personal notes and emoji ratings to help you find favourites later.

## License

Apache 2.0
