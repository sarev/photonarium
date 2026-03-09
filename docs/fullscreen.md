# Full-screen Viewer

[< Back to README](../README.md)

The full-screen viewer is for focused viewing and quick decisions. It gives you fast navigation, zooming, and (optionally) face tagging without breaking your flow. It handles both images and videos.

## Controls

- **Escape** closes full-screen.
- **Left / Right arrows** go to previous or next item.
- **Home / End** go to first or last item in the current order.
- **Mouse wheel** zooms in and out (images only).
- **Double click** toggles zoom level (images only).
- When zoomed in, **click and drag** to pan (images only).

Touch gestures for zoom and pan may vary by device and browser.

## Keyboard shortcuts

These shortcuts use Ctrl on Windows/Linux and Cmd on macOS:

- **Ctrl/Cmd + F** toggles face tagging mode on or off (images only).
- **Ctrl/Cmd + I** ignores all unknown faces in the current image (marks them as `-`).
- **Ctrl/Cmd + R** rotates the image right (90 deg clockwise). Not available for videos.
- **Ctrl/Cmd + L** rotates the image left (90 deg). Not available for videos.
- **Ctrl/Cmd + Backspace** or **Ctrl/Cmd + Delete** moves the current item to trash and advances to the next one.

## Video playback

When a video is opened in full-screen, it displays with standard playback controls (play/pause, seek, volume). Videos start paused so you can decide when to begin watching. The face tagging and rotate buttons are visually disabled for videos, since those features apply only to images.

When opening a video from the [Videos screen](videos.md), the player seeks to the relevant position - for example, to the start of a matching scene when you've searched for video content.

If a video has been transcribed, **subtitles** are loaded automatically as a WebVTT caption track. Use the browser's built-in subtitle controls to show or hide them.

---

# Slideshow

The slideshow lets you sit back and watch your photos and videos play through automatically, with smooth cross-fade transitions between items.

## Starting a slideshow

- In the full-screen viewer, click the **play** button in the toolbar for linear playback (in the current sort order), or the **shuffle** button for random order.
- On the Groups screen, hover over any group stack to reveal play and shuffle badges - click one to jump straight into a full-screen slideshow scoped to that group's items.
- Press **Space** to start a linear slideshow from anywhere in the full-screen viewer.

## While a slideshow is running

- **Space** pauses or resumes.
- **Escape** stops the slideshow and exits full-screen.
- **Left / Right arrows** manually skip to the previous or next item. The slideshow continues from the new position.
- Moving the mouse or pressing any key resets the hold timer, so the current item stays on screen a little longer while you interact.

Clicking the other mode button (play or shuffle) while a slideshow is running switches to that mode without stopping.

## Videos in slideshows

When a slideshow reaches a video, it begins playing automatically. The slideshow advances to the next item once the video finishes (or after the hold duration, whichever comes first for short clips). During video playback, the standard slideshow controls still work - you can skip forward, pause, or stop at any time.

## Timing

The hold duration (how long each image stays on screen) defaults to 5 seconds and can be changed in `photonarium.yml` with the `slideshow_interval` setting (in seconds).

---

# Face tagging (in full-screen)

Face tagging helps you name people, ignore false positives, and correct mistakes directly on the photo. It is available for images only - face detection does not apply to video frames.

Turn it on and off using the face icon in the full-screen viewer.

## Bounding box colours

- **Green**: this face is named
- **Grey**: this face is ignored (named `-`)
- **Red**: this face is unknown (not named yet)
- **Orange**: you are currently renaming this face (the name field has focus)

## Hover controls on a face box

When you hover a face box, you may see:

- **Grey circle with `-`**: mark this face as ignored
- **Green circle with `x`**: remove the name, returning it to the unknown faces list
- **Red circle with `x`**: remove the bounding box (it is not a face)
- **Sparkle button**: open Quick Match to see likely people matches (for unknown faces)

## Naming a face

- Click a face's label and type a name.
- As you type, you'll see suggestions.
- **Up / Down arrows** move through suggestions.
- **Enter** confirms.
- **Escape** cancels your edit (restores the previous value).
- **Tab / Shift+Tab** cycles through unknown face inputs so you can name several quickly.

As more photos are tagged, Photonarium can recognise that person in other images.
