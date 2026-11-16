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

# Languages written in Devanagari script.  A transcript in one of these
# languages that comes back mostly in Latin letters is almost certainly
# romanized or misdetected output rather than a usable transcript.
_DEVANAGARI_LANGUAGES = {"hi", "mr", "ne", "sa", "bho"}

# Frames per read when checking a recording for silence.  Reading in bounded
# chunks keeps a multi-hour meeting from being loaded into memory twice.
_SILENCE_CHUNK_FRAMES = 262_144

# PCM layouts the silence check understands: sample width -> (numpy dtype,
# full-scale divisor, unsigned offset).
_PCM_LAYOUTS: dict[int, tuple[str | None, float, float]] = {
    1: ("uint8", 128.0, 128.0),
    2: ("<i2", 32768.0, 0.0),
    4: ("<i4", 2147483648.0, 0.0),
}


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
        skipped by mistake.  The scan reads bounded chunks, so a long
        recording is never held in memory just to be measured.
        """

        try:
            with wave.open(str(audio_path), "rb") as wav:
                sample_width = wav.getsampwidth()
                dtype, scale, offset = _PCM_LAYOUTS.get(sample_width, (None, None, 0.0))
                if dtype is None:
                    return False

                saw_frames = False
                peak = 0.0
                while True:
                    raw = wav.readframes(_SILENCE_CHUNK_FRAMES)
                    if not raw:
                        break
                    saw_frames = True
                    samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)
                    if offset:
                        samples = samples - offset
                    samples /= scale
                    if samples.size:
                        peak = max(peak, float(np.max(np.abs(samples))))
                        if peak >= self._SILENCE_PEAK_THRESHOLD:
                            return False
        except (wave.Error, EOFError, OSError):
            return False

        return not saw_frames or peak < self._SILENCE_PEAK_THRESHOLD

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

        # An explicit language is the single most effective guardrail: with
        # auto-detection a Hindi meeting is easily misdetected and decoded
        # into gibberish.  ``TEAMMEETINGAI_WHISPER_LANGUAGE=hi`` pins it.
        requested_language = language or (self.settings.whisper_language or None)
        initial_prompt = (self.settings.whisper_initial_prompt or "").strip()
        options: dict[str, object] = {
            # Greedy decoding is markedly faster than the beam search default
            # and is what makes a long meeting finish in reasonable time.
            "beam_size": self.settings.whisper_beam_size,
            "vad_filter": True,
            "vad_parameters": {
                "min_silence_duration_ms": self.settings.whisper_vad_min_silence_ms
            },
            # Decoding each window independently avoids the repetition loops
            # and invented sentences that conditioning can produce, and is
            # slightly cheaper as well.
            "condition_on_previous_text": False,
        }
        if requested_language:
            options["language"] = requested_language
        if initial_prompt:
            # Biases spelling and script (e.g. Devanagari) without adding
            # content; it is forwarded to the model, never executed.
            options["initial_prompt"] = initial_prompt

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
        detected_language = getattr(info, "language", None)
        self._log_language_confidence(
            detected_language,
            float(getattr(info, "language_probability", 0.0) or 0.0),
            pinned=bool(requested_language),
        )
        warning = _script_mismatch_warning(full_text, detected_language)
        if warning:
            logger.warning("%s", warning)
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
            language=detected_language,
            segments=collected,
        )

    @staticmethod
    def _log_language_confidence(
        language: str | None, probability: float, *, pinned: bool
    ) -> None:
        """Log language detection quality without logging transcript text."""

        if not language:
            return
        if pinned:
            logger.info(
                "Transcribed with pinned language '%s' (auto-detect: %.2f).",
                language,
                probability,
            )
            return
        if probability and probability < 0.5:
            logger.warning(
                "Low language-detection confidence (%.2f) for '%s'; the "
                "transcript may be inaccurate. Consider pinning "
                "TEAMMEETINGAI_WHISPER_LANGUAGE.",
                probability,
                language,
            )

    # -- cancellation ----------------------------------------------------

    @staticmethod
    def _check_cancelled(cancel_cb) -> None:
        if cancel_cb is not None and cancel_cb():
            raise TranscriptionCancelled("Transcription was cancelled.")


def _script_mismatch_warning(text: str, language: str | None) -> str | None:
    """Warn when a Devanagari-script language came back in Latin letters.

    Whisper sometimes detects Hindi correctly but writes romanized text, and
    sometimes misdetects it as English altogether.  Both produce a transcript
    that reads as gibberish.  This only counts characters - transcript content
    itself is never logged - so a silent failure at least becomes visible in
    the log.
    """

    if not text or not isinstance(language, str):
        return None
    if language.split("-")[0].lower() not in _DEVANAGARI_LANGUAGES:
        return None

    devanagari = 0
    latin = 0
    for character in text:
        if "\u0900" <= character <= "\u097f":
            devanagari += 1
        elif character.isascii() and character.isalpha():
            latin += 1

    letters = devanagari + latin
    # Too little text to judge, and any Devanagari at all means the model did
    # produce the expected script.
    if letters < 20 or devanagari:
        return None
    return (
        f"Transcript for language '{language}' contains {latin} Latin letters "
        "and no Devanagari script. The recognition may be romanized or "
        "misdetected; check TEAMMEETINGAI_WHISPER_LANGUAGE and "
        "TEAMMEETINGAI_WHISPER_INITIAL_PROMPT."
    )
