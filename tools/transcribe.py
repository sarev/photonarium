#!/usr/bin/env python3
"""Transcribe an audio file using faster-whisper."""

import sys

if len(sys.argv) < 2:
    print(f'Usage: {sys.argv[0]} <audio_file> [language]')
    print('  language: en, fr, de, etc. (default: auto-detect)')
    sys.exit(1)

audio_path = sys.argv[1]
language = sys.argv[2] if len(sys.argv) > 2 else None

from faster_whisper import WhisperModel

print('Loading model...', flush=True)
model = WhisperModel('large-v3', device='cuda', compute_type='float16')

print(f'Transcribing {audio_path}...', flush=True)
segments, info = model.transcribe(
    audio_path,
    language=language,
    beam_size=5,
    vad_filter=True,
)

if language is None:
    print(f'Detected language: {info.language} (probability {info.language_probability:.2f})\n')

for segment in segments:
    print(f'[{segment.start:.1f}s - {segment.end:.1f}s] {segment.text.strip()}')
