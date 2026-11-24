"""Local speech-to-text service contract.

This module intentionally does **not** import or call Faster-Whisper.  It
defines the data types and a provider interface so the speech engine can be
wired in at the application composition root (``main.py``) without coupling
the rest of the application to a specific engine.

A provider is any object implementing :class:`TranscriptionProvider`::

    class MyTranscriptionProvider:
        def transcribe(self, audio_path, *, language, progress_cb):
            # Call the chosen local STT engine here.
            return TranscriptionResult(text=..., language=..., segments=[...])

No audio leaves the machine as long as the wired provider is local.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from ai.segment_postprocess import postprocess_segments
from config import Settings

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]
CancelCallback = Callable[[], bool]


class TranscriptionError(RuntimeError):
    """Raised when audio cannot be transcribed."""


class NoSpeechError(TranscriptionError):
    """Raised when a recording contains no recognizable speech."""


class TranscriptionCancelled(RuntimeError):
    """Raised when a cooperative cancellation was requested mid-transcription.

    This is deliberately **not** a :class:`TranscriptionError`: cancellation is
    a normal, user-initiated outcome, not a failure, so it must never be
    swallowed by generic error handling.
    """


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None
    segments: list[dict[str, object]] = field(default_factory=list)


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Port implemented by a local speech-recognition engine adapter.

    ``cancel_cb`` is optional and backward-compatible: providers that do not
    accept it simply never receive it (the service only passes it when a
    cancellation callback is actually wired in).
    """

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb: ProgressCallback | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> TranscriptionResult:
        ...


class UnconfiguredTranscriptionProvider:
    """Default provider used until the application owner wires an engine."""

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb: ProgressCallback | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> TranscriptionResult:
        raise TranscriptionError(
            "No local transcription provider is configured. Wire a "
            "TranscriptionProvider in main.py and pass it to "
            "TranscriptionService."
        )


class TranscriptionService:
    """Validate recordings and delegate transcription to an injected provider.

    The service owns input validation and result normalisation; the provider
    owns model loading and engine-specific work.  This keeps the UI, database,
    and processing worker independent of any particular STT library.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        provider: TranscriptionProvider | None = None,
    ) -> None:
        self.settings = settings
        self._provider: TranscriptionProvider = (
            provider if provider is not None else UnconfiguredTranscriptionProvider()
        )

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        progress_cb: ProgressCallback | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> TranscriptionResult:
        path = Path(audio_path)
        if not path.is_file():
            raise TranscriptionError("The recording file is missing or unreadable.")
        try:
            if path.stat().st_size <= 44:
                raise TranscriptionError("The recording file is empty.")
        except OSError as exc:
            raise TranscriptionError("The recording file cannot be read.") from exc

        if progress_cb is not None:
            progress_cb(0.02, "Preparing recording for transcription...")

        try:
            # Only forward a cancellation callback when one is wired in so
            # providers with the older two-keyword signature keep working.
            kwargs: dict[str, object] = {}
            if cancel_cb is not None:
                kwargs["cancel_cb"] = cancel_cb
            result = self._provider.transcribe(
                path, language=language, progress_cb=progress_cb, **kwargs  # type: ignore[arg-type]
            )
        except TranscriptionCancelled:
            raise
        except TranscriptionError:
            raise
        except Exception as exc:
            logger.error("Transcription provider failed.")
            raise TranscriptionError(
                "Transcription failed while processing the recording. "
                "Check the configured local speech-recognition provider."
            ) from exc

        if not isinstance(result, TranscriptionResult):
            raise TranscriptionError(
                "The transcription provider returned an invalid result."
            )
        text = (result.text or "").strip()
        if not text:
            raise NoSpeechError(
                "No speech was recognized in the recording. Check that the "
                "microphone and system audio were audible."
            )

        segments = _normalise_segments(result.segments)
        segments, removed = postprocess_segments(segments)
        if removed:
            # Deduplication only ever fires on echo duplications, so the
            # full text must be rebuilt from the kept segments or the
            # duplicated utterance would still reach the analysis stage.
            logger.info("Removed %d duplicated segment(s).", removed)
            text = "\n".join(str(segment["text"]) for segment in segments)
            if not text.strip():
                raise NoSpeechError(
                    "No speech was recognized in the recording. Check that the "
                    "microphone and system audio were audible."
                )
        logger.info("Transcription complete: %s segments.", len(segments))
        if progress_cb is not None:
            progress_cb(1.0, "Transcription complete.")
        return TranscriptionResult(
            text=text,
            language=result.language,
            segments=segments,
        )

def _normalise_segments(
    segments: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    """Keep only well-formed timestamped segments from a provider.

    A ``speaker`` label, when a provider supplies one, is preserved on the
    normalised segment.
    """

    normalised: list[dict[str, object]] = []
    for raw in segments or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", "") or "").strip()
        if not text:
            continue
        try:
            start = float(raw.get("start", 0.0) or 0.0)
            end = float(raw.get("end", start) or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        entry: dict[str, object] = {
            "start": round(max(0.0, start), 3),
            "end": round(max(0.0, end), 3),
            "text": text,
        }
        speaker = raw.get("speaker")
        if isinstance(speaker, str) and speaker.strip():
            entry["speaker"] = speaker.strip()
        for k in ("language", "language_code", "confidence"):
            v = raw.get(k)
            if v is None:
                continue
            if k == "confidence":
                try:
                    entry[k] = float(v)
                except (TypeError, ValueError):
                    pass
            elif isinstance(v, str) and v.strip():
                entry[k] = v.strip()
        normalised.append(entry)
    return normalised
