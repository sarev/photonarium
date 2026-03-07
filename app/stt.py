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


class STTBackend(ABC):
    """Abstract base class for speech-to-text backends."""

    @abstractmethod
    def transcribe(self, audio_path: Path, language: str = '') -> list[STTSegment]:
        """Transcribe an audio file and return timed segments.

        Args:
            audio_path: Path to a WAV audio file (16kHz mono).
            language: Language code (e.g. 'en', 'fr').  Empty string means
                auto-detect.

        Returns:
            List of STTSegment named tuples with start/end times and text.
            Returns an empty list on failure.
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

                # Use GPU if available, fall back to CPU
                device = 'auto'
                compute_type = 'auto'

                logger.info(f'Loading faster-whisper model: {self._model_size}')
                self._model = WhisperModel(
                    self._model_size,
                    device=device,
                    compute_type=compute_type,
                )
                logger.info(f'faster-whisper model loaded: {self._model_size}')
                return True

            except (MemoryError, RuntimeError) as e:
                logger.error(f'Failed to load faster-whisper model (OOM): {e}')
                self._load_failed = True
                try:
                    import torch

                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except ImportError:
                    pass
                return False
            except Exception as e:
                logger.error(f'Failed to load faster-whisper model: {e}')
                self._load_failed = True
                return False

    def transcribe(self, audio_path: Path, language: str = '') -> list[STTSegment]:
        """Transcribe audio using faster-whisper.

        Args:
            audio_path: Path to 16kHz mono WAV file.
            language: Language code or empty for auto-detect.

        Returns:
            List of STTSegment tuples, or empty list on failure.
        """
        if not self._load_model():
            return []

        try:
            kwargs = {}
            if language:
                kwargs['language'] = language

            segments, _info = self._model.transcribe(
                str(audio_path),
                beam_size=5,
                **kwargs,
            )

            result = []
            for seg in segments:
                text = seg.text.strip()
                if text:
                    result.append(STTSegment(start=seg.start, end=seg.end, text=text))

            return result

        except (MemoryError, RuntimeError) as e:
            logger.error(f'OOM during transcription of {audio_path}: {e}')
            self._load_failed = True
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass
            return []
        except Exception as e:
            logger.error(f'Transcription failed for {audio_path}: {e}')
            return []


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
