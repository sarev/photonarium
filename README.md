# Imaginary

![Imaginary Logo](logo.png)

Imaginary is a photo catalogue that stays on your computer. It’s for people who want the convenience of modern search and face grouping, without uploading their life to someone else’s servers.

## Why Imaginary exists

Most photo apps push you towards the cloud. That is great until you care about privacy, subscriptions, slow uploads, or working offline.

Imaginary keeps your library local and helps you do the three things people actually want:

- **Find** photos quickly, even when you cannot remember filenames, and exclude what you don't want
- **Tidy** a messy collection, especially duplicates and near-duplicates
- **Organise** around people, favourites, and your own notes

Find out more about the motivations behind Imaginary in [`BACKGROUND.md`](BACKGROUND.md).

## What it can do

- **Fast browsing** with a thumbnail grid that stays smooth on large libraries
- **AI search** that understands what you type (for example: "sunset over mountains", "birthday cake"), with negative terms to exclude concepts (e.g. "beach -people")
- **Face workflows**: detect faces, group unknowns, name people, and use those names later
- **Duplicate finding** at multiple strictness levels (identical, near-identical, similar, related) plus user-curated **custom groups** (albums)
- **Ratings and descriptions** so you can build your own “favourites” system

Once you have run the model downloader, the models stay on your machine. Everything runs locally.

## A quick start (how most people use it)

1. Start Imaginary, then open the web page in your browser.
2. Go to **Database** and **add one or more folders** that contain photos.
3. Let it scan. Big libraries take time, especially face detection.
4. Go back to **Gallery** and start browsing.
5. Use **Search** when you want to find something specific.
6. Use **Groups** when you want to clean up duplicates or organise images into albums.
7. Use **Faces** when you want to name people and improve recognition.

## Getting around

Use the toolbar buttons, or these shortcuts (ignored while you are typing in a text box):

- **Ctrl/Cmd + G**: Gallery
- **Ctrl/Cmd + M**: Manage Database
- **Ctrl/Cmd + D**: Groups
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

### Reviewing groups in the Gallery

If you opened a group into the Gallery, you can move between groups without going back:

- **Alt + Left / Alt + Right** navigates to the previous/next group.
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
- **Sparkle button**: open Quick Match to see likely people matches (for unknown faces)

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
- “summer holiday” + “⭐️⭐️⭐️” + People ("Sam")
- “Red steam train on sunny day”

### Text search (description)

This is semantic, so it matches meaning, not filenames.

- Type a phrase into the description search box.
- Adjust the similarity slider:
  - Lower similarity finds broader matches.
  - Higher similarity is more strict.
- Press **Enter** in the description box to apply.

#### Negative terms

You can exclude concepts from your search by prefixing words with `-`:

- `beach -people` finds beaches without people
- `-indoor sunset` finds sunsets that aren't indoors
- `red train -steam-engine` finds red trains but not steam engines

Hyphens within words are preserved: `double-blind` searches for the phrase "double-blind", not "double" minus "blind".

**Tip:** More terms give better results. A simple query like `beach -people` may still return beach photos with people in them. For stronger exclusion, be more specific: `beach sand sea waves blue skies sunshine -people -man -woman -child -person -crowd` does a much better job of finding empty beaches. The same applies to positive terms: adding synonyms and related words helps the model understand exactly what you want.

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

## Groups

Groups helps you clean up your library by finding duplicates and also lets you organise images into custom albums.

### Duplicate detection (levels 0-3)

- Review duplicate "stacks" (groups) of related images.
- Adjust how strict duplicate matching should be:
  - **Related**, **Similar**, **Near-identical**, **Identical**
- Double click a stack (or press **Enter**) to open that group in the Gallery.
- While viewing a group in the Gallery, use **Alt + Left / Right** to move between groups.

### Custom groups (albums)

Slide to **Custom** (the leftmost position) to manage your own named groups:

- Create, rename, and delete groups from the toolbar.
- Add images to groups via the group button that appears when you hover over a Gallery thumbnail.
- The Group Picker dialog lets you manage which groups an image belongs to.
- An image can belong to multiple groups (overlap allowed).
- Groups are kept even when all images are removed.
- While viewing a custom group in the Gallery, press **Backspace** to remove selected images from the group (does not delete them).

### Semantic sorting

You can sort stacks by similarity to a concept using the semantic sort button. This helps surface particular types of duplicates:

- `blurry` surfaces out-of-focus shots
- `dark underexposed` finds poorly lit images
- `cropped tight` finds heavily cropped versions

Negative terms work here too. Use `blurry -sharp` or `dark -bright -colorful` to push good images down and bad ones up, making it easier to pick which duplicates to delete.

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
  - Sparkle button to open Quick Match (see below)

#### Quick Match

Once you have a few people established with locked faces, Quick Match becomes a powerful way to rapidly identify unknown faces. Click the sparkle button on any unknown face (or select multiple faces and click the sparkle on any of them) to see a card showing the top matching people from your library.

- The card shows up to 5 people, ranked by how closely their locked faces match the selected face(s)
- Click a person to assign all selected faces to them instantly
- Click outside the card or press **Escape** to dismiss without making changes

This is especially useful when you have a large backlog of unknown faces and many established people. Instead of scrolling through the Known People list or remembering names, Quick Match shows you the most likely candidates based on face similarity.

#### Semantic search for faces

The "Search faces..." input at the top of the Unknown Faces grid uses the same semantic search as the main Search screen. You can describe what you're looking for (e.g., "smiling", "glasses", "outdoor") to filter unknown faces. Negative terms work here too: `outdoor -sunglasses` finds outdoor faces without sunglasses.

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
- Use the sparkle button to Quick Match a face to a different person if it was mis-assigned.
- Double click a face to open the source image in the full-screen viewer.

Matching threshold:
- You can adjust the “Matching threshold” slider to re-evaluate which faces belong to this person. Lowering it tends to add more matches, raising it tends to remove weaker matches.
- Locked faces are used as reliable examples when re-evaluating, and changes can add or remove faces for this person.

### Advice on tagging faces

When you first add a folder of images to Imaginary, it will try to spot all of the faces in the images (which can take some time!). This will normally result in the Faces screen showing a lot of 'unknown' faces. Try to find a face for a person you know and enter their name against their image. This will create your first 'person' for the People list. Then, name a few more examples of their face, ideally in different poses and lighting conditions. At this point, you can move onto another person. Follow these steps for a reasonable selection of the people you want to tag (a few images of each). You can drag-and-drop unknown faces onto a person (even multiple at once) to quickly name them.

As you come across faces of people that you're not bothered about naming, you can name them '-'. This is a special name that tells Imaginary that this person is someone you want to ignore.  Clicking on the grey circle with a '-' in it that appears as you hover over an unknown face marks them as a face to ignore. If you have multiple unknown faces selected, they will all be ignored with one click. Moving people under the ignore name is helpful for reducing clutter in the unknown faces list.

Occasionally, you'll come across images in the unknown faces list that aren't faces. These are incorrect detections by the face detection model. You can click the red circle 'x' control that appears when you hover over it to remove this from the faces list altogether (it is essentially forgotten).

When you name (or ignore) a face, it will be 'locked' to the name you've given it. This means two things:

1. Imaginary uses this face to try to find other similar faces and match them to the same name
2. Imaginary won't automatically match this face to any other person, even if it's very similar

In the background, while you are naming faces, or marking them as ignored, Imaginary will work to see if any of the remaining unknown and unlocked faces can be moved under any of the named people. When it does this, the faces will be named appropriately but *unlocked*, so that they might be automatically moved later as it becomes clearer what each person looks like (more locked faces with that name).

Double-clicking on a person's face in the list of people will open a view where you see all of the faces that have been given that name. The ones with a green padlock symbol are locked, clicking this will unlock the face. Clicking a grey (unlocked) padlock symbol will lock the face. Hovering over a face, you'll see the grey '-' (ignore) and the green 'x' (unname) controls appear. This helps you to fine-tune the faces under this person. You can also select the star badge (turns gold) to choose a single face that Imaginary will use as the 'preferred' face for this person - this is the one it uses elsewhere in the app when referencing that person.

A useful trick is to click the open padlock button in the toolbar to show only the unlocked faces for this person (if any). These are all the ones that were automatically assigned. By lowering the "match threshold" slider (and wait a few seconds - this can take a little time), Imaginary will review all unknown and unlocked faces to see if any can move across under this name. A lower threshold means faces don't have to be quite as similar to be considered a match. You can then lock the faces that you are happy really *do* belong to that person, before raising the matching threshold back up to a more strict level.

Clicking the "Focus on one person" button in the toolbar returns you to the main Faces screen.

Finally, once you're reasonably happy you've built up a good cross-section of faces for each person, you can go into the faces list for the 'ignored' list (double-click the '-' entry in the people list), and look for people who you missed (should actually be named). If you find any, just unlock them and you can (hopefully) get Imaginary to automatically move them across to the right place.

---

## Database

Database is where you tell Imaginary where your photos live, and where you can see what the app is currently doing.

- Add folders (scanned recursively)
- Rescan folders to pick up changes
- Watch progress for indexing, embeddings, and face work (with ETAs when possible)

Supported image types include: `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`, and camera RAW formats (`.cr2`, `.cr3`, `.nef`, `.nrw`, `.arw`, `.srf`, `.dng`, `.raf`, `.rw2`, `.orf`, `.pef`, `.srw`, `.x3f`, `.3fr`, `.iiq`, `.rwl`, `.kdc`, `.dcr`, `.erf`). RAW support requires the `rawpy` package. Note that RAW files cannot be rotated within Imaginary. RAW files are also slower to process than standard formats — each file requires full demosaicing of the sensor data, so indexing and opening full-screen images will take a little longer than with JPEGs.

---

# Installation

## Requirements

- Python 3.11 or later
- A CUDA-capable GPU is recommended for faster processing, but not required

## Quick install (recommended)

The installer scripts create a virtual environment, install all dependencies in the correct order, initialise the configuration, and download the ML models. They will ask where to store your data (database, thumbnails, config) and confirm before making changes.

**Windows:**

Open the Imaginary folder in File Explorer and double-click `install.bat`. If Windows SmartScreen shows a "Windows protected your PC" warning, click **More info** then **Run anyway** — the script only installs Python packages and downloads ML models.

Alternatively, open Command Prompt, navigate to the Imaginary folder, and run:

```
install.bat
```

**Linux / macOS:**

Open a terminal, navigate to the Imaginary folder, and run:

```bash
chmod +x install.sh
./install.sh
```

The `chmod` command only needs to be run once (it marks the script as executable).

## Manual installation

If you prefer to install manually, or the installer script doesn't suit your setup, follow these steps.

1. **Create a virtual environment**

   ```bash
   python -m venv env
  ```

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
   # Windows / Linux:
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
   # macOS (use default PyPI — the cu124 index has no macOS wheels):
   # pip install torch torchvision torchaudio

   # Other dependencies (install facenet-pytorch with --no-deps to avoid replacing CUDA torch)
   pip install open_clip_torch
   pip install --no-deps facenet-pytorch
   pip install pillow numpy pyyaml opencv-python imagehash flask waitress requests orjson transformers==4.44.* rawpy exifread
   ```

   Note: you may see pip warnings about facenet-pytorch version conflicts with numpy, Pillow, and torch. These can usually be ignored.

4. **Initialise the configuration**

  This step is optional.

  Imaginary has various aspects of its behaviour which may be tuned. To do this, you might want to run it once just to create the default configuration file. This contains all of the standard settings along with comments to explain how they work.

  ```bash
  # Start the app to display the default models it is configured to use, then automatically quits
  python app.py --list-models
  ```

  This will create the `.imaginary.yml` configuration file. You can load this into a text editor and make changes, if you'd like. For example, you may want to select different 'models' to be used for things like generating image descriptions.

5. **Download ML models**

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
* Once you have several people established, use Quick Match (sparkle button) to rapidly identify unknown faces. It shows you the most likely matches based on face similarity.
* If two people get mixed up, increase that person's recognition threshold.
* Emoji ratings work well for quick favourites, and make filtering pleasant.
* Use negative terms in search (`beach -people`) to exclude concepts from results.

## Licence

Apache 2.0
