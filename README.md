# Imaginary

![Imaginary Logo](logo.png)

A local image catalogue for people who want to organise their photo collection without uploading everything to the cloud.

## Why Imaginary?

Your photos are personal. They live on your hard drive, and that's where they should stay. Imaginary helps you browse, search, and manage your image collection entirely on your own computer—no subscriptions, no cloud uploads, no privacy concerns.

It uses AI-powered semantic search, so you can find photos by describing what's in them ("sunset over mountains", "birthday cake", "dog playing in snow") rather than relying on filenames or folders.

## Features

- **Smart Search** — Find images by describing their content, not just filenames
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

   # Other dependencies
   pip install open_clip_torch pillow opencv-python imagehash numpy pyyaml flask flask-cors waitress
   ```

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
```

## Getting Started

1. **Add folders** — Click the Database button and add folders containing your images
2. **Wait for scanning** — Imaginary will index your images and generate AI embeddings
3. **Browse** — Return to the Gallery to see your collection
4. **Search** — Click the Filter button to search by text, date, or rating

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
| Adjust thumbnail size | Slider or buttons | Slider or buttons | — |

### Fullscreen Viewer

| Action | Mouse | Touch | Keyboard |
|--------|-------|-------|----------|
| Exit | Click X button | Tap X button | Escape |
| Previous/Next image | Click arrows | Swipe left/right | Arrow keys |
| First/Last image | — | — | Home / End |
| Zoom in/out | Scroll wheel | Pinch | — |
| Toggle zoom (fit ↔ 100%) | Double-click | Double-tap | — |
| Pan (when zoomed) | Click and drag | Drag | — |

### Global Shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+G | Go to Gallery |
| Ctrl+M | Go to Database Management |
| Ctrl+D | Go to Duplicates |
| Ctrl+F | Go to Search/Filter |

### Duplicates View

| Action | Mouse/Touch | Keyboard |
|--------|-------------|----------|
| Open stack | Double-click / Double-tap | Enter |
| Previous/Next group | Toolbar buttons | Alt+Left / Alt+Right |

## Configuration

Settings are stored in `.imaginary.yml` (created automatically on first run). Key options:

| Setting | Default | Description |
|---------|---------|-------------|
| `thumbnail_quality` | 85 | JPEG quality for thumbnails (1-100) |
| `thumbnail_cache_size_mb` | 100 | RAM cache size for thumbnails |
| `indexing_threads` | 4 | Parallel threads for scanning |

## Tips

- **Large collections** — Imaginary handles tens of thousands of images, but initial scanning takes time. Let it run in the background.
- **Duplicate detection** — Use the similarity slider to adjust sensitivity. Level 0 finds exact copies, Level 3 finds thematically related images.
- **Descriptions & ratings** — Add personal notes and emoji ratings to help you find favourites later.

## License

MIT
