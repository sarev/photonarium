# Imaginary

![Imaginary Logo](logo.png)

Imaginary is a photo catalogue that stays on your computer.

It is for people who want the convenience of modern search and face grouping, without uploading their life to someone else’s servers.

## Why Imaginary exists

Most photo apps push you towards the cloud. That is great until you care about privacy, subscriptions, slow uploads, or working offline.

Imaginary keeps your library local and helps you do the three things people actually want:

- **Find** photos quickly, even when you cannot remember filenames
- **Tidy** a messy collection, especially duplicates and near-duplicates
- **Organise** around people, favourites, and your own notes

## What it can do

- **Fast browsing** with a thumbnail grid that stays smooth on large libraries
- **AI search** that understands what you type (for example: “sunset over mountains”, “birthday cake”)
- **Face workflows**: detect faces, group unknowns, name people, and use those names later
- **Duplicate finding** at multiple strictness levels (identical, near-identical, similar, related)
- **Ratings and descriptions** so you can build your own “favourites” system

Everything runs locally. On first run, models may download once, then stay on your machine.

## A quick start (how most people use it)

1. **Start Imaginary**, then open the web page in your browser.
2. Go to **Database** and **add one or more folders** that contain photos.
3. Let it scan. Big libraries take time, especially face work.
4. Go back to **Gallery** and start browsing.
5. Use **Filter** when you want to find something specific.
6. Use **Duplicates** when you want to clean up.
7. Use **Faces** when you want to name people and improve recognition.

## Getting around

You can use the toolbar buttons, or these shortcuts:

- **Ctrl or Cmd + G**: Gallery  
- **Ctrl or Cmd + M**: Database  
- **Ctrl or Cmd + D**: Duplicates  
- **Ctrl or Cmd + F**: Filter  
- **Ctrl or Cmd + P**: Faces (people)

(These shortcuts are ignored while you are typing in a text box.)

## Gallery

The Gallery is the main view: a grid of thumbnails with an info panel on the right for the selected image.

### Selecting photos

Selection is designed to work quickly with mouse, keyboard, and touch.

Mouse and trackpad:
- **Click** a thumbnail to select it.
- **Ctrl or Cmd + click** toggles a thumbnail in the selection.
- **Shift + click** selects a range (from the last “anchor” selection).
- **Right click** toggles a thumbnail in the selection.
- **Drag on empty space** to draw a selection box:
  - Left button: replaces the selection with what is inside the box
  - Right button: toggles everything inside the box

Touch:
- **Tap** to select.
- **Long press** on a thumbnail to add it to the selection without clearing.

Keyboard (in any grid view):
- **Arrow keys** move the active selection.
- **Shift + arrows** extends the selection.
- **Page Up / Page Down** moves by a page.
- **Ctrl or Cmd + Up / Down** jumps to first or last item.
- **Ctrl or Cmd + A** selects all.
- **Escape** clears the selection.
- **Enter** opens the selected item in fullscreen view (when a single item is selected).
- **Delete / Backspace** deletes selected items (when the screen supports deletion).

### Sorting

The sort buttons change the order of the gallery. Two are especially useful:

- **Sort by content**: select an image and this button then groups visually similar images, handy for finding “related shots”
- **Sort by people**: groups images based on who appears in them (after face detection has run)

### Gallery Info panel

When you select a photo, the info panel shows basic details and lets you edit fields like:
- **Description** (free text)
- **Rating** (emoji works well for favourites)

Descriptions and ratings also help when you search later.

## Fullscreen viewer

Open fullscreen with:
- **Double click** a thumbnail, or
- Select one thumbnail and press **Enter**, or
- Use the toolbar button (with one thumbnail selected)

Controls:
- **Escape** closes fullscreen.
- **Left / Right arrows** go to previous or next image.
- **Home / End** go to first or last image in the current order.
- **Mouse wheel** zooms in and out.
- **Double click** toggles zoom level.
- When zoomed in, **click and drag** to pan.

Note: touch gestures for zoom and pan are still evolving. The app works best with mouse or trackpad in fullscreen today.

## Face tagging (in fullscreen)

If face detection is enabled and has run, you can turn on face tagging from the toolbar in the Gallery.

What you can do:
- **See face boxes** over the photo
- **Click a face** and type a name
- Use **autocomplete** to avoid re-typing names
- **Enter** commits a name, **Escape** cancels editing
- **Tab** cycles through unknown face inputs (use **Shift + Tab** to go backwards)
- If something is not a real face, click the **X** on the box to suppress it

As more photos are tagged, Imaginary can recognise that person in other images.

## Filter (search and narrow down)

Filter lets you decide what the Gallery shows. You can combine multiple filters.

- **Text search**: type what you are looking for. This is semantic, so it matches meaning, not filenames.
- **People**: pick one or more known people (only available when face detection is enabled).
- **Date range**: start, end, or both.
- **Rating**: use emoji ratings to find favourites.

When a text search is active, a similarity control appears in the Gallery toolbar so you can make the match stricter or looser.

## Duplicates

Duplicates shows “stacks” of images that look the same or related, depending on the strictness level you choose.

- Use the **similarity level slider** to switch between identical, near-identical, similar, and related.
- **Double click** a stack (or press **Enter**) to open that group in the Gallery.
- While viewing a group in the Gallery you can use **Alt + Left / Right** to move between groups.

You can also sort stacks, including a semantic sort mode where you type something like “blurry” to surface low-quality duplicates.

## Faces

Faces is where you clean up and organise people.

- Known people appear first.
- Unknown faces appear below, grouped by similarity.

Useful workflows:
- **Batch tagging**: select several unknown faces, then type a name on one of them to apply it to the whole selection.
- **Only unknowns**: this toolbar button allows you to focus on faces that still need names.

When starting from scratch, it's a good idea to select two or three different-looking images of a given person, name those, and then move onto a different person. Having many people setup before refining any individual person helps the automatic naming process and reduces false-positive matches.

### Pick preferred face (per person)

Open a person to manage them in more detail. In this mode you can:
- Pick a preferred face as their “avatar”
- Rename the person
- Remove mistakes (send wrongly tagged faces back to unknown or rename them)
- Adjust recognition sensitivity for that person

## Database

Database is where you tell Imaginary where your photos live.

- Add folders (scanned recursively)
- Rescan folders to pick up changes
- Watch progress for indexing, embeddings, and face work (with ETAs when possible)

Supported image types include: `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`.

## Installation

### Requirements

- Python 3.11 or later
- A CUDA-capable GPU is recommended for faster processing, but not required

### Setup

1. **Create a virtual environment**
   ```bash
   python -m venv env
````

2. **Activate it**

   ```bash
   # Windows (Command Prompt)
   .\env\Scripts\activate

   # Windows (Git Bash / MinGW)
   . env/Scripts/Activate

   # Linux / macOS
   source env/bin/activate
   ```

3. **Install dependencies**

   ```bash
   # Upgrade pip
   python -m pip install --upgrade pip

   # PyTorch (with CUDA support for GPU acceleration)
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

   # Other dependencies (install facenet-pytorch with --no-deps to avoid replacing CUDA torch)
   pip install open_clip_torch
   pip install --no-deps facenet-pytorch
   pip install pillow numpy pyyaml opencv-python imagehash flask waitress requests orjson transformers
   ```

   Note: you may see pip warnings about facenet-pytorch version conflicts with numpy, Pillow, and torch. These can usually be ignored.

4. **Download ML models**

   ```bash
   python download_models.py
   ```

   This downloads the AI models required for image search and captioning. Models are cached locally and only need to be downloaded once (or when you change model settings).

## Running Imaginary

```bash
python app.py
```

Then open **[http://localhost:5000](http://localhost:5000)**

The app runs entirely offline after models are downloaded.

### Command line options

```bash
python app.py --port 8080              # Use a different port
python app.py --generate-thumbnails    # Pre-generate thumbnails for all images
python app.py --scan                   # Run folder scan on startup
python app.py --detect-faces           # Run face detection on startup
python app.py --group-faces            # Run unknown face grouping on startup
python app.py --scan --detect-faces    # Combine flags as needed
python app.py --list-models            # Output required models as JSON (for scripting)
```

By default, no processing runs at startup. Add flags to opt in to the phases you want.

### Changing ML models

If you change model settings in `.imaginary.yml`, run the model downloader again:

```bash
python download_models.py
```

Available caption models (from smallest to largest):
* `Salesforce/blip-image-captioning-base` (~1GB, fastest)
* `Salesforce/blip-image-captioning-large` (~2GB, default)
* `Salesforce/blip2-opt-2.7b` (~5GB, better quality)
* `Salesforce/blip2-flan-t5-xl` (~8GB, most descriptive)

## Configuration

Settings are stored in `.imaginary.yml` (created automatically on first run). Examples:

* `thumbnail_quality`: JPEG quality for thumbnails (1 to 100)
* `thumbnail_cache_size_mb`: RAM cache size for thumbnails
* `indexing_threads`: parallel threads for scanning
* `face_detection_enabled`: enable automatic face detection
* `face_detection_min_confidence`: detection confidence threshold
* `face_recognition_threshold`: default similarity threshold for auto-recognition
  (this can be overridden per person in pick preferred mode)
* `caption_model`: BLIP model for image captioning (run `python download_models.py` after changing)
* `caption_max_length`: maximum caption length in tokens
* `caption_min_length`: minimum caption length (higher = more descriptive)

## Tips

* Large imports and database rescans take time. Let it run and come back later.
* Face recognition improves as you tag more clear examples of the same person.
* Add multiple people before multiple images of a specific person, this will speed up automatic tagging.
* If two people get mixed up, increase that person’s recognition threshold.
* Emoji ratings work well for quick favourites, and make filtering pleasant.

## Licence

Apache 2.0
