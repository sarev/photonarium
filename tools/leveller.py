#!/usr/bin/env python3
"""Aggressive audio levelling — boost quiet speakers, tame loud ones.

Two-pass approach:
  1. Analyse the whole file to find the noise floor (true silence)
  2. Apply per-window gain to bring everything above the noise floor
     up to the target level, with massive gain allowed for very quiet
     speech and hard limiting on the loud sections.

Usage:
    python leveller.py input.mp3 output.mp3
    python leveller.py input.mp3 output.wav --window 0.5 --target -18
    python leveller.py input.mp3 output.mp3 --first 4:30  # only process first 4m30s

Requires: pip install pydub numpy
(pydub needs ffmpeg on the PATH for mp3 support)
"""

import argparse
import os
import sys

import numpy as np

# Ensure ffmpeg/ffprobe from ffmpeg-binaries pip package are findable
try:
    from pathlib import Path
    _bindir = Path(__import__('ffmpeg').__path__[0]) / 'binaries'
    if _bindir.is_dir():
        os.environ['PATH'] = str(_bindir) + os.pathsep + os.environ.get('PATH', '')
except (ImportError, IndexError):
    pass


def _rms_array(mono: np.ndarray, window_size: int, hop_size: int) -> np.ndarray:
    """Compute per-window RMS values for the entire signal."""
    num_samples = len(mono)
    num_hops = max(1, (num_samples - window_size) // hop_size + 1)
    rms = np.empty(num_hops, dtype=np.float64)
    for i in range(num_hops):
        start = i * hop_size
        end = min(start + window_size, num_samples)
        rms[i] = np.sqrt(np.mean(mono[start:end] ** 2))
    return rms


def _estimate_noise_floor(rms_values: np.ndarray) -> float:
    """Estimate the noise floor from the RMS distribution.

    Takes the 5th percentile of non-zero RMS values as the noise floor.
    Anything below this is true silence / background noise that should
    not be amplified.
    """
    nonzero = rms_values[rms_values > 0]
    if len(nonzero) == 0:
        return 0.0
    return float(np.percentile(nonzero, 5))


def level_audio(
    samples: np.ndarray,
    sample_rate: int,
    window_sec: float,
    target_dbfs: float,
) -> np.ndarray:
    """Apply aggressive short-window RMS levelling.

    Pass 1: Measure per-window RMS and estimate the true noise floor.
    Pass 2: Compute gain per window — everything above the noise floor
    gets boosted/cut to the target level.  Gains are smoothed with a
    short median filter (preserves edges between loud/quiet sections
    better than a moving average) then interpolated to per-sample.
    A hard limiter prevents clipping.

    Args:
        samples: Audio as float64, shape (N,) or (N, channels).
        sample_rate: Sample rate in Hz.
        window_sec: Analysis window in seconds.
        target_dbfs: Target RMS level in dBFS.

    Returns:
        Levelled audio as float64, same shape as input.
    """
    mono = samples.mean(axis=1) if samples.ndim == 2 else samples
    num_samples = len(mono)
    window_size = int(sample_rate * window_sec)
    hop_size = window_size // 4  # 75% overlap

    target_rms = 10 ** (target_dbfs / 20.0)

    # ── Pass 1: analyse ──
    rms = _rms_array(mono, window_size, hop_size)
    noise_floor = _estimate_noise_floor(rms)
    # Speech threshold: 2x the noise floor — anything below this is
    # background hiss, not a quiet speaker.
    speech_threshold = noise_floor * 2.0

    num_hops = len(rms)
    speech_count = np.sum(rms > speech_threshold)
    quiet_count = np.sum((rms > speech_threshold) & (rms < target_rms * 0.1))

    print(f'  Noise floor: {20 * np.log10(noise_floor + 1e-10):.1f} dBFS')
    print(f'  Speech threshold: {20 * np.log10(speech_threshold + 1e-10):.1f} dBFS')
    print(f'  Windows: {num_hops} total, {speech_count} with speech, {quiet_count} very quiet speech')

    # ── Pass 2: compute gains ──
    gains = np.ones(num_hops, dtype=np.float64)
    max_gain = 1000.0  # 60 dB boost — enough for near-inaudible speech

    for i in range(num_hops):
        if rms[i] > speech_threshold:
            gains[i] = min(target_rms / rms[i], max_gain)
        else:
            # Below speech threshold — apply modest gain (don't amplify
            # silence to full volume, but don't leave it untouched either
            # in case there's faint speech mixed with noise).
            gains[i] = min(target_rms / max(rms[i], noise_floor * 0.5) * 0.3, max_gain * 0.1)

    # Smooth with a short median filter — preserves the sharp gain
    # transitions between loud and quiet speakers better than a moving
    # average (which drags quiet gains down when adjacent to loud sections).
    from scipy.ndimage import median_filter
    smooth_radius = max(3, int(0.15 / (hop_size / sample_rate)))  # ~150ms
    smooth_radius = smooth_radius | 1  # Must be odd
    gains_smooth = median_filter(gains, size=smooth_radius)

    # Second pass: gentle moving average to remove any remaining steps
    avg_window = max(3, smooth_radius // 2) | 1
    kernel = np.ones(avg_window) / avg_window
    gains_smooth = np.convolve(gains_smooth, kernel, mode='same')

    # Interpolate to per-sample
    hop_centres = np.arange(num_hops) * hop_size + window_size // 2
    sample_indices = np.arange(num_samples)
    gain_envelope = np.interp(sample_indices, hop_centres, gains_smooth)

    # Apply gain
    if samples.ndim == 2:
        levelled = samples * gain_envelope[:, np.newaxis]
    else:
        levelled = samples * gain_envelope

    # Hard limiter — clip to 0.95 to prevent distortion
    np.clip(levelled, -0.95, 0.95, out=levelled)

    # Report what we did
    peak_gain_db = 20 * np.log10(np.max(gains_smooth) + 1e-10)
    min_gain_db = 20 * np.log10(np.min(gains_smooth[gains_smooth > 0]) + 1e-10)
    print(f'  Gain range: {min_gain_db:+.1f} dB to {peak_gain_db:+.1f} dB')

    return levelled


def parse_time(s: str) -> float:
    """Parse a time string like '4:30' or '270' to seconds."""
    parts = s.split(':')
    if len(parts) == 1:
        return float(parts[0])
    elif len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    raise ValueError(f'Invalid time format: {s}')


def main():
    parser = argparse.ArgumentParser(
        description='Aggressive audio levelling — equalise speaker volumes.',
    )
    parser.add_argument('input', help='Input audio file (mp3, wav, etc.)')
    parser.add_argument('output', help='Output audio file')
    parser.add_argument(
        '--window', type=float, default=0.3,
        help='Analysis window in seconds (default: 0.3)',
    )
    parser.add_argument(
        '--target', type=float, default=-18.0,
        help='Target RMS level in dBFS (default: -18)',
    )
    parser.add_argument(
        '--first', type=str, default=None,
        help='Only process the first N seconds (e.g. "4:30" or "270"). '
             'The rest is passed through unchanged.',
    )
    args = parser.parse_args()

    try:
        from pydub import AudioSegment
    except ImportError:
        print('Error: pip install pydub  (and ensure ffmpeg is on the PATH)', file=sys.stderr)
        sys.exit(1)

    print(f'Loading {args.input}...', flush=True)
    audio = AudioSegment.from_file(args.input)
    sample_rate = audio.frame_rate
    channels = audio.channels

    # Convert to numpy float64
    samples = np.array(audio.get_array_of_samples(), dtype=np.float64)
    max_val = float(2 ** (audio.sample_width * 8 - 1))
    samples /= max_val

    if channels > 1:
        samples = samples.reshape(-1, channels)

    # Optionally split into "process" and "passthrough" sections
    if args.first:
        first_sec = parse_time(args.first)
        split_sample = int(first_sec * sample_rate)
        if samples.ndim == 2:
            process_samples = samples[:split_sample]
            passthrough = samples[split_sample:]
        else:
            process_samples = samples[:split_sample]
            passthrough = samples[split_sample:]
        print(f'Processing first {first_sec:.1f}s ({split_sample} samples), '
              f'passing through remaining {len(passthrough)} samples', flush=True)
    else:
        process_samples = samples
        passthrough = None

    print(f'Levelling ({len(process_samples) / sample_rate:.1f}s, {sample_rate}Hz, '
          f'{channels}ch, window={args.window}s, target={args.target}dBFS)...', flush=True)

    levelled = level_audio(process_samples, sample_rate, args.window, args.target)

    if passthrough is not None:
        levelled = np.concatenate([levelled, passthrough], axis=0)

    # Convert back to int samples
    levelled_int = np.clip(levelled * max_val, -max_val, max_val - 1).astype(
        np.int16 if audio.sample_width == 2 else np.int32
    )
    if channels > 1:
        levelled_int = levelled_int.flatten()

    output = AudioSegment(
        data=levelled_int.tobytes(),
        sample_width=audio.sample_width,
        frame_rate=sample_rate,
        channels=channels,
    )

    print(f'Writing {args.output}...', flush=True)
    output_format = args.output.rsplit('.', 1)[-1].lower()
    output.export(args.output, format=output_format)
    print('Done.')


if __name__ == '__main__':
    main()
