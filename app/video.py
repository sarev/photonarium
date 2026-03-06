"""
Video processing utilities for Photonarium.

All video I/O is handled through this module using PyAV (``av``), which bundles
FFmpeg libraries in its pip wheels — no system FFmpeg installation is needed.

Key capabilities:
    - Video metadata extraction (duration, dimensions, codec, creation time)
    - Single-frame extraction at arbitrary timestamps
    - Keyframe thumbnail generation (same sharpening/JPEG pipeline as images)
    - Scene boundary detection via FFmpeg's ``select`` filter
    - Multi-frame extraction per scene for embedding
    - Audio segment extraction for speech-to-text

All functions degrade gracefully if PyAV is not installed — the module-level
``is_video_supported()`` check lets callers skip video processing cleanly.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageFilter

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PyAV availability check (cached at module level)
# ---------------------------------------------------------------------------

_av_available: bool | None = None


def is_video_supported() -> bool:
    """Check whether the ``av`` (PyAV) package is importable.

    The result is cached after the first call so that repeated checks
    during ingestion are essentially free.

    Returns:
        True if PyAV is available, False otherwise.
    """
    global _av_available
    if _av_available is None:
        try:
            import av  # noqa: F401

            _av_available = True
        except ImportError:
            _av_available = False
    return _av_available


# ---------------------------------------------------------------------------
# Video metadata
# ---------------------------------------------------------------------------


@dataclass
class VideoMetadata:
    """Container for video file metadata extracted without decoding frames."""

    duration: float  # seconds
    width: int
    height: int
    codec: str
    creation_time: datetime | None


def get_video_metadata(path: Path) -> VideoMetadata | None:
    """Extract metadata from a video file via ``av.open()``.

    Opens the container and reads stream info without decoding any frames,
    making this very fast even for large files.

    Args:
        path: Path to the video file.

    Returns:
        VideoMetadata on success, None if the file cannot be opened or has
        no video stream.
    """
    if not is_video_supported():
        return None

    import av

    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                logger.warning(f'No video stream found in: {path}')
                return None

            video_stream = container.streams.video[0]

            # Duration: prefer container duration, fall back to stream
            duration = float(container.duration) / av.time_base if container.duration else 0.0
            if duration <= 0 and video_stream.duration:
                duration = float(video_stream.duration * video_stream.time_base)

            # Dimensions
            width = video_stream.codec_context.width or 0
            height = video_stream.codec_context.height or 0

            # Codec name
            codec = video_stream.codec_context.name or 'unknown'

            # Creation time from container metadata
            creation_time = None
            metadata = container.metadata or {}
            ct_str = metadata.get('creation_time')
            if ct_str:
                try:
                    creation_time = datetime.fromisoformat(ct_str.replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    pass

            return VideoMetadata(
                duration=duration,
                width=width,
                height=height,
                codec=codec,
                creation_time=creation_time,
            )
    except Exception as e:
        logger.error(f'Failed to read video metadata for {path}: {e}')
        return None


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------


def extract_frame(path: Path, time_seconds: float) -> Image.Image | None:
    """Seek to a timestamp and decode a single video frame.

    Uses ``av.open()`` with seeking to decode one frame near the requested
    time.  The returned PIL Image is in RGB mode.

    Args:
        path: Path to the video file.
        time_seconds: Target position in seconds.

    Returns:
        PIL Image (RGB) on success, None on failure.
    """
    if not is_video_supported():
        return None

    import av

    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                return None

            stream = container.streams.video[0]
            stream.codec_context.thread_type = 'AUTO'

            # Seek to the nearest keyframe before the target time
            target_pts = int(time_seconds / stream.time_base)
            container.seek(target_pts, stream=stream)

            for frame in container.decode(stream):
                return frame.to_image().convert('RGB')

        return None
    except Exception as e:
        logger.debug(f'Failed to extract frame at {time_seconds:.1f}s from {path}: {e}')
        return None


def extract_keyframe_thumbnail(
    video_path: Path,
    dest_path: Path,
    size: int,
    quality: int = 85,
    time_offset: float = 0,
) -> bool:
    """Extract a frame from a video and save it as a JPEG thumbnail.

    Uses the same sharpening and JPEG pipeline as the image thumbnail
    generator in ``thumbnails.py``: resize with Lanczos, apply
    UnsharpMask, save as JPEG.

    Args:
        video_path: Path to the source video file.
        dest_path: Where to write the JPEG thumbnail.
        size: Target size (largest dimension).
        quality: JPEG quality (1-100).
        time_offset: Time in seconds to extract the frame from.
            Defaults to 0 (first frame).  For a representative poster,
            callers typically pass a small offset (e.g. 1-2s).

    Returns:
        True if the thumbnail was created, False on failure.
    """
    frame = extract_frame(video_path, time_offset)
    if frame is None:
        return False

    try:
        # Resize preserving aspect ratio (same as thumbnails.py)
        frame.thumbnail((size, size), Image.LANCZOS)

        # Apply sharpening (matches the image thumbnail pipeline)
        frame = frame.filter(ImageFilter.UnsharpMask(radius=1.0, percent=40, threshold=3))

        # Ensure destination directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        frame.save(str(dest_path), 'JPEG', quality=quality)
        return True
    except Exception as e:
        logger.error(f'Failed to create video thumbnail for {video_path}: {e}')
        return False


# ---------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------


def detect_scenes(
    path: Path,
    threshold: float = 27.0,
    min_scene_duration: float = 1.0,
) -> list[tuple[float, float]]:
    """Detect scene boundaries in a video using FFmpeg's scene detection.

    Uses ``ffprobe`` with the ``select`` filter to find frames where the
    scene change score exceeds the threshold.  Falls back to uniform
    segmentation if detection fails or the video is very short.

    Args:
        path: Path to the video file.
        threshold: Scene change threshold (0-100).  Higher values detect
            fewer scene changes.  Recommended: 20-35.
        min_scene_duration: Minimum scene duration in seconds. Scenes
            shorter than this are merged with the previous scene.

    Returns:
        List of (start_time, end_time) tuples in seconds. Always returns
        at least one scene covering the entire video.
    """
    meta = get_video_metadata(path)
    if meta is None or meta.duration <= 0:
        return []

    duration = meta.duration

    # For very short videos (< 3s), just use one scene
    if duration < 3.0:
        return [(0.0, duration)]

    # Use ffprobe to detect scene changes via the select filter.
    # This avoids fully decoding the video and is much faster than
    # frame-by-frame analysis in Python.
    try:
        cmd = [
            'ffprobe',
            '-v',
            'quiet',
            '-show_entries',
            'frame=pts_time',
            '-of',
            'csv=p=0',
            '-f',
            'lavfi',
            f"movie='{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}',"
            f"select='gt(scene,{threshold / 100.0})'",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        scene_times: list[float] = [0.0]
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line:
                    try:
                        t = float(line)
                        if t > scene_times[-1] + min_scene_duration:
                            scene_times.append(t)
                    except ValueError:
                        continue

    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f'ffprobe scene detection unavailable ({e}), using uniform segments')
        scene_times = [0.0]

    # Build scene list from boundary timestamps
    scenes: list[tuple[float, float]] = []
    for i in range(len(scene_times)):
        start = scene_times[i]
        end = scene_times[i + 1] if i + 1 < len(scene_times) else duration
        scenes.append((start, end))

    # If no scene changes were detected, fall back to uniform segments
    if len(scenes) <= 1:
        return _uniform_scenes(duration)

    return scenes


def _uniform_scenes(duration: float, target_length: float = 30.0) -> list[tuple[float, float]]:
    """Split a video into uniform segments of approximately ``target_length``.

    Used as a fallback when scene detection finds no boundaries.

    Args:
        duration: Total video duration in seconds.
        target_length: Target segment length in seconds.

    Returns:
        List of (start, end) tuples.
    """
    if duration <= target_length:
        return [(0.0, duration)]

    num_segments = max(1, round(duration / target_length))
    segment_len = duration / num_segments
    return [(i * segment_len, min((i + 1) * segment_len, duration)) for i in range(num_segments)]


# ---------------------------------------------------------------------------
# Multi-frame extraction per scene
# ---------------------------------------------------------------------------


def extract_scene_frames(
    path: Path,
    scenes: list[tuple[float, float]],
    interval: float = 2.0,
    max_per_scene: int = 10,
) -> list[tuple[int, float, Image.Image]]:
    """Extract frames from each scene at regular intervals.

    For each scene, extracts frames spaced ``interval`` seconds apart,
    up to ``max_per_scene`` frames.  The first frame is always at the
    scene midpoint (used as keyframe) and additional frames are spread
    evenly.

    Args:
        path: Path to the video file.
        scenes: List of (start_time, end_time) tuples from ``detect_scenes()``.
        interval: Seconds between sampled frames within each scene.
        max_per_scene: Maximum number of frames to extract per scene.

    Returns:
        List of (scene_index, frame_time, pil_image) tuples. Failed
        extractions are silently skipped.
    """
    results: list[tuple[int, float, Image.Image]] = []

    for scene_idx, (start, end) in enumerate(scenes):
        scene_duration = end - start
        if scene_duration <= 0:
            continue

        # Calculate frame times within this scene
        frame_times: list[float] = []
        if scene_duration <= interval:
            # Short scene: just take the midpoint
            frame_times.append(start + scene_duration / 2)
        else:
            t = start + interval / 2  # Small offset from start to avoid transition frames
            while t < end and len(frame_times) < max_per_scene:
                frame_times.append(t)
                t += interval

        # Extract each frame
        for t in frame_times:
            frame = extract_frame(path, t)
            if frame is not None:
                results.append((scene_idx, t, frame))

    return results


# ---------------------------------------------------------------------------
# Audio extraction for STT
# ---------------------------------------------------------------------------


def extract_audio_segment(
    video_path: Path,
    output_path: Path,
    start: float,
    end: float,
) -> bool:
    """Extract an audio segment from a video as a WAV file.

    Uses PyAV to decode the audio stream, resample to 16kHz mono (the
    format expected by Whisper), and write it out as WAV.

    Args:
        video_path: Path to the source video.
        output_path: Where to write the WAV file.
        start: Start time in seconds.
        end: End time in seconds.

    Returns:
        True if audio was successfully extracted, False on failure
        (e.g. no audio stream, decode error).
    """
    if not is_video_supported():
        return False

    import av

    try:
        with av.open(str(video_path)) as container:
            if not container.streams.audio:
                logger.debug(f'No audio stream in {video_path}')
                return False

            audio_stream = container.streams.audio[0]

            # Create resampler to 16kHz mono (Whisper's expected format)
            resampler = av.AudioResampler(
                format='s16',
                layout='mono',
                rate=16000,
            )

            # Seek to start
            start_pts = int(start / audio_stream.time_base) if audio_stream.time_base else int(start * 1000000)
            container.seek(start_pts, stream=audio_stream)

            # Collect audio frames
            audio_frames = []
            for frame in container.decode(audio_stream):
                frame_time = float(frame.pts * audio_stream.time_base) if frame.pts is not None else 0.0
                if frame_time > end:
                    break
                if frame_time >= start - 0.1:  # Small tolerance for seek imprecision
                    resampled = resampler.resample(frame)
                    for rf in resampled:
                        audio_frames.append(rf)

            if not audio_frames:
                return False

            # Write WAV file
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with av.open(str(output_path), 'w') as out_container:
                out_stream = out_container.add_stream('pcm_s16le', rate=16000)
                out_stream.layout = 'mono'
                for af in audio_frames:
                    for packet in out_stream.encode(af):
                        out_container.mux(packet)
                # Flush
                for packet in out_stream.encode(None):
                    out_container.mux(packet)

            return True

    except Exception as e:
        logger.error(f'Failed to extract audio segment from {video_path}: {e}')
        return False
