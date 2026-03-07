# Photonarium

![Photonarium Logo](www/images/logo.png)

[Photonarium](http://photonarium.org/) is a photo and video catalogue that stays on your computer. It's for people who want the convenience of modern search and face grouping, without uploading their life to someone else's servers.

## Contents

- [Why Photonarium exists](#why-photonarium-exists)
- [What it can do](#what-it-can-do)
- [A quick start](#a-quick-start-how-most-people-use-it)
- [What to expect](#what-to-expect)
- [Getting around](#getting-around)

**Screen guides:**
- [Gallery](docs/gallery.md) -- browsing, sorting, info panel
- [Full-screen viewer, Slideshow & Face tagging](docs/fullscreen.md) -- viewing, playback, tagging
- [Search](docs/search.md) -- text search, filters, people, metadata
- [Videos](docs/videos.md) -- video grid, scene timeline, heatmap
- [Groups & Smart Groups](docs/groups.md) -- duplicates, directories, albums, saved searches
- [Faces](docs/faces.md) -- face detection, people, recognition, Quick Match
- [Database](docs/database.md) -- folders, import, processing, settings

**Installation & configuration:**
- [Installation](docs/installation.md) -- Docker, direct install, running, configuration

**Other:**
- [Tips](#tips)
- [Acknowledgements](#acknowledgements)

## Why Photonarium exists

Most photo apps push you towards the cloud. That is great until you care about privacy, subscriptions, slow uploads, or working offline.

Photonarium keeps your library local and helps you do the three things people actually want:

- **Find** photos and videos quickly, even when you cannot remember filenames, and exclude what you don't want
- **Tidy** a messy collection, especially duplicates and near-duplicates
- **Organise** around people, favourites, and your own notes

Find out more about the motivations behind Photonarium in [`BACKGROUND.md`](docs/background.md).

## What it can do

- **100% offline and private** -- runs entirely on your machine. No cloud, no accounts, no tracking. Your files never leave your computer.
- **Photos and videos** -- manages images and video files side by side. Videos are broken into scenes with keyframe thumbnails, and can be searched at the scene level.
- **Multi-device sync** -- use Photonarium from multiple devices at once. Changes made on one (naming faces, rating photos) appear on all others within seconds.
- **Mobile friendly** -- browse your library from any device on your network. The responsive layout adapts to phones and tablets in both portrait and landscape.
- **AI-powered search** that understands what you type (e.g. "sunset over mountains", "birthday cake"), with negative terms to exclude concepts (e.g. "beach -people"). Search images, videos, or both -- video search finds matching scenes within clips.
- **Face recognition** -- automatic face detection and recognition. Name faces and Photonarium finds them across your library.
- **Quality scoring** -- AI aesthetic scoring ranks your images by visual quality. Find your best shots instantly.
- **Slideshow** mode in the full-screen viewer with smooth cross-fade transitions, linear or shuffled playback, configurable timing, and video autoplay
- **Smart Groups** -- saved searches that stay up to date automatically. Set your filter criteria once and matching items appear whenever you open the group, even files added later.
- **Duplicate detection** at four levels of similarity (identical, near-identical, similar, related) plus auto-generated **directory groups** and user-curated **custom groups** (albums), with **Refine Groups** to filter by quality and view the best (or worst) images in the Gallery, or prune duplicates to trash
- **Camera RAW support** -- native support for 20+ camera RAW formats alongside JPEG, PNG, and other standard image types. No conversion needed.
- **Video formats** -- `.mp4`, `.mkv`, `.avi`, `.mov`, `.webm`, `.m4v`, `.wmv`, `.flv` with automatic scene detection, keyframe extraction, and speech transcription
- **Camera data** -- full EXIF metadata extraction. Search and filter by camera, lens, ISO, aperture, shutter speed, and more.
- **Import into catalogue** -- copy images and videos from SD cards, phone uploads, or downloads into a managed catalogue directory, organised by date. Preflight dedup avoids importing files you already have.
- **Ratings and descriptions** so you can build your own favourites system

Once you have run the model downloader, the models stay on your machine. Everything runs locally.

## A quick start (how most people use it)

1. Start the Photonarium app in a terminal window.
2. Open the Photonarium web page in your browser, the default is http://localhost:5000
3. Go to **Database** and **add one or more folders** that contain photos and videos.
4. Let it scan. Big libraries take time, especially face detection and video processing.
5. Go to **Gallery** and start browsing.
6. Use **Search** when you want to find specific images or video scenes.
7. Use **Videos** to browse your video library and explore scene timelines.
8. Use **Groups** when you want to clean up duplicates or organise items into albums.
9. Use **Faces** when you want to name people and improve recognition.

## What to expect

Photonarium is currently in **beta**. It works, and people are using it day-to-day, but installation is still a manual process, the interface is evolving, and you may encounter rough edges. If something breaks or feels wrong, please [open an issue](https://github.com/sarev/photonarium/issues) -- feedback during this stage is especially valuable.

### Intended setup

Photonarium is a **desktop application** that runs in your browser, not a mobile app. The backend (Python) and the frontend (the browser tab) are designed to run on the same machine -- typically your laptop, desktop PC, or a home server where your photos and videos are stored.

It also works over a local network: you can run the backend on one machine (say, a NAS or always-on PC) and open the UI in a browser on another. Most features work fine in this setup, with a few caveats:

- **Performance** -- it may be a little slower, for two reasons: 1) the backend machine needs some grunt, ideally a decent GPU, especially during image and video ingestion, and 2) quite a bit of data between the backend and UI needs to go over your local network.
- **Reveal in folder** opens a file-manager window on the machine running the *backend*, which is only useful if that's also the machine you're sitting at. On a headless server this will either silently fail or pop a window nobody sees.
- **The folder picker** (for adding directories) likewise opens on the backend machine. If you're accessing Photonarium remotely, use the CLI instead: `python app/app.py --add-folder /path/to/photos`.

### Mobile devices

The UI adjusts for smaller screens and touch input -- the toolbar collapses to a hamburger menu, touch-friendly scroll zones appear, and layout stacks vertically. It's usable for browsing and basic tasks on a phone or tablet, but the full experience (drag-box selection, keyboard shortcuts, side-by-side info panel) is designed for a desktop browser. Think of mobile as a handy way to flick through your library on the sofa, not a replacement for the desktop workflow.

### Multi-user and security

Photonarium has **no user accounts, no login, and no access control**. Anyone who can reach the server's address can view and modify your library. This is fine for personal use and trusted home networks, but you should not expose it to the public internet.

Multiple browser tabs or devices on the same network can use Photonarium at the same time. Changes made on one client, naming a face, rating an image, creating a group, trashing a photo, are automatically pushed to every other open client within a couple of seconds. If a client falls too far behind (e.g. a laptop lid was closed for a while), it detects the gap and silently reloads to catch up. If your browser loses the connection with the Photonarium backend for any reason, your changes are blocked with a warning message until the connection is restored.

## Getting around

Use the toolbar buttons, or these shortcuts (ignored while you are typing in a text box):

- **Ctrl/Cmd + G**: Gallery
- **Ctrl/Cmd + M**: Manage Database
- **Ctrl/Cmd + D**: Groups (Duplicates and Albums)
- **Ctrl/Cmd + S**: Search and Filter
- **Ctrl/Cmd + F**: Faces and People

Common keys across screens:

- **Escape**: go back / close a panel (for example: exit Search, Duplicates, or Database back to Gallery; close dialogs; close full-screen).
- **Enter**: open the selected item (where it makes sense, like opening an image or video).
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

## Tips

* Large imports and database rescans take time. Let it run and come back later.
* Face recognition improves as you tag more clear examples of the same person.
* Add multiple people before refining any one person, this tends to reduce false matches.
* Once you have several people established, use Quick Match (sparkle button) to rapidly identify unknown faces. It shows you the most likely matches based on face similarity.
* If two people get mixed up, increase that person's recognition threshold.
* Emoji ratings work well for quick favourites, and make filtering pleasant.
* Use negative terms in search (`beach -people`) to exclude concepts from results.
* When searching for video content, use "Videos" mode to see scene-level results with a heatmap showing exactly where your query matches within each clip.

## Acknowledgements

Photonarium is built on the shoulders of some remarkable open-source AI/ML work:

- [OpenCLIP](https://github.com/mlfoundations/open_clip) (LAION) -- the semantic image embeddings that power search and similarity
- [BLIP / BLIP-2](https://github.com/salesforce/LAVIS) (Salesforce Research) -- automatic image captioning
- [facenet-pytorch](https://github.com/timesler/facenet-pytorch) -- MTCNN face detection and InceptionResnetV1 face recognition
- [LAION Aesthetic Predictor](https://github.com/LAION-AI/aesthetic-predictor) (LAION) -- lightweight aesthetic quality scoring
- [NIMA](https://github.com/truskovskiyk/nima.pytorch) -- neural image quality assessment trained on human aesthetic judgements
- [PyTorch](https://pytorch.org/) (Meta) -- the foundation all of the above is built on

Thanks to the broader Python community -- Flask, Pillow, NumPy, OpenCV, and countless other libraries -- for making a project like this feasible for a small team.

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
