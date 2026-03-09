# Database

[< Back to README](../README.md)

Database is where you tell Photonarium where your photos and videos live, import new files, and see what the app is currently doing.

## What you can do

- Add folders (scanned recursively for images and videos)
- Import files into a managed catalogue directory (see [Import](#import) below)
- Rescan folders to pick up changes
- Watch progress for each pipeline stage (indexing, thumbnails, embeddings, scoring, face detection, video scenes, and imports)
- Click **Edit Settings** to open the in-app settings editor (works from any device on your network). The settings dialog also has a button to re-launch the **Setup Assistant** if you want to change your hardware profile or search language
- Click **View Logs** to see recent server log output in a colour-coded, filterable dialog (useful for diagnosing issues without needing terminal access)
- Click **Restart** to restart the backend server from the UI (useful for headless or Docker deployments where you don't have terminal access, e.g. after changing settings)

## Processing pipeline

When you add a folder or rescan, Photonarium processes files through a sequential pipeline. Each stage runs to completion before the next begins, and progress is shown on the Database screen:

1. **Indexing** - discovers new files, reads metadata, writes placeholder thumbnails so media appears in the Gallery immediately
2. **Thumbnails** - replaces placeholders with real thumbnails for images; detects scenes and generates scene thumbnails for videos. Shows per-video step detail (e.g. "Detecting scenes (1/4)")
3. **Embeddings** - generates AI embeddings for semantic search (images and video scenes)
4. **Scoring** - rates image aesthetic quality (NIMA and LAION scores)
5. **Face detection** - finds faces in images (not videos)
6. **Grouping** - computes duplicate groups, directory groups, and face reassessment
7. **Transcription** - speech-to-text for video audio (enabled by default with automatic language detection)

The pipeline is self-healing: if the app is interrupted mid-processing, restarting picks up where it left off automatically. Large libraries take time, especially on first scan. You can keep browsing while processing runs in the background.

## Import

The Database screen has an Import section that lets you copy images and videos into a Photonarium-managed directory, organised by date (`YYYY/YYYY-MM-DD/filename`). The originals are not modified. By default, the catalogue lives at `<data-dir>/catalogue/` - set `catalogue_dir` in settings to use a different location.

**On desktop:**
- Drag and drop files or folders onto the import drop zone, or use the Pick Folder / Pick Photos buttons.
- When you drop a folder, a choice dialog lets you either reference the folder in place (Add Folder) or copy its contents into the catalogue (Import).
- File drops always import.

**On mobile:**
- Tap Pick Photos to open the system photo picker. On Android, a Pick Folder button is also available.
- Before uploading, the browser sends file names and sizes to the backend for a fast duplicate check. Only new files are uploaded, saving bandwidth.

Imported files are processed by the normal pipeline (indexing, thumbnails, embeddings, scoring, face detection) automatically.

### Supported formats

**Images:** `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`, and camera RAW formats (`.cr2`, `.cr3`, `.nef`, `.nrw`, `.arw`, `.srf`, `.dng`, `.raf`, `.rw2`, `.orf`, `.pef`, `.srw`, `.x3f`, `.3fr`, `.iiq`, `.rwl`, `.kdc`, `.dcr`, `.erf`). RAW support requires the `rawpy` package. Note that RAW files cannot be rotated within Photonarium. RAW files are also slower to process than standard formats - each file requires full demosaicing of the sensor data, so indexing and opening full-screen images will take a little longer than with JPEGs.

**Videos:** `.mp4`, `.mkv`, `.avi`, `.mov`, `.webm`, `.m4v`, `.wmv`, `.flv`. Videos are processed through scene detection, keyframe extraction, and (where audio is present) speech transcription.

## How Photonarium dates your media

Every photo and video in Photonarium has a timestamp that determines where it appears when you sort by date. Getting this right matters - a wrong date puts a file in the wrong month or year, which makes it hard to find later. Photonarium uses several sources to figure out when a photo or video was actually captured, trying them in order of reliability:

1. **Camera metadata (EXIF)** - Most cameras and phones embed the exact date and time in the file. When this is available it's almost always correct, so Photonarium uses it first.
2. **Filename and folder path** - Many apps and devices encode dates into filenames or folder names. Photonarium can read a wide range of these patterns (see examples below).
3. **File system dates** - As a last resort, the file's creation or modification date is used. These are the least reliable since copying files often resets them.

### Filename patterns Photonarium understands

Photonarium's filename parser handles far more than just `IMG_20240315.jpg`. It reads dates from both the filename and the folder structure, and combines hints from multiple levels. Some real-world examples:

| Path | Result | How |
|------|--------|-----|
| `IMG_20240315_143022.jpg` | 15 Mar 2024, 14:30:22 | Standard camera naming |
| `WhatsApp Image 2026-01-06 at 12.33.29.jpeg` | 6 Jan 2026, 12:33:29 | WhatsApp timestamp with dot-separated time |
| `photos/2006/Summer/June-02/img.jpg` | 2 Jun 2006 | Year from folder, season narrows it, month name and day pin it down |
| `photos/Feb'03/scan001.jpg` | 1 Feb 2003 | Apostrophe-style year with month abbreviation |
| `photos/early may/IMG_0001.JPG` | 1 May (current year) | "early" sets the day to the 1st |
| `Xmas 2019/DSC0042.jpg` | 25 Dec 2019 | "Xmas" gives December 25th, year from the same folder |
| `Holiday 2019/batch_03/DSC0042.jpg` | 1 Jan 2019 | Year from a parent folder; month and day default to January 1st when only a year is found |

### Ambiguous dates (day/month order)

A date like `07-03-2024` is ambiguous - it could be 7 March or 3 July, depending on where you live. Photonarium uses the **Date Order** setting (in Settings, under Image Processing) to resolve this. The default is DMY (day-month-year), which interprets `07-03-2024` as 7 March. Change it to MDY if you're in a region that writes the month first. This is a preference, not a hard rule: if the preferred interpretation produces an impossible date (like month 13), Photonarium automatically uses the valid alternative.

### WhatsApp and similar apps

Some apps rewrite the camera metadata to the time you *received* the photo rather than when it was *taken*. WhatsApp is the most common example - the filename `WhatsApp Image 2024-03-15 at 10.30.45.jpeg` contains the real capture time, but the EXIF data says when you downloaded it. Photonarium handles this automatically: filenames matching `WhatsApp Image *` and `WhatsApp Video *` use the filename date instead of EXIF. You can add more patterns in Settings under **Filename Date Overrides** if other apps on your phone behave the same way.
