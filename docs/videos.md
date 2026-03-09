# Videos

[< Back to README](../README.md)

The Videos screen is the dedicated home for browsing and managing video content. It's always accessible from the navigation bar, whether you're searching or just browsing your library.

## Overview

The screen is divided into two panels:

- **Top panel** - a thumbnail grid of all your videos (or search results when a filter is active)
- **Bottom panel** - a scene timeline for the currently selected video

## Video grid

The grid works like the Gallery's thumbnail grid, with all the same selection controls (click, Ctrl+click, Shift+click, drag-select). Each video card shows:

- A thumbnail of the video's preferred scene
- A duration badge
- A score badge (only when a search filter is active, showing how well the video matches your query)

**Single click** a video to select it and populate the timeline below. **Double click** to open it in the [full-screen viewer](fullscreen.md) - if a search is active, it seeks to the start of the best-matching scene; otherwise it starts from the beginning.

### Sorting

When no search filter is active, videos can be sorted by date, rating, or content similarity. **Sort by similarity** works just like in the Gallery: select a video, then click the similarity button to re-order all videos by visual similarity to the selected one.

When a search is active, videos are sorted by match score so the most relevant videos appear first.

The thumbnail size can be adjusted independently from the Gallery using the +/- buttons in the Videos toolbar.

## Scene timeline

When you select a single video, the bottom panel shows its scenes as a horizontal strip of keyframe thumbnails. If no video or multiple videos are selected, the timeline smoothly collapses out of view. Each scene is **proportionally sized by duration** - a 30-second scene takes up more space than a 5-second scene - giving you an intuitive sense of the video's structure at a glance.

### What you see on each scene

- **Keyframe thumbnail** - a representative frame from the middle of the scene, slightly darkened and brightening on hover
- **Timecode** - the scene's start time (bottom-left corner)
- **Preferred star** - click to set this scene as the video's preferred scene (top-right corner). The preferred scene's star appears in gold; other scenes' stars appear grey. The preferred scene determines the thumbnail shown in the Gallery and Videos grid.

### Heatmap overlay (search mode)

When a search filter is active, each scene gets a colour overlay showing how well it matches your query:

- **Transparent** - no match (score 0)
- **Blue to yellow** - weak to moderate match (score 0 to 0.5)
- **Yellow to red** - moderate to strong match (score 0.5 to 1.0)

Scores are normalised across all videos and scenes in the search results, so the colours are relative to the best match in your results. This makes it easy to spot exactly which moments in a video are relevant to what you searched for.

When no search is active, the timeline shows scenes with thumbnails and timecodes but no heatmap overlay.

### Scrolling and the minimap

If a video has many scenes, the timeline overflows horizontally. You can navigate it in several ways:

- **Drag-to-scroll** - click and drag anywhere on the timeline to scroll through scenes, like a touch surface
- **Minimap** - a thin bar appears below the timeline showing the full video duration. It includes time ticks and a draggable viewport indicator. Click anywhere on the minimap to jump to that position, or drag the viewport block to scroll proportionally.
- **Gradient indicators** appear at the edges to show there are more scenes to see

When a search is active, the minimap also displays a smoothed heatmap gradient reflecting the per-scene match scores across the full duration.

The minimap hides automatically when the timeline fits without scrolling. Each scene has a minimum width to ensure the keyframe remains recognisable.

### Scene preview popup

**Hover** over a scene thumbnail (or **long-press** on touch devices) to see a preview popup with a larger keyframe image. If the scene has a transcription, the text is shown below the image — a quick way to scan what's being said without opening the video.

### Interacting with scenes

- **Single click** highlights a scene.
- **Double click** opens the video in the full-screen viewer, seeked to that scene's start time, and begins playback automatically.
- **Click the star** to make that scene the video's preferred scene (updates the thumbnail in the Gallery and Videos grid immediately).

## Transcriptions

When a video has audio, Photonarium automatically transcribes speech during processing (enabled by default with automatic language detection). The transcription text appears below the scene timeline, with each segment labelled by its timecode. Transcriptions are semantically searchable - searching for a phrase someone said in a video will find matching scenes.

### Subtitles in playback

Transcriptions are also displayed as **subtitles** when playing videos in the full-screen viewer. Standard browser subtitle controls apply.

### Per-video language

Right-click a video in the grid to set its transcription language. This is useful for multilingual libraries where automatic detection may not always pick the right language. After changing the language, you can retranscribe the video to get more accurate results.

## Browse mode vs search mode

**Browse mode** (no search active): the grid shows all videos in your library, sorted by your chosen order. The timeline shows scene thumbnails and timecodes, but no heatmap.

**Search mode** (after searching in Videos mode from the [Search screen](search.md)): the grid shows only matching videos, sorted by relevance. Score badges appear on each card, and the timeline heatmap highlights which scenes match your query.

To exit search mode, clear the filter from the Search screen or the toolbar. The Videos screen returns to showing all videos.

## Preferred scenes

Each video has a **preferred scene** that represents it throughout the app:

- The Gallery uses the preferred scene's keyframe as the video's thumbnail
- The Videos grid shows the preferred scene's keyframe
- When searching in "All" mode, the video is ranked by its preferred scene's similarity to the query

By default, the first scene is the preferred scene. To change it, select a video in the grid, then click the star on the scene you prefer in the timeline. The change takes effect immediately across all screens.
