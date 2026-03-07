# Gallery

[< Back to README](../README.md)

The Gallery is where you spend most of your time: browsing your library quickly, picking the best shots, and adding a little information (ratings and notes) so you can find things again later.

## What you can do

- Browse a smooth thumbnail grid, even for large libraries of images and videos.
- Sort the Gallery to find the media you care about.
- Open images and videos full-screen.
- Delete, rotate (images only), and reveal files on disk.
- Edit descriptions and ratings in the info panel.

## Sorting

The toolbar has sort buttons for date, rating, content similarity, people, and quality. Date and rating are straightforward; the others are worth knowing about:

- **Sort by content similarity**: select one image, then click the similarity button. Photonarium compares every image in the library against the selected one using AI embeddings (OpenCLIP) and re-orders the Gallery by visual similarity. The most visually similar images appear first (or last, if you flip the sort direction). This is a powerful way to find related shots, variants of the same scene, or images with a similar mood or composition -- even when they don't share any text, tags, or metadata.
- **Sort by quality**: ranks images by a weighted combination of aesthetic quality (AI-scored), sharpness, and resolution. Especially useful when viewing a duplicate group or custom group, where you want the best version at the top.
- **Sort by people**: groups images based on who appears in them (after face detection has run).

## Opening full-screen

Open full-screen with:
- **Double click** a thumbnail, or
- Select one thumbnail and press **Enter**, or
- Use the toolbar button (with one thumbnail selected).

Videos play inline in the full-screen viewer. See the [Full-screen viewer](fullscreen.md) guide for details.

## Quick actions on selected items

- **Delete / Backspace** moves selected items to the trash directory (with a confirmation).
- **Rotate left / rotate right** fixes images that are sideways (not available for videos).
- **Reveal in folder** opens your file manager at the file location (only available when exactly one item is selected).

## Info panel

The info panel sits to the right of the thumbnail grid. Click the toggle at its edge to collapse or expand it. On narrow screens or when the panel would take more than 20% of the viewport, it collapses automatically. Your preference is remembered across sessions.

When you select an image or video, the info panel shows basic details and lets you edit:

- **Description** (free text)
  - Press **Enter** to save (Shift+Enter adds a new line).
  - Optionally generate an automatic caption using the sparkle button, then edit it if needed. (Captioning is available for images only.)
- **Rating** (emoji works well for favourites)
  - Use the emoji button to insert emoji quickly.

Descriptions and ratings help when you search later.

- **Histogram** -- for images, a colour histogram is displayed in the info panel showing the distribution of red, green, and blue channels. This gives a quick visual summary of exposure and colour balance.
- **Metadata** opens a dialog showing EXIF data extracted from the file (camera, lens, focal length, aperture, shutter speed, ISO, and so on). If any field looks interesting, click the filter icon next to it to select it. You can select several fields, then click **Done** to jump straight to Search with those values pre-filled as metadata filters. (EXIF metadata is primarily available for images; video files may have limited metadata.)

## Videos in the Gallery

Videos appear alongside images in the Gallery. Each video's thumbnail shows its **preferred scene** -- the scene you've chosen to represent that video (or the first scene by default). A small duration badge appears on video thumbnails so you can tell them apart from images at a glance.

To manage videos in more detail -- browse all videos, see their scene timelines, or search specifically for video content -- use the dedicated [Videos](videos.md) screen.

## Reviewing groups in the Gallery

If you opened a group into the Gallery, you can move between groups without going back:

- **Alt + Left / Alt + Right** navigates to the previous/next group.
- The toolbar also shows previous/next group buttons for the same action.
