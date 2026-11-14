"""Local Faster-Whisper speech-recognition adapter.

This is the only place in the application that imports ``faster_whisper``.
The adapter implements the :class:`ai.transcription.TranscriptionProvider`
port, keeps all processing on this machine, and loads the model lazily so a
missing or still-downloading model never blocks application startup.

Model weights are downloaded once from Hugging Face into the application data
directory on first use.  That download contains model weights only - meeting
audio and transcripts never leave this computer.
"""

from __future__ import annotations

import logging
import os
import time
import wave
from pathlib import Path

import numpy as np

from ai.transcription import (
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionResult,
)
from config import Settings

logger = logging.getLogger(__name__)


class FasterWhisperProvider:
    """Transcription provider backed by a local Faster-Whisper model."""

    is_cloud = False

    # Audio whose peak is below this fraction of full scale (-54 dBFS) is
    # treated as silence.  Real speech, even quiet, is far above this; the
    # threshold only catches recordings where nothing was captured at all.
    _SILENCE_PEAK_THRESHOLD = 0.002

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = None

    @property
    def model_name(self) -> str:
        return self.settings.whisper_model_size

    # -- model loading ---------------------------------------------------

    def _resolve_model_reference(self) -> tuple[str, dict[str, object]]:
        """Return the model argument plus WhisperModel keyword arguments.

        The configured model may be a Hugging Face model size (for example
        ``small``) or a local model directory.  A local directory is used
        directly and no download root is configured.
        """

        model_ref = self.settings.whisper_model_size
        kwargs: dict[str, object] = {}

        candidate = Path(model_ref)
        looks_like_path = candidate.is_absolute() or (
            os.sep in model_ref or "/" in model_ref or "\\" in model_ref
        )
        if looks_like_path and candidate.is_dir():
            return model_ref, kwargs

        download_root = self.settings.data_dir / "models" / "whisper"
        try:
            download_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TranscriptionError(
                "Could not create the local speech-recognition model "
                f"directory: {download_root}"
            ) from exc
        kwargs["download_root"] = str(download_root)
        if self.settings.whisper_cpu_threads > 0:
            kwargs["cpu_threads"] = self.settings.whisper_cpu_threads
        return model_ref, kwargs

    def _load_model(self):
        if self._model is not None:
            return self._model

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError(
                "The faster-whisper package is not installed. Install it "
                "with: pip install faster-whisper"
            ) from exc

        model_ref, kwargs = self._resolve_model_reference()
        logger.info(
            "Loading speech-recognition model '%s' (device=%s, compute=%s).",
            model_ref,
            self.settings.whisper_device,
            self.settings.whisper_compute_type,
        )
        try:
            self._model = WhisperModel(
                model_ref,
                device=self.settings.whisper_device,
                compute_type=self.settings.whisper_compute_type,
                **kwargs,
            )
        except Exception as exc:
            logger.error("Speech-recognition model failed to load.")
            raise TranscriptionError(
                f"Could not load the speech-recognition model '{model_ref}'. "
                "Check the model name, device, and compute type settings."
            ) from exc
        return self._model

    # -- transcription ---------------------------------------------------

    def _audio_is_silent(self, audio_path: Path) -> bool:
        """Best-effort silence check for WAV files.

        Returns ``False`` whenever the file cannot be inspected (missing,
        unreadable, or not a supported WAV layout) so real audio is never
        skipped by mistake.  A silent file would only cost Whisper a VAD
        scan and language detection that produce nothing, so skipping it
        roughly halves the processing time when one track was not captured.
        """

        try:
            with wave.open(str(audio_path), "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                sample_width = wav.getsampwidth()
        except (wave.Error, EOFError, OSError):
            return False

        if not frames:
            return True

        if sample_width == 2:
            samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        elif sample_width == 4:
            samples = (
                np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
            )
        elif sample_width == 1:
            samples = (
                np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0
            ) / 128.0
        else:
            return False

        if samples.size == 0:
            return True
        peak = float(np.max(np.abs(samples)))
        return peak < self._SILENCE_PEAK_THRESHOLD

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb=None,
        cancel_cb=None,
    ) -> TranscriptionResult:
        self._check_cancelled(cancel_cb)
        started = time.monotonic()

        if self._audio_is_silent(audio_path):
            logger.info(
                "Audio file %s is silent; skipped speech recognition.",
                audio_path.name,
            )
            return TranscriptionResult(text="", language=None, segments=[])

        model = self._load_model()

        requested_language = language or (self.settings.whisper_language or None)
        options: dict[str, object] = {
            "beam_size": self.settings.whisper_beam_size,
            "vad_filter": True,
            "condition_on_previous_text": True,
        }
        if requested_language:
            options["language"] = requested_language

        if progress_cb is not None:
            progress_cb(0.05, "Transcribing audio locally...")

        segments_iter, info = model.transcribe(str(audio_path), **options)

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        collected: list[dict[str, object]] = []
        for segment in segments_iter:
            # Cancellation is cooperative: it is honoured between segments so
            # a long recording stops promptly without corrupting anything.
            self._check_cancelled(cancel_cb)
            text = (getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            collected.append(
                {
                    "start": float(getattr(segment, "start", 0.0) or 0.0),
                    "end": float(getattr(segment, "end", 0.0) or 0.0),
                    "text": text,
                }
            )
            if progress_cb is not None and duration > 0:
                fraction = max(0.0, min(0.99, collected[-1]["end"] / duration))
                progress_cb(fraction, "Transcribing audio...")

        full_text = "\n".join(str(item["text"]) for item in collected).strip()
        elapsed = time.monotonic() - started
        audio_duration = float(getattr(info, "duration", 0.0) or 0.0)
        if audio_duration > 0 and elapsed > 0:
            logger.info(
                "Transcribed %.1fs of audio from %s in %.1fs (%.1fx realtime).",
                audio_duration,
                audio_path.name,
                elapsed,
                audio_duration / elapsed,
            )
        else:
            logger.info("Transcribed %s in %.1fs.", audio_path.name, elapsed)
        return TranscriptionResult(
            text=full_text,
            language=getattr(info, "language", None),
            segments=collected,
        )

    # -- cancellation ----------------------------------------------------

    @staticmethod
    def _check_cancelled(cancel_cb) -> None:
        if cancel_cb is not None and cancel_cb():
            raise TranscriptionCancelled("Transcription was cancelled.")
