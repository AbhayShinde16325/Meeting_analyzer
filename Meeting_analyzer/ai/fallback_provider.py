"""Automatic Whisper fallback wrapper around the Sarvam primary provider.

Architecture rule: Sarvam is the mandatory primary; Whisper runs only when
Sarvam fails or is unavailable.  Cancellation is never treated as a failure
(it is a user decision, not a provider error), so a cancelled run is *not*
retried with Whisper.

Whether a fallback happened — and why — is exposed through
``fallback_info()`` so the pipeline can record it in the transcript row and
the processing job.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from ai.transcription import (
    CancelCallback,
    ProgressCallback,
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


class FallbackTranscriptionProvider:
    """Try the primary provider; on any failure run the fallback once."""

    provider_name = "primary+fallback"

    def __init__(self, primary: TranscriptionProvider, fallback: TranscriptionProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_from: str | None = None
        self._fallback_reason: str | None = None
        # The provider that produced the most recent successful result.
        self.last_provider: TranscriptionProvider | None = None

    @property
    def model_name(self) -> str:
        return getattr(self._primary, "model_name", "primary")

    @property
    def is_cloud(self) -> bool:
        return bool(getattr(self._primary, "is_cloud", False))

    def fallback_info(self) -> tuple[str | None, str | None]:
        """Return ``(fallback_from, reason)`` for the most recent call."""

        return self._fallback_from, self._fallback_reason

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb: Callable[[float, str], None] | None = None,
        cancel_cb: CancelCallback | None = None,
    ) -> TranscriptionResult:
        self._fallback_from = None
        self._fallback_reason = None
        self.last_provider = self._primary
        try:
            return self._primary.transcribe(
                audio_path, language=language, progress_cb=progress_cb, cancel_cb=cancel_cb
            )
        except TranscriptionCancelled:
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Primary transcription failed (%s); using the Whisper fallback.",
                type(exc).__name__,
            )
            if progress_cb is not None:
                progress_cb(0.1, "Primary transcription unavailable; using Whisper...")
            try:
                result = self._fallback.transcribe(
                    audio_path, language=language, progress_cb=progress_cb, cancel_cb=cancel_cb
                )
            except TranscriptionCancelled:
                raise
            except Exception as fallback_exc:
                logger.error("Both transcription providers failed.")
                raise TranscriptionError(
                    "Transcription failed on both providers: "
                    f"primary ({reason}); fallback "
                    f"({type(fallback_exc).__name__}: {fallback_exc})"
                ) from fallback_exc
            self._fallback_from = getattr(self._primary, "provider_name", "primary")
            self._fallback_reason = reason
            self.last_provider = self._fallback
            return result
