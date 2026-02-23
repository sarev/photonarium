# Photonarium

![Photonarium Logo](www/images/logo.png)

[Photonarium](http://photonarium.org/) is a photo catalogue that stays on your computer. It's for people who want the convenience of modern search and face grouping, without uploading their life to someone else's servers.

## Contents

- [Why Photonarium exists](#why-photonarium-exists)
- [What it can do](#what-it-can-do)
- [A quick start](#a-quick-start-how-most-people-use-it)
- [What to expect](#what-to-expect)
- [Getting around](#getting-around)
- [Gallery](#gallery)
- [Full-screen viewer](#full-screen-viewer)
- [Slideshow](#slideshow)
- [Face tagging](#face-tagging-in-full-screen)
- [Search](#search)
- [Groups](#groups)
- [Smart Groups](#smart-groups)
- [Faces](#faces)
- [Database](#database)
- [Docker Installation](#docker-installation)
- [Manual Installation](#manual-installation)
- [Running Photonarium](#running-photonarium)
- [Configuration](#configuration)
- [Tips](#tips)
- [Acknowledgements](#acknowledgements)

## Why Photonarium exists

Most photo apps push you towards the cloud. That is great until you care about privacy, subscriptions, slow uploads, or working offline.

Photonarium keeps your library local and helps you do the three things people actually want:

- **Find** photos quickly, even when you cannot remember filenames, and exclude what you don't want
- **Tidy** a messy collection, especially duplicates and near-duplicates
- **Organise** around people, favourites, and your own notes

Find out more about the motivations behind Photonarium in [`BACKGROUND.md`](BACKGROUND.md).

## What it can do

- **100% offline and private** - runs entirely on your machine. No cloud, no accounts, no tracking. Your images never leave your computer.
- **Multi-device sync** - use Photonarium from multiple devices at once. Changes made on one (naming faces, rating photos) appear on all others within seconds.
- **Mobile friendly** - browse your library from any device on your network. The responsive layout adapts to phones and tablets in both portrait and landscape.
- **AI-powered search** that understands what you type (e.g. "sunset over mountains", "birthday cake"), with negative terms to exclude concepts (e.g. "beach -people")
- **Face recognition** - automatic face detection and recognition. Name faces and Photonarium finds them across your library.
- **Quality scoring** - AI aesthetic scoring ranks your images by visual quality. Find your best shots instantly.
- **Slideshow** mode in the full-screen viewer with smooth cross-fade transitions, linear or shuffled playback, and configurable timing
- **Smart Groups** - saved searches that stay up to date automatically. Set your filter criteria once and matching images appear whenever you open the group, even photos added later.
- **Duplicate detection** at four levels of similarity (identical, near-identical, similar, related) plus auto-generated **directory groups** and user-curated **custom groups** (albums), with **Refine Groups** to filter by quality and view the best (or worst) images in the Gallery, or prune duplicates to trash
- **Camera RAW support** - native support for 20+ camera RAW formats alongside JPEG, PNG, and other standard image types. No conversion needed.
- **Camera data** - full EXIF metadata extraction. Search and filter by camera, lens, ISO, aperture, shutter speed, and more.
- **Import into catalogue** - copy images from SD cards, phone uploads, or downloads into a managed catalogue directory, organised by date. Preflight dedup avoids importing files you already have.
- **Ratings and descriptions** so you can build your own favourites system

Once you have run the model downloader, the models stay on your machine. Everything runs locally.

## A quick start (how most people use it)

1. Start the Photonarium app in a terminal window.
2. Open the Photonarium web page in your browser, the default is http://localhost:5000
3. Go to **Database** and **add one or more folders** that contain photos.
4. Let it scan. Big libraries take time, especially face detection.
5. Go to **Gallery** and start browsing.
6. Use **Search** when you want to find specific images.
7. Use **Groups** when you want to clean up duplicates or organise images into albums.
8. Use **Faces** when you want to name people and improve recognition.

## What to expect

Photonarium is currently in **beta**. It works, and people are using it day-to-day, but installation is still a manual process, the interface is evolving, and you may encounter rough edges. If something breaks or feels wrong, please [open an issue](https://github.com/sarev/photonarium/issues) - feedback during this stage is especially valuable.

### Intended setup

Photonarium is a **desktop application** that runs in your browser, not a mobile app. The backend (Python) and the frontend (the browser tab) are designed to run on the same machine - typically your laptop, desktop PC, or a home server where your photos are stored.

It also works over a local network: you can run the backend on one machine (say, a NAS or always-on PC) and open the UI in a browser on another. Most features work fine in this setup, with a few caveats:

- **Performance** it may be a little slower, for two reasons: 1) the backend machine needs some grunt, ideally a decent GPU, especially during image ingestion, and 2) quite a bit of data between the backend and UI needs to go over your local network. 
- **Reveal in folder** opens a file-manager window on the machine running the *backend*, which is only useful if that's also the machine you're sitting at. On a headless server this will either silently fail or pop a window nobody sees.
- **The folder picker** (for adding image directories) likewise opens on the backend machine. If you're accessing Photonarium remotely, use the CLI instead: `python app/app.py --add-folder /path/to/photos`.

### Mobile devices

The UI adjusts for smaller screens and touch input - the toolbar collapses to a hamburger menu, touch-friendly scroll zones appear, and layout stacks vertically. It's usable for browsing and basic tasks on a phone or tablet, but the full experience (drag-box selection, keyboard shortcuts, side-by-side info panel) is designed for a desktop browser. Think of mobile as a handy way to flick through your library on the sofa, not a replacement for the desktop workflow.

### Multi-user and security

Photonarium has **no user accounts, no login, and no access control**. Anyone who can reach the server's address can view and modify your library. This is fine for personal use and trusted home networks, but you should not expose it to the public internet.

Multiple browser tabs or devices on the same network can use Photonarium at the same time. Changes made on one client, naming a face, rating an image, creating a group, trashing a photo, are automatically pushed to every other open client within a couple of seconds. If a client falls too far behind (e.g. a laptop lid was closed for a while), it detects the gap and silently reloads to catch up. If your browser loses the connection with the Photonatium backend for any reason, your changes are blocked with a warning message until the connection is restored.

## Getting around

Use the toolbar buttons, or these shortcuts (ignored while you are typing in a text box):

- **Ctrl/Cmd + G**: Gallery
- **Ctrl/Cmd + M**: Manage Database
- **Ctrl/Cmd + D**: Groups (Duplicates and Albums)
- **Ctrl/Cmd + S**: Search and Filter
- **Ctrl/Cmd + F**: Faces and People

Common keys across screens:

- **Escape**: go back / close a panel (for example: exit Search, Duplicates, or Database back to Gallery; close dialogs; close full-screen).
- **Enter**: open the selected item (where it makes sense, like opening an image).
- **Delete / Backspace**: remove selected items (where supported, usually with a confirmation).

### Selecting items in grids (thumbnails, duplicate stacks, faces)

Most screens use the same selection behaviour:

Mouse and trackpad:
- **Click** to select.
- **Ctrl/Cmd + click** toggles an item in the selection.
- **Shift + click** selects a range (from the last "anchor" selection).
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

Sort changes the order of the Gallery. Three especially useful modes:

- **Sort by quality**: appears when viewing a duplicate group or custom group. Ranks images by aesthetic quality, sharpness, and resolution so the best version is at the top.
- **Sort by content**: select an image and this button then groups visually similar images, handy for finding related shots.
- **Sort by people**: groups images based on who appears in them (after face detection has run).

### Opening full-screen

Open full-screen with:
- **Double click** a thumbnail, or
- Select one thumbnail and press **Enter**, or
- Use the toolbar button (with one thumbnail selected).

### Quick actions on selected photos

- **Delete / Backspace** moves selected images to the trash directory (with a confirmation).
- **Rotate left / rotate right** fixes photos that are sideways.
- **Reveal in folder** opens your file manager at the image location (only available when exactly one image is selected).

### Gallery info panel

The info panel sits to the right of the thumbnail grid. Click the toggle at its edge to collapse or expand it. On narrow screens or when the panel would take more than 20% of the viewport, it collapses automatically. Your preference is remembered across sessions.

When you select a photo, the info panel shows basic details and lets you edit:

- **Description** (free text)
  - Press **Enter** to save (Shift+Enter adds a new line).
  - Optionally generate an automatic caption using the sparkle button, then edit it if needed.
- **Rating** (emoji works well for favourites)
  - Use the emoji button to insert emoji quickly.

Descriptions and ratings help when you search later.

- **Metadata** opens a dialog showing EXIF data extracted from the image file (camera, lens, focal length, aperture, shutter speed, ISO, and so on). If any field looks interesting, click the filter icon next to it to select it. You can select several fields, then click **Done** to jump straight to Search with those values pre-filled as metadata filters.

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
- **Ctrl/Cmd + R** rotates the image right (90 deg clockwise).
- **Ctrl/Cmd + L** rotates the image left (90 deg).
- **Ctrl/Cmd + Backspace** or **Ctrl/Cmd + Delete** moves the current image to trash and advances to the next one.

---

## Slideshow

The slideshow lets you sit back and watch your photos play through automatically, with smooth cross-fade transitions between images.

### Starting a slideshow

- In the full-screen viewer, click the **play** button in the toolbar for linear playback (in the current sort order), or the **shuffle** button for random order.
- On the Groups screen, hover over any group stack to reveal play and shuffle badges - click one to jump straight into a full-screen slideshow scoped to that group's images.
- Press **Space** to start a linear slideshow from anywhere in the full-screen viewer.

### While a slideshow is running

- **Space** pauses or resumes.
- **Escape** stops the slideshow and exits full-screen.
- **Left / Right arrows** manually skip to the previous or next image. The slideshow continues from the new position.
- Moving the mouse or pressing any key resets the hold timer, so the current image stays on screen a little longer while you interact.

Clicking the other mode button (play or shuffle) while a slideshow is running switches to that mode without stopping.

### Timing

The hold duration (how long each image stays on screen) defaults to 5 seconds and can be changed in `photonarium.yml` with the `slideshow_interval` setting (in seconds).

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

- Click a face's label and type a name.
- As you type, you'll see suggestions.
- **Up / Down arrows** move through suggestions.
- **Enter** confirms.
- **Escape** cancels your edit (restores the previous value).
- **Tab / Shift+Tab** cycles through unknown face inputs so you can name several quickly.

As more photos are tagged, Photonarium can recognise that person in other images.

---

## Search

Search lets you narrow a large library down to "just the photos I mean". It builds a filter, then the Gallery shows only the matching images.

You can combine multiple filters at once, for example:
- "summer holiday" + "***" + People ("Sam")
- "Red steam train on sunny day"

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

### People

Only available when face detection is enabled and you have named people.

If you mention a known person's name in the description field (e.g. "bob at the beach"), Photonarium automatically recognises it and adds a people filter chip as you type. Multi-word names like "Mary Jane" are matched in preference to shorter overlapping names. When you apply the filter, recognised names are stripped from the search text so the AI focuses on the descriptive content.

You can also add people manually using the picker:

#### People picker dialog

- Type part of a person's name to narrow the list.
- Click a person to add them to the filter.
- Click them again (in the selected list) to remove them.
- You can also drag and drop people between the available and selected lists.
- **Enter** confirms (unless you're typing in the search box).
- **Escape** cancels.

### Date range

- Set a start date and/or end date.
- Leave either blank to make it open-ended.

### Rating

- Type directly into the rating field.
- Or click the emoji button to insert an emoji quickly.

### Metadata

Filter by EXIF camera settings such as Camera, Lens, ISO, Aperture, Shutter Speed, and others. This is useful for finding all images taken with a particular camera body, lens, or shooting settings.

- Click the metadata area or the camera button to open the metadata picker.
- Type into any field to search - matching is fuzzy (subsequence), so "nkn" will find "Nikon" and "d85" will find "D850".
- As you type, a dropdown shows matching values from your library.
- Click **Done** to confirm your choices. Each filled field appears as a chip in the filter bar.
- Click the **x** on a chip to remove that criterion.
- Multiple metadata filters are combined with AND (all must match).

You can also add metadata filters directly from the Gallery: open the info panel, click **View EXIF data**, then click the filter icon next to any value you want to filter by.

### Applying, saving, or clearing

- **Apply** uses your current filters and returns to the Gallery.
- **Save as Smart Group** saves your current filters as a [Smart Group](#smart-groups) - a dynamic group that re-evaluates the criteria each time you open it.
- **Clear** removes all filters.

Tip: You can also leave Search with **Escape**, returning to the Gallery.

---

## Groups

Groups helps you clean up your library by finding duplicates and also lets you organise images into custom albums.

### Duplicate detection (levels 0-3)

- Review duplicate "stacks" (groups) of related images.
- Adjust how strict duplicate matching should be:
  - **Related**, **Similar**, **Near-identical**, **Identical**
- Double click a stack (or press **Enter**) to open that group in the Gallery, automatically sorted by quality with the best image selected.
- While viewing a group in the Gallery, use **Alt + Left / Right** to move between groups.
- **Refine Groups**: click the refine button to filter groups by quality. Choose how many images to keep (or trash) per group - best only, top N, or top N%. Two actions are available:
  - **View in Gallery** shows the selected subset (the best or worst images) as a filtered Gallery view, without changing anything. Works for all group levels including directories and custom groups.
  - **Trash** moves the non-kept images to the trash directory (levels 0-4 only). Uses the same quality scoring as the Gallery Quality sort.

### Directory groups

Slide to **Directories** to see your images organised by folder. Directory groups are automatically created and kept in sync whenever a folder is scanned:

- Each directory that contains images becomes a group, named after the folder.
- When two folders share the same name, parent directories are added to make names unique (e.g. `Holiday/Beach` vs `Birthday/Beach`).
- Hover over a directory group name to see the full path.
- Directory groups are read-only - they mirror the filesystem and update automatically.

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

### Quality sorting

When you open a group in the Gallery, images are automatically sorted by **Quality** with the best image pre-selected. The quality score is a blend of several factors:

- **Aesthetic appeal** (60%) - how visually pleasing the image is, scored by two neural networks (NIMA and LAION) that were trained on large datasets of human aesthetic judgements.
- **Sharpness** (20%) - how well-focused the image is, measured by Laplacian variance.
- **Resolution** (15%) - total pixel count (higher resolution = better).
- **Compression quality** (5%) - bits per pixel, which favours less-compressed originals over heavily compressed copies.

The aesthetic component uses the raw model scores (divided by 10 to normalise to 0-1), so a score of 60% reflects a genuine 6/10 from the models rather than a relative ranking. Sharpness, resolution, and compression quality are percentile-ranked within the current image set, since they have no natural absolute scale.

These weights can be adjusted in `photonarium.yml` to suit your preferences:

- `quality_weight_aesthetic`, `quality_weight_sharpness`, `quality_weight_pixels`, `quality_weight_bpp` - the four component weights (should sum to 1.0).
- `quality_alpha` - controls how the two aesthetic models are blended (0.0 = LAION only, 1.0 = NIMA only, default 0.60 = a mix of both, leaning more to NIMA).
- `nima_enabled` - set to `false` to skip NIMA scoring entirely (quality falls back to LAION with sharpness and resolution).

NIMA and LAION approach aesthetics differently. NIMA was trained on hundreds of thousands of photos rated by people, so it has a good sense of what makes a photograph look appealing - composition, lighting, colour. LAION is faster and lighter but more impressionistic; it can favour vibrant or striking images even if they're technically flawed. Blending the two gives more balanced results than either alone, which is why both are used by default.

---

## Smart Groups

Regular custom groups are like photo albums - you add specific images by hand. Smart Groups are more like saved searches: you define what you are looking for (a text description, a date range, certain people, camera settings, a rating - any combination of the filters on the Search screen) and Photonarium finds matching images every time you open the group. If new photos are added to your library later, they appear automatically when they match the criteria.

### Creating a Smart Group

1. Go to the **Search** screen and set up the filters you want (text, date range, rating, people, metadata - any combination).
2. Click **Save as Smart Group**.
3. Enter a name when prompted.

The new group appears on the Groups screen alongside your regular custom groups.

### Viewing a Smart Group

On the Groups screen, slide to **Custom** to see your Smart Groups mixed in with regular groups. Smart Groups are easy to spot: they show "Smart Group" in italics beneath the name instead of an image count.

Double-click (or press **Enter**) to open the group. Photonarium evaluates the saved filters and opens the Gallery with the matching images. This can take a moment if the filter includes a text search, since it runs a fresh semantic search each time.

### Editing a Smart Group

Hover over a Smart Group stack to reveal an **edit** badge (green circle, top-right). Click it to go back to the Search screen with the saved filters pre-loaded. Adjust anything you like, then click **Update Smart Group** to save the changes. Close the Search screen and the button returns to "Save as Smart Group" for new groups.

### How they differ from regular groups

| | Regular group | Smart Group |
|---|---|---|
| Membership | You add and remove images by hand | Determined automatically by filter criteria |
| Stays current | Only contains what you put in | Picks up new matching photos automatically |
| Add images via Gallery | Yes (group picker) | No - membership is dynamic |
| Slideshow from stack | Yes (hover badges) | Open the group first, then start a slideshow from fullsceen view |

You can rename and delete Smart Groups the same way as regular groups. The Gallery's "Add to Group" picker only shows regular groups, since adding a specific image to a dynamic group would not make sense.

---

## Faces

Faces is where you clean up and organise people so you can later filter the Gallery by who is in the photo. It's designed to be fast to tidy up: name people, ignore false detections, merge duplicates, and choose a good thumbnail for each person.

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
- You can adjust the "Matching threshold" slider to re-evaluate which faces belong to this person. Lowering it tends to add more matches, raising it tends to remove weaker matches.
- Locked faces are used as reliable examples when re-evaluating, and changes can add or remove faces for this person.

### Advice on tagging faces

When you first add a folder of images to Photonarium, it will try to spot all of the faces in the images (which can take some time!). This will normally result in the Faces screen showing a lot of 'unknown' faces. Try to find a face for a person you know and enter their name against their image. This will create your first 'person' for the People list. Then, name a few more examples of their face, ideally in different poses and lighting conditions. At this point, you can move onto another person. Follow these steps for a reasonable selection of the people you want to tag (a few images of each). You can drag-and-drop unknown faces onto a person (even multiple at once) to quickly name them.

As you come across faces of people that you're not bothered about naming, you can name them '-'. This is a special name that tells Photonarium that this person is someone you want to ignore.  Clicking on the grey circle with a '-' in it that appears as you hover over an unknown face marks them as a face to ignore. If you have multiple unknown faces selected, they will all be ignored with one click. Moving people under the ignore name is helpful for reducing clutter in the unknown faces list.

Occasionally, you'll come across images in the unknown faces list that aren't faces. These are incorrect detections by the face detection model. You can click the red circle 'x' control that appears when you hover over it to remove this from the faces list altogether (it is essentially forgotten).

When you name (or ignore) a face, it will be 'locked' to the name you've given it. This means two things:

1. Photonarium uses this face to try to find other similar faces and match them to the same name
2. Photonarium won't automatically match this face to any other person, even if it's very similar

In the background, while you are naming faces, or marking them as ignored, Photonarium will work to see if any of the remaining unknown and unlocked faces can be moved under any of the named people. When it does this, the faces will be named appropriately but *unlocked*, so that they might be automatically moved later as it becomes clearer what each person looks like (more locked faces with that name).

Double-clicking on a person's face in the list of people will open a view where you see all of the faces that have been given that name. The ones with a green padlock symbol are locked, clicking this will unlock the face. Clicking a grey (unlocked) padlock symbol will lock the face. Hovering over a face, you'll see the grey '-' (ignore) and the green 'x' (unname) controls appear. This helps you to fine-tune the faces under this person. You can also select the star badge (turns gold) to choose a single face that Photonarium will use as the 'preferred' face for this person - this is the one it uses elsewhere in the app when referencing that person.

A useful trick is to click the open padlock button in the toolbar to show only the unlocked faces for this person (if any). These are all the ones that were automatically assigned. By lowering the "match threshold" slider (and wait a few seconds - this can take a little time), Photonarium will review all unknown and unlocked faces to see if any can move across under this name. A lower threshold means faces don't have to be quite as similar to be considered a match. You can then lock the faces that you are happy really *do* belong to that person, before raising the matching threshold back up to a more strict level.

Clicking the "Focus on one person" button in the toolbar returns you to the main Faces screen.

Finally, once you're reasonably happy you've built up a good cross-section of faces for each person, you can go into the faces list for the 'ignored' list (double-click the '-' entry in the people list), and look for people who you missed (should actually be named). If you find any, just unlock them and you can (hopefully) get Photonarium to automatically move them across to the right place.

---

## Database

Database is where you tell Photonarium where your photos live, import new images, and see what the app is currently doing.

- Add folders (scanned recursively)
- Import images into a managed catalogue directory (see Import below)
- Rescan folders to pick up changes
- Watch progress for indexing, embeddings, face work, and imports (with ETAs when possible)
- Click **Edit Settings** to open the in-app settings editor (works from any device on your network)

### Import

The Database screen has an Import section that lets you copy images into a Photonarium-managed directory, organised by date (`YYYY/YYYY-MM-DD/filename.jpg`). The originals are not modified. By default, the catalogue lives at `<data-dir>/catalogue/` -- set `catalogue_dir` in settings to use a different location.

**On desktop:**
- Drag and drop images or folders onto the import drop zone, or use the Pick Folder / Pick Photos buttons.
- When you drop a folder, a choice dialog lets you either reference the folder in place (Add Folder) or copy its contents into the catalogue (Import).
- File drops always import.

**On mobile:**
- Tap Pick Photos to open the system photo picker. On Android, a Pick Folder button is also available.
- Before uploading, the browser sends file names and sizes to the backend for a fast duplicate check. Only new files are uploaded, saving bandwidth.

Imported files are processed by the normal indexing pipeline (thumbnails, embeddings, face detection) automatically.

Supported image types include: `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`, and camera RAW formats (`.cr2`, `.cr3`, `.nef`, `.nrw`, `.arw`, `.srf`, `.dng`, `.raf`, `.rw2`, `.orf`, `.pef`, `.srw`, `.x3f`, `.3fr`, `.iiq`, `.rwl`, `.kdc`, `.dcr`, `.erf`). RAW support requires the `rawpy` package. Note that RAW files cannot be rotated within Photonarium. RAW files are also slower to process than standard formats - each file requires full demosaicing of the sensor data, so indexing and opening full-screen images will take a little longer than with JPEGs.

---

# Docker Installation

Docker is the easiest way to run Photonarium, especially on NAS devices (Synology, QNAP, Unraid, etc.) or any system where you want a self-contained deployment. All ML models are pre-downloaded in the image, so you can start using Photonarium immediately.

## Quick Start

Pull and run the CPU image (works on any system):

```bash
# Create directories for persistent data
mkdir -p ~/photonarium/config ~/photonarium/catalogue

# Run the container
docker run -d \
  --name photonarium \
  -p 5000:5000 \
  -v ~/photonarium/config:/config \
  -v ~/photonarium/catalogue:/catalogue \
  -v /path/to/your/photos:/photos:ro \
  -e PUID=$(id -u) \
  -e PGID=$(id -g) \
  7thsw/photonarium:latest \
  --add-folder /photos --scan
```

Then open `http://localhost:5000` in your browser. Your photos will start indexing automatically.

The `--add-folder /photos` flag registers the mounted photo directory (only needed on first run - folders are saved in the database). The `--scan` flag triggers indexing. The `--add-folder` flag is needed because Docker runs in headless mode, which hides the "Add Folder" button (native folder picker dialogs don't work without a display). The folder list and Rescan button remain available in the web UI.

On subsequent runs, you can omit `--add-folder` and just use `--scan` to pick up new images, or omit both flags entirely and use the **Rescan Local Folders** button in the web UI.

## Image Variants

Pre-built images are available on DockerHub at `7thsw/photonarium`:

| Tag | Size | Best For |
|-----|------|----------|
| `latest` / `cpu` | ~4.5 GB | Most NAS devices, systems without a dedicated GPU |
| `cu118` | ~8 GB | NVIDIA GTX 10-series, RTX 20-series (CUDA 11.8) |
| `cu126` | ~10 GB | NVIDIA RTX 30-series, 40-series (CUDA 12.6) |
| `cu128` | ~10 GB | NVIDIA RTX 50-series / Blackwell (CUDA 12.8) |
| `intel` | ~5 GB | Intel integrated graphics (Celeron/Atom NAS CPUs) |
| `arm64` | ~4 GB | ARM64 systems (Raspberry Pi 4/5, Apple Silicon) |

The CPU and CUDA images are x86_64 only. Use the `arm64` tag for ARM-based systems. The CPU/arm64 images work without a dedicated GPU but process images more slowly. If you have a supported GPU, use the matching CUDA or Intel variant for significantly faster indexing and face detection.

## Using Docker Compose

Docker Compose makes it easier to manage configuration. Create a `docker-compose.yml` file:

```yaml
services:
  photonarium:
    container_name: photonarium
    image: 7thsw/photonarium:latest
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      # Application data (database, thumbnails, models)
      - ./config:/config
      # Catalogue for imported photos
      - ./catalogue:/catalogue
      # Your photo library (read-only recommended)
      - /path/to/your/photos:/photos:ro
      # Timezone sync
      - /etc/localtime:/etc/localtime:ro
    environment:
      - PUID=1000
      - PGID=1000
    # Register photo folder and start indexing
    command: --add-folder /photos --scan --detect-faces
```

Then run:

```bash
docker compose up -d
```

The `command:` line registers your photo folder and starts processing. The `--add-folder` flag is idempotent (safe to repeat), so leaving it in the compose file is fine - it won't create duplicates. On subsequent container restarts, registered folders are rescanned for new images.

### Multiple Photo Folders

Mount each photo folder separately and register them all in the command:

```yaml
volumes:
  - ./config:/config
  - ./catalogue:/catalogue
  - /nas/photos/holidays:/photos/holidays:ro
  - /nas/photos/family:/photos/family:ro
  - /nas/photos/archive:/photos/archive:ro
command: >-
  --add-folder /photos/holidays
  --add-folder /photos/family
  --add-folder /photos/archive
  --scan --detect-faces
```

Each `--add-folder` flag registers a folder for indexing. Folders are stored in the database, so subsequent restarts will rescan them even if you remove the `--add-folder` flags from the command.

### Syncing Photos to Your NAS

Photonarium doesn't include built-in phone backup - and that's intentional. NAS vendors and cloud services already have excellent sync tools, and there's no need to reinvent the wheel:

- **Synology**: Use [Cloud Sync](https://www.synology.com/en-us/dsm/feature/cloud_sync) to sync from Google Drive, Dropbox, OneDrive, etc., or [Synology Photos](https://www.synology.com/en-us/dsm/feature/photos) mobile app for phone backup
- **QNAP**: Use [HybridMount](https://www.qnap.com/en/software/hybrid-mount) or [Qsync](https://www.qnap.com/en/software/qsync) for phone backup
- **Unraid/TrueNAS**: Mount cloud storage via rclone, or use Nextcloud for phone backup
- **Any NAS**: Native mobile apps from Apple Photos, Google Photos, OneDrive, and Dropbox can back up to their respective clouds, which you then sync to your NAS

Once photos land on your NAS (however they get there), mount that folder into Photonarium and it will index them. Your existing backup workflow stays unchanged - Photonarium just adds AI-powered search and organisation on top.

## Hardware Acceleration

### NVIDIA GPUs

GPU acceleration dramatically speeds up image indexing and face detection. To enable it:

1. **Install the NVIDIA Container Toolkit** on your host system. Follow the [official installation guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

2. **Use a CUDA-enabled image** that matches your GPU:
   - RTX 30-series, 40-series: `7thsw/photonarium:cu126`
   - RTX 20-series, GTX 10-series: `7thsw/photonarium:cu118`
   - RTX 50-series (Blackwell): `7thsw/photonarium:cu128`

3. **Add GPU access to your container**:

```yaml
services:
  photonarium:
    image: 7thsw/photonarium:cu126
    # ... other settings ...
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
```

To verify GPU access, check the container logs on startup - it should show your GPU device.

### Intel Integrated Graphics

Many NAS devices (Synology, QNAP) have Intel Celeron or Atom CPUs with integrated graphics. The Intel image uses IPEX (Intel Extension for PyTorch) to accelerate computation on these iGPUs.

```yaml
services:
  photonarium:
    image: 7thsw/photonarium:intel
    # ... other settings ...
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - video
      - render
```

Requires `/dev/dri` to be accessible on the host (standard on most Linux systems).

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | 1000 | User ID for file ownership (match your NAS user) |
| `PGID` | 1000 | Group ID for file ownership |
| `PHOTONARIUM_PORT` | 5000 | Port the server listens on |

**Tip:** On Synology/QNAP, find your user's PUID/PGID with `id your_username` via SSH.

### Volumes

| Container Path | Purpose |
|----------------|---------|
| `/config` | Database, thumbnails, configuration file |
| `/catalogue` | Imported photos (organised by date) |
| `/photos` | Your photo library (mount read-only with `:ro`) |

### Configuration File

On first run, Photonarium creates `/config/photonarium.yml` with Docker-appropriate defaults:

- `headless: true` - hides desktop-only features (folder picker dialogs, reveal in explorer)
- `scan_interval_minutes: 60` - automatic rescan every hour (useful if photos sync continuously)

Edit this file to change settings. Most settings can also be changed via the **Edit Settings** button in the web UI.

## Running on Proxmox (LXC Containers)

If you're running Docker inside a Proxmox LXC container, photo directories from the host must be bind-mounted into the LXC before Docker can see them. Docker bind mounts only work on paths that already exist inside the LXC.

**On the Proxmox host** (not inside the LXC), edit the container config:

```bash
# Replace 102 with your LXC container ID
nano /etc/pve/lxc/102.conf

# Add a bind mount for your photo directory:
mp0: /path/to/photos/on/proxmox,mp=/mnt/photos,ro=1
```

Then restart the LXC container. After this, `/mnt/photos` inside the LXC will have your files, and you can use it as a Docker volume:

```yaml
volumes:
  - /mnt/photos:/photos:ro
```

**Disk space:** The LXC needs enough storage for the Docker image (~4.5GB for CPU) plus the `/config` volume (database, thumbnails). For a small library, 15-20GB is sufficient. Larger libraries need more space for thumbnails.

## Performance Tips

### Put the Database on an SSD

The `/config` volume contains Photonarium's SQLite database and thumbnail cache. For best performance, especially with large libraries (50,000+ images):

- **Store `/config` on local SSD storage**, not network storage (NFS/SMB)
- SQLite requires a local filesystem with proper locking - network storage causes corruption
- Thumbnails also benefit from fast random-read performance

Your photos (`/photos`) can remain on slower network or HDD storage since they're read sequentially during scanning.

### Memory Considerations

- The CPU image uses ~2-3 GB RAM during normal operation (the ML models account for most of this)
- CUDA images may use more during batch processing
- Face detection and image captioning temporarily spike memory usage
- For systems with limited RAM (e.g. NAS devices, small VMs), reduce `embedding_batch_size`, `face_detection_batch_size`, and `nima_batch_size` in settings (default: 16-32). Smaller batches use less memory at the cost of slower processing.
- The thumbnail RAM cache is configurable via `thumbnail_cache_size_mb` (default: 100MB). Reduce this on memory-constrained systems.

### Network Storage for Photos

Accessing photos over NFS or SMB is fine for the `/photos` mount:

- Initial indexing may be slower due to network latency
- Thumbnail generation reads each file once, then serves from the local cache
- Subsequent browsing is fast because thumbnails are stored locally in `/config`

## Scheduled Rescans

For NAS setups where photos are synced continuously (e.g., via cloud services), enable automatic periodic rescans by editing `/config/photonarium.yml`:

```yaml
# Rescan all folders every 60 minutes
scan_interval_minutes: 60
```

This runs in the background without blocking the UI. Combined with the `--scan` startup flag, this ensures new photos are indexed automatically whether they arrive while the container is running or while it was stopped.

## Updating

To update to a new version:

```bash
# Pull the latest image
docker pull 7thsw/photonarium:latest

# Restart the container
docker compose down
docker compose up -d
```

Your data in `/config` and `/catalogue` is preserved across updates. ML models are baked into the image, so updates include the latest models automatically.

## Building from Source

If you want to build the image yourself (developers, custom modifications):

```bash
# Clone the repository
git clone https://github.com/7thsw/photonarium.git
cd photonarium

# Download ML models (run once, requires ~2.5GB disk space)
# This pre-downloads models so they can be baked into the image
make download-models

# Build CPU image (x86_64)
make build

# Build CUDA 12.6 image (x86_64, RTX 30xx/40xx)
make build-cu126

# Build ARM64 image (Raspberry Pi, Apple Silicon)
make build-arm64

# Build all variants
make all-images
```

The `make download-models` step downloads all ML models (OpenCLIP, BLIP, FaceNet, LAION, NIMA) to `docker/models/` so they can be copied into the Docker image during build. This only needs to be run once - subsequent builds reuse the cached models. The build will fail with an error if models haven't been downloaded.

See the Makefile for all available build targets. Note that building ARM64 images on x86_64 uses QEMU emulation and is slow.

---

# Direct Installation

If you prefer to run Photonarium directly on your system without Docker, follow these instructions.

## Requirements

- Python 3.10 or later (with tkinter - see note below)
- A GPU is recommended for faster processing (NVIDIA with CUDA, or Apple Silicon with MPS), but not required

### Tested configurations

The installer auto-detects your CUDA version and installs the matching PyTorch build. These combinations have been verified to install and run correctly:

| Python | PyTorch | CUDA | GPU acceleration |
|--------|---------|------|------------------|
| 3.10 | cu118 | 11.x | Yes |
| 3.10 | cu124 | 12.x | Yes |
| 3.10 | cpu | - | No |
| 3.11 | cu118 | 11.x | Yes |
| 3.11 | cu124 | 12.x | Yes |
| 3.11 | cpu | - | No |
| 3.13 | cu124 | 12.x | Yes |
| 3.13 | cpu | - | No |

macOS uses the default PyPI torch build (MPS acceleration on Apple Silicon).

**tkinter note:** Photonarium uses tkinter for the native folder picker dialog. On Windows, make sure "tcl/tk and IDLE" is checked during Python installation (it is by default, but some minimal installs omit it). On Linux, install the `python3-tk` package (e.g. `sudo apt install python3-tk`). On macOS with Homebrew, `brew install python-tk@3.12`. The installer scripts will warn you if tkinter is missing.

## Quick install (recommended)

The installer scripts create a virtual environment, install all dependencies in the correct order, initialise the configuration, and download the ML models. They will ask where to store your data (database, thumbnails, config) and confirm before making changes.

**Windows:**

Open the Photonarium folder in File Explorer and double-click `install.bat`. If Windows SmartScreen shows a "Windows protected your PC" warning, click **More info** then **Run anyway** - the script only installs Python packages and downloads ML models.

Alternatively, open Command Prompt, navigate to the Photonarium folder, and run:

```
install.bat
```

**Linux / macOS:**

Open a terminal, navigate to the Photonarium folder, and run:

```bash
chmod +x install.sh
./install.sh
```

The `chmod` command only needs to be run once (it marks the script as executable).

## Manual installation detail

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
   # Replace cu124 with cu118 for CUDA 11.x, or cpu for no GPU:
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
   # macOS (use default PyPI -- the CUDA indexes have no macOS wheels):
   # pip install torch torchvision torchaudio

   # Other dependencies
   pip install open_clip_torch
   pip install pillow numpy pyyaml opencv-python imagehash flask waitress requests orjson transformers rawpy exifread

   # Install facenet-pytorch last with --no-deps to avoid its overly strict
   # version bounds on torch/numpy/pillow (the package is unmaintained)
   pip install --no-deps facenet-pytorch
   ```

4. **Initialise the configuration**

  This step is optional.

  Photonarium has various aspects of its behaviour which may be tuned. To do this, you might want to create the default configuration file first. This contains all of the standard settings along with comments to explain how they work.

  ```bash
  # Create the config file at the OS default location and exit
  python app/app.py --init-config .
  ```

  This will create a `photonarium.yml` configuration file at the OS-appropriate location (see [Configuration](#configuration) below). You can change settings later via the in-app **Edit Settings** button on the Database screen, or by editing the YAML file directly in a text editor.

5. **Download ML models**

   ```bash
   python download_models.py
   ```

   If you use a custom data directory, pass it here too so the aesthetic scoring model is stored in the right place:

   ```bash
   python download_models.py --data-dir /path/to/data
   ```

   This downloads the AI models required for image search and captioning. Models are cached locally and only need to be downloaded once (or when you change model settings).

## Running Photonarium

  ```bash
  python app/app.py
  ```

  Then open `http://localhost:5000`

  The app runs entirely offline after models are downloaded.

  If you haven't looked already, take a look at the [Photonarium site](http://photonarium.org/tutorial/), and take a look at the tutorial.

  By default, the server listens on all network interfaces (`0.0.0.0`), so other devices on your local network can reach it. To restrict access to this machine only, set `server_host: 127.0.0.1` in `photonarium.yml`.

  **Important:** Photonarium is designed for use on a trusted home network. It has not been hardened for exposure to the public internet or untrusted networks. Do not make it accessible outside your local network - doing so may introduce security risks that are outside the scope of this project.

### Command line options

```bash
python app/app.py --port 8080              # Use a different port
python app/app.py --data-dir /path/to/data # Override data directory for this session
python app/app.py --config /path/to/yml    # Use a specific config file
python app/app.py --init-config /data/dir  # Create config with data_dir set, then exit
python app/app.py --generate-thumbnails    # Pre-generate thumbnails for all images
python app/app.py --scan                   # Run folder scan on startup
python app/app.py --detect-faces           # Run face detection on startup
python app/app.py --group-faces            # Run unknown face grouping on startup
python app/app.py --scan --detect-faces    # Combine flags as needed
python app/app.py --extract-exif           # Extract EXIF metadata for all images and exit
python app/app.py --list-models            # Output required models as JSON (for scripting)
```

By default, no processing runs at startup. Add flags to opt in to the phases you want.

After running the installer (or `--init-config`), `python app.py` reads the data directory from the config file - no `--data-dir` needed.

### Changing ML models

If you change model settings in `photonarium.yml`, run the model downloader again:

```bash
python download_models.py
```

Available caption models (from smallest to largest):

* `Salesforce/blip-image-captioning-base` (~1GB, fastest)
* `Salesforce/blip-image-captioning-large` (~2GB, default)
* `Salesforce/blip2-opt-2.7b` (~5GB, better quality)
* `Salesforce/blip2-flan-t5-xl` (~8GB, most descriptive)

## Configuration

Settings can be changed via the **Edit Settings** button on the Database screen, which opens an in-app editor that works from any device on your network. Settings are stored in `photonarium.yml` at the OS-appropriate location:

- **Windows:** `%LOCALAPPDATA%\Photonarium\photonarium.yml`
- **macOS:** `~/Library/Application Support/Photonarium/photonarium.yml`
- **Linux:** `~/.config/photonarium/photonarium.yml` (or `$XDG_CONFIG_HOME`)

The config file is created automatically on first run (or by the installer). Use `--config /path/to/file.yml` to override the location.

Key settings:

* `data_dir`: where Photonarium stores its database, thumbnails, and models (set by installer, overridable with `--data-dir`)
* `thumbnail_quality`: JPEG quality for thumbnails (1 to 100)
* `thumbnail_cache_size_mb`: RAM cache size for thumbnails
* `indexing_threads`: parallel threads for scanning
* `face_detection_enabled`: enable automatic face detection
* `face_detection_min_confidence`: detection confidence threshold
* `face_recognition_threshold`: default similarity threshold for auto-recognition (can be overridden per person in pick preferred mode)
* `caption_model`: BLIP model for image captioning (run `python download_models.py` after changing)
* `caption_max_length`: maximum caption length in tokens
* `caption_min_length`: minimum caption length (higher = more descriptive)
* `catalogue_dir`: path to the managed catalogue directory for imports (default: `<data-dir>/catalogue/`)
* `import_threads`: parallel threads for file copying during import (1-16, default 4)
* `trash_dir`: custom path for the trash directory (default: `<data-dir>/trash/`)

## Trash directory

When you delete images (from the Gallery, full-screen viewer, or the Groups refine dialog), the files are moved to a trash directory instead of being permanently deleted. By default, this is `<data-dir>/trash/`.

* Files keep their original names; collisions get a counter suffix (`beach.jpg`, `beach (2).jpg`, etc.).
* The trash directory must not overlap any indexed folder. If it does, Photonarium disables trash operations and shows a warning.
* To recover a trashed image, move the file back into an indexed folder and rescan.
* To customise the location, set `trash_dir` in `photonarium.yml`.

## Tips

* Large imports and database rescans take time. Let it run and come back later.
* Face recognition improves as you tag more clear examples of the same person.
* Add multiple people before refining any one person, this tends to reduce false matches.
* Once you have several people established, use Quick Match (sparkle button) to rapidly identify unknown faces. It shows you the most likely matches based on face similarity.
* If two people get mixed up, increase that person's recognition threshold.
* Emoji ratings work well for quick favourites, and make filtering pleasant.
* Use negative terms in search (`beach -people`) to exclude concepts from results.

## Acknowledgements

Photonarium is built on the shoulders of some remarkable open-source AI/ML work:

- [OpenCLIP](https://github.com/mlfoundations/open_clip) (LAION) - the semantic image embeddings that power search and similarity
- [BLIP / BLIP-2](https://github.com/salesforce/LAVIS) (Salesforce Research) - automatic image captioning
- [facenet-pytorch](https://github.com/timesler/facenet-pytorch) - MTCNN face detection and InceptionResnetV1 face recognition
- [LAION Aesthetic Predictor](https://github.com/LAION-AI/aesthetic-predictor) (LAION) - lightweight aesthetic quality scoring
- [NIMA](https://github.com/truskovskiyk/nima.pytorch) - neural image quality assessment trained on human aesthetic judgements
- [PyTorch](https://pytorch.org/) (Meta) - the foundation all of the above is built on

Thanks to the broader Python community - Flask, Pillow, NumPy, OpenCV, and countless other libraries - for making a project like this feasible for a small team.

The tutorial example images come from [Lorem Picsum](https://picsum.photos), which serves freely usable photos from [Unsplash](https://unsplash.com).

Finally, thanks to [Anthropic](https://www.anthropic.com) and [Claude Code](https://claude.ai/code) for doing a lot of the grunt work.

## Support 7th software

We hope you enjoy Photonarium and find it valuable. If you'd like to show your support, please use one of the links below:

![Payment links](www/images/support.png)

- [USD ($) contribution](https://buy.stripe.com/fZu3cv4WOdN0b0N8Jaebu01)
- [GBP contribution](https://buy.stripe.com/14A14nexodN00m94sUebu00)
- [EUR contribution](https://buy.stripe.com/dRmbJ1blc4cq3ylbVmebu02)

## Licence

Copyright (c) 2026 7th software Ltd. - Licensed under Apache 2.0
