# Imaginary

![Imaginary Logo](logo.png)

Imaginary is a photo catalogue that stays on your computer. It’s for people who want the convenience of modern search and face grouping, without uploading their life to someone else’s servers.

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

Once you have run the model downloader, the models stay on your machine. Everything runs locally.

## A quick start (how most people use it)

1. Start Imaginary, then open the web page in your browser.
2. Go to **Database** and **add one or more folders** that contain photos.
3. Let it scan. Big libraries take time, especially face detection.
4. Go back to **Gallery** and start browsing.
5. Use **Search** when you want to find something specific.
6. Use **Duplicates** when you want to clean up.
7. Use **Faces** when you want to name people and improve recognition.

## Getting around

Use the toolbar buttons, or these shortcuts (ignored while you are typing in a text box):

- **Ctrl/Cmd + G**: Gallery
- **Ctrl/Cmd + M**: Manage Database
- **Ctrl/Cmd + D**: Duplicates
- **Ctrl/Cmd + S**: Search
- **Ctrl/Cmd + F**: Faces

Common keys across screens:

- **Escape**: go back / close a panel (for example: exit Search, Duplicates, or Database back to Gallery; close dialogs; close full-screen).
- **Enter**: open the selected item (where it makes sense, like opening an image).
- **Delete / Backspace**: remove selected items (where supported, usually with a confirmation).

### Selecting items in grids (thumbnails, duplicate stacks, faces)

Most screens use the same selection behaviour:

Mouse and trackpad:
- **Click** to select.
- **Ctrl/Cmd + click** toggles an item in the selection.
- **Shift + click** selects a range (from the last “anchor” selection).
- **Right click** toggles an item in the selection.
- **Drag on empty space** to draw a selection box:
  - Left button: replaces the selection with what is inside the box
  - Right button: toggles everything inside the box

Touch:
- **Tap** to select.
- **Long press** to add to the selection without clearing.

Keyboard (in any grid view):
- **Arrow keys** move the active selection.
- **Shift + arrows** extends the selection.
- **Page Up / Page Down** moves by a page.
- **Ctrl/Cmd + Up / Down** jumps to first or last item.
- **Ctrl/Cmd + A** selects all.
- **Escape** clears the selection.

---

## Gallery

The Gallery is where you spend most of your time: browsing your library quickly, picking the best shots, and adding a little information (ratings and notes) so you can find things again later.

### What you can do

- Browse a smooth thumbnail grid, even for large libraries.
- Sort the Gallery to find the images you care about.
- Open photos full-screen.
- Delete, rotate, and reveal photos on disk.
- Edit descriptions and ratings in the info panel.

### Sorting

Sort changes the order of the Gallery. Two especially useful modes:

- **Sort by content**: select an image and this button then groups visually similar images, handy for finding related shots.
- **Sort by people**: groups images based on who appears in them (after face detection has run).

### Opening full-screen

Open full-screen with:
- **Double click** a thumbnail, or
- Select one thumbnail and press **Enter**, or
- Use the toolbar button (with one thumbnail selected).

### Quick actions on selected photos

- **Delete / Backspace** deletes selected images (with a confirmation).
- **Rotate left / rotate right** fixes photos that are sideways.
- **Reveal in folder** opens your file manager at the image location (only available when exactly one image is selected).

### Gallery info panel

When you select a photo, the info panel shows basic details and lets you edit:

- **Description** (free text)
  - Press **Enter** to save (Shift+Enter adds a new line).
  - Optionally generate an automatic caption using the sparkle button, then edit it if needed.
- **Rating** (emoji works well for favourites)
  - Use the emoji button to insert emoji quickly.

Descriptions and ratings help when you search later.

### Reviewing duplicates in the Gallery

If you opened a duplicate group into the Gallery, you can move between groups without going back:

- **Alt + Left / Alt + Right** navigates to the previous/next duplicate group.
- The toolbar also shows previous/next group buttons for the same action.

---

## Full-screen viewer

The full-screen viewer is for focused viewing and quick decisions. It gives you fast navigation, zooming, and (optionally) face tagging without breaking your flow.

### Controls

- **Escape** closes full-screen.
- **Left / Right arrows** go to previous or next image.
- **Home / End** go to first or last image in the current order.
- **Mouse wheel** zooms in and out.
- **Double click** toggles zoom level.
- When zoomed in, **click and drag** to pan.

Touch gestures for zoom and pan may vary by device and browser.

### Keyboard shortcuts

These shortcuts use Ctrl on Windows/Linux and Cmd on macOS:

- **Ctrl/Cmd + F** toggles face tagging mode on or off.
- **Ctrl/Cmd + I** ignores all unknown faces in the current image (marks them as `-`).
- **Ctrl/Cmd + R** rotates the image right (90° clockwise).
- **Ctrl/Cmd + L** rotates the image left (90°).
- **Ctrl/Cmd + Backspace** or **Ctrl/Cmd + Delete** deletes the current image and advances to the next one.

---

## Face tagging (in full-screen)

Face tagging helps you name people, ignore false positives, and correct mistakes directly on the photo.

Turn it on and off using the face icon in the full-screen viewer.

### Bounding box colours

- **Green**: this face is named
- **Grey**: this face is ignored (named `-`)
- **Red**: this face is unknown (not named yet)
- **Orange**: you are currently renaming this face (the name field has focus)

### Hover controls on a face box

When you hover a face box, you may see:

- **Grey circle with `-`**: mark this face as ignored
- **Green circle with `x`**: remove the name, returning it to the unknown faces list
- **Red circle with `x`**: remove the bounding box (it is not a face)

### Naming a face

- Click a face’s label and type a name.
- As you type, you’ll see suggestions.
- **Up / Down arrows** move through suggestions.
- **Enter** confirms.
- **Escape** cancels your edit (restores the previous value).
- **Tab / Shift+Tab** cycles through unknown face inputs so you can name several quickly.

As more photos are tagged, Imaginary can recognise that person in other images.

---

## Search

Search lets you narrow a large library down to “just the photos I mean”. It builds a filter, then the Gallery shows only the matching images.

You can combine multiple filters at once, for example:
- “Photos of Sam” + “taken last summer” + “⭐️⭐️⭐️”
- “Anything with ‘concert’ in the description” + “after 2022”

### Text search (description)

This is semantic, so it matches meaning, not filenames.

- Type a phrase into the description search box.
- Adjust the similarity slider:
  - Lower similarity finds broader matches.
  - Higher similarity is more strict.
- Press **Enter** in the description box to apply.

### Date range

- Set a start date and/or end date.
- Leave either blank to make it open-ended.

### Rating

- Type directly into the rating field.
- Or click the emoji button to insert an emoji quickly.

### People

Only available when face detection is enabled and you have named people.

#### People picker dialog

- Type part of a person’s name to narrow the list.
- Click a person to add them to the filter.
- Click them again (in the selected list) to remove them.
- You can also drag and drop people between the available and selected lists.
- **Enter** confirms (unless you’re typing in the search box).
- **Escape** cancels.

### Applying or clearing

- **Apply** uses your current filters and returns to the Gallery.
- **Clear** removes all filters.

Tip: You can also leave Search with **Escape**, returning to the Gallery.

---

## Duplicates

Duplicates helps you clean up your library by grouping photos that are the same, or nearly the same, so you can keep the best version and remove the rest.

### What you can do

- Review duplicate “stacks” (groups) of related images.
- Adjust how strict duplicate matching should be:
  - **Related**, **Similar**, **Near-identical**, **Identical**
- Double click a stack (or press **Enter**) to open that group in the Gallery.
- While viewing a group in the Gallery, use **Alt + Left / Right** to move between groups.

You can also sort stacks, including a semantic sort mode where you type something like “blurry” to surface low-quality duplicates.

---

## Faces

Faces is where you clean up and organise people so you can later filter the Gallery by who is in the photo. It’s designed to be fast to tidy up: name people, ignore false detections, merge duplicates, and choose a good thumbnail for each person.

### Known People list

What you can do:

- Click a person to select them.
- Click the filter icon on a person to open the Gallery filtered to images containing them.
- Drag and drop one person onto another to merge them into the destination person.

Keyboard tips (when the Known People list is focused):
- Arrow keys move between people.
- Enter opens that person in pick-preferred mode.
- Escape clears the selection.

### Unknown Faces grid

This is where you deal with faces that are not named or matched yet.

What you can do:

- Drag an unknown face onto a person in the Known People list to associate it with that person.
- Double click a face to open the source image in the full-screen viewer.
- Hover a face to reveal:
  - Grey `-` to mark the face as ignored
  - Red `x` to remove the bounding box (it is not a face)

Useful workflow:
- When starting from scratch, name a few clear examples of a person, then move on to a different person. Having several people set up before refining any one person helps recognition and reduces mix-ups.

### Pick preferred face (per person)

Open a person to manage them in more detail. This mode is for improving one person at a time:

- Pick a preferred face as their avatar (star).
- Keep a face firmly associated with this person (lock).
- Remove mistakes:
  - Press **Delete** to move a face back to Unknown Faces.
  - Use grey `-` to ignore a face.
  - Use green `x` to un-name it and return it to Unknown Faces.
- Double click a face to open the source image in the full-screen viewer.

Matching threshold:
- You can adjust the “Matching threshold” slider to re-evaluate which faces belong to this person. Lowering it tends to add more matches, raising it tends to remove weaker matches.
- Locked faces are used as reliable examples when re-evaluating, and changes can add or remove faces for this person.

---

## Database

Database is where you tell Imaginary where your photos live, and where you can see what the app is currently doing.

- Add folders (scanned recursively)
- Rescan folders to pick up changes
- Watch progress for indexing, embeddings, and face work (with ETAs when possible)

Supported image types include: `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`.

---

# Installation

## Requirements

- Python 3.11 or later
- A CUDA-capable GPU is recommended for faster processing, but not required

## Setup

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
   pip install pillow numpy pyyaml opencv-python imagehash flask waitress requests orjson transformers==4.44.*
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

Then open `http://localhost:5000`

The app runs entirely offline after models are downloaded.

### Command line options

```bash
python app.py --port 8080              # Use a different port
python app.py --data-dir /path/to/data # Store user data in a specific directory
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
* `face_recognition_threshold`: default similarity threshold for auto-recognition (can be overridden per person in pick preferred mode)
* `caption_model`: BLIP model for image captioning (run `python download_models.py` after changing)
* `caption_max_length`: maximum caption length in tokens
* `caption_min_length`: minimum caption length (higher = more descriptive)

## Tips

* Large imports and database rescans take time. Let it run and come back later.
* Face recognition improves as you tag more clear examples of the same person.
* Add multiple people before refining any one person, this tends to reduce false matches.
* If two people get mixed up, increase that person’s recognition threshold.
* Emoji ratings work well for quick favourites, and make filtering pleasant.

## Licence

Apache 2.0
