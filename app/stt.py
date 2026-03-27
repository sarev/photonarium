"""
Pluggable speech-to-text (STT) interface for Photonarium.

Provides an abstract base class for STT backends and a concrete implementation
using faster-whisper.  The module is designed for graceful degradation: if
faster-whisper is not installed, `get_stt_backend()` returns None and the
caller simply skips transcription.

OOM protection follows the project-wide pattern: `_load_failed` flag prevents
retry loops after a failed model load, and `torch.cuda.empty_cache()` is
called on GPU OOM.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from config import Config

logger = logging.getLogger(__name__)


class STTSegment(NamedTuple):
    """A single transcription segment with timing information."""

    start: float  # seconds
    end: float  # seconds
    text: str


class STTResult(NamedTuple):
    """Result of a transcription with detected language info."""

    segments: list[STTSegment]
    language: str  # ISO 639-1 code (e.g. 'en'), empty if unknown


class STTBackend(ABC):
    """Abstract base class for speech-to-text backends."""

    @abstractmethod
    def transcribe(self, audio_path: Path, language: str = '') -> STTResult:
        """Transcribe an audio file and return timed segments with language.

        Args:
            audio_path: Path to a WAV audio file (16kHz mono).
            language: Language code (e.g. 'en', 'fr').  Empty string means
                auto-detect.

        Returns:
            STTResult with segments and detected language code.
            Returns STTResult(segments=[], language='') on failure.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether this backend's dependencies are installed.

        Returns:
            True if the backend can be used, False otherwise.
        """
        ...


class FasterWhisperBackend(STTBackend):
    """STT backend using faster-whisper (CTranslate2-based Whisper).

    The model is loaded lazily on the first transcription call.  OOM
    protection: if the model fails to load (MemoryError or RuntimeError),
    `_load_failed` is set and subsequent calls return early without
    retrying.
    """

    def __init__(self, model_size: str = 'base') -> None:
        self._model_size = model_size
        self._model = None
        self._model_lock = threading.Lock()
        self._load_failed = False
        self._gpu_health = None  # Set via set_gpu_health()

    def set_gpu_health(self, gpu_health: object) -> None:
        """Set the centralised GPU health tracker (avoids circular imports)."""
        self._gpu_health = gpu_health

    def is_available(self) -> bool:
        """Check if faster-whisper is importable."""
        try:
            import faster_whisper  # noqa: F401

            return True
        except ImportError:
            return False

    def _load_model(self) -> bool:
        """Lazy-load the Whisper model.

        Uses the project-wide OOM protection pattern: double-checked
        locking with a `_load_failed` flag to prevent retry loops.

        Returns:
            True if the model is ready, False otherwise.
        """
        if self._load_failed:
            return False
        if self._model is not None:
            return True

        with self._model_lock:
            if self._load_failed:
                return False
            if self._model is not None:
                return True

            try:
                from faster_whisper import WhisperModel

                # Use GPU health tracker if available, otherwise auto-detect
                if self._gpu_health:
                    gh_device = self._gpu_health.device
                    device = gh_device if gh_device != 'xpu' else 'cpu'  # faster-whisper doesn't support xpu
                    compute_type = 'auto'
                else:
                    device = 'auto'
                    compute_type = 'auto'

                logger.info(f'Loading faster-whisper model: {self._model_size}')
                t0 = time.perf_counter()
                self._model = WhisperModel(
                    self._model_size,
                    device=device,
                    compute_type=compute_type,
                )
                logger.info(
                    'faster-whisper model loaded: %s (%.1fs)',
                    self._model_size, time.perf_counter() - t0,
                )
                return True

            except (MemoryError, RuntimeError) as e:
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except ImportError:
                    pass
                from gputil import is_oom_error
                if is_oom_error(e):
                    self._load_failed = True
                    logger.error(f'OOM loading faster-whisper model: {e}')
                elif self._gpu_health:
                    self._gpu_health.report_failure('transcription')
                    # Don't set _load_failed — let next call retry with new device
                else:
                    self._load_failed = True
                    logger.error(f'GPU error loading faster-whisper model: {e}')
                return False
            except Exception as e:
                self._load_failed = True
                logger.error(f'Failed to load faster-whisper model: {e}')
                return False

    def transcribe(self, audio_path: Path, language: str = '') -> STTResult:
        """Transcribe audio using faster-whisper.

        Args:
            audio_path: Path to 16kHz mono WAV file.
            language: Language code or empty for auto-detect.

        Returns:
            STTResult with segments and detected language code.
        """
        if not self._load_model():
            return STTResult(segments=[], language='')

        try:
            kwargs = {}
            if language:
                kwargs['language'] = language

            segments, info = self._model.transcribe(
                str(audio_path),
                beam_size=5,
                word_timestamps=True,
                **kwargs,
            )

            detected_language = getattr(info, 'language', '') or ''

            result = []
            for seg in segments:
                # With word_timestamps=True, each segment carries individual
                # word timings.  Emit per-word STTSegments so the pipeline
                # can assign text to scenes with fine granularity.
                if seg.words:
                    for word in seg.words:
                        text = word.word.strip()
                        if text:
                            result.append(STTSegment(start=word.start, end=word.end, text=text))
                else:
                    # Fallback if words are missing (shouldn't happen)
                    text = seg.text.strip()
                    if text:
                        result.append(STTSegment(start=seg.start, end=seg.end, text=text))

            return STTResult(segments=result, language=detected_language)

        except (MemoryError, RuntimeError) as e:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass
            from gputil import is_oom_error
            if is_oom_error(e):
                self._load_failed = True
                logger.error(f'OOM during transcription of {audio_path}: {e}')
            elif self._gpu_health:
                self._gpu_health.report_failure('transcription')
            else:
                self._load_failed = True
                logger.error(f'GPU error during transcription of {audio_path}: {e}')
            return STTResult(segments=[], language='')
        except Exception as e:
            logger.error(f'Transcription failed for {audio_path}: {e}')
            return STTResult(segments=[], language='')


def get_stt_backend(config: Config) -> STTBackend | None:
    """Factory function to create an STT backend based on configuration.

    Returns None if STT is disabled or the required package is not installed.

    Args:
        config: Application configuration.

    Returns:
        An STTBackend instance, or None if STT is unavailable.
    """
    if not config.stt_enabled:
        return None

    backend = FasterWhisperBackend(model_size=config.stt_model)
    if not backend.is_available():
        logger.warning(
            'STT enabled in config but faster-whisper is not installed. Install with: pip install faster-whisper'
        )
        return None

    return backend


# ---------------------------------------------------------------------------
# CLI test harness
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    import sys
    import tempfile

    parser = argparse.ArgumentParser(
        description='Test the STT backend by transcribing an audio or video file.',
    )
    parser.add_argument(
        'file',
        help='Path to a WAV audio file or a video file.  Video files are '
        'converted to 16kHz mono WAV automatically via video.extract_audio_segment().',
    )
    parser.add_argument(
        '-m',
        '--model',
        default='base',
        help='Whisper model size: tiny, base, small, medium, large-v3 (default: base)',
    )
    parser.add_argument(
        '-l',
        '--language',
        default='',
        help='Language code (e.g. "en", "fr").  Empty for auto-detect (default).',
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    backend = FasterWhisperBackend(model_size=args.model)
    if not backend.is_available():
        print('ERROR: faster-whisper is not installed.  pip install faster-whisper')
        sys.exit(1)

    input_path = Path(args.file).resolve()
    if not input_path.exists():
        print(f'ERROR: file not found: {input_path}')
        sys.exit(1)

    # If not a .wav file, try extracting audio from it as a video
    tmp_wav = None
    if input_path.suffix.lower() != '.wav':
        try:
            from video import extract_audio_segment
        except ImportError:
            print('ERROR: video.py not importable (run from the app/ directory)')
            sys.exit(1)

        tmp_fd, tmp_name = tempfile.mkstemp(suffix='.wav')
        import os

        os.close(tmp_fd)
        tmp_wav = Path(tmp_name)

        print(f'Extracting audio from {input_path.name}...')
        if not extract_audio_segment(input_path, tmp_wav, 0.0, float('inf')):
            print('ERROR: failed to extract audio from video')
            tmp_wav.unlink(missing_ok=True)
            sys.exit(1)
        input_path = tmp_wav

    # Transcribe
    print(f'Transcribing with model={args.model}, language={args.language or "(auto)"}...')
    stt_result = backend.transcribe(input_path, language=args.language)

    # Clean up temp file
    if tmp_wav:
        tmp_wav.unlink(missing_ok=True)

    if stt_result.language:
        print(f'Detected language: {stt_result.language}')

    if not stt_result.segments:
        print('No speech detected.')
        sys.exit(0)

    # Print results
    print(f'\n{len(stt_result.segments)} word segments:\n')
    print(f'{"Start":>8s}  {"End":>8s}  Text')
    print(f'{"-----":>8s}  {"---":>8s}  ----')
    for seg in stt_result.segments:
        print(f'{seg.start:8.2f}s {seg.end:8.2f}s  {seg.text}')

    # Also print a reconstructed full transcript
    full = ' '.join(seg.text for seg in stt_result.segments)
    print(f'\nFull transcript:\n{full}')
