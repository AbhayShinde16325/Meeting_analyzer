"""Provider selection for the configured analysis backend.

Transcription is always local (Faster-Whisper).  Analysis is selected with
``TEAMMEETINGAI_ANALYSIS_BACKEND``:

* ``none`` (default) - no analysis provider; the pipeline stops at a saved
  transcript with the meeting status ``transcribed``.
* ``groq`` - cloud analysis via the Groq API.  Requires
  ``TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS=1`` because the transcript leaves the
  computer, plus ``TEAMMEETINGAI_GROQ_MODEL`` and
  ``TEAMMEETINGAI_GROQ_API_KEY``.
* ``ollama`` - reserved for a future local backend and deliberately not
  supported in this build.
"""

from __future__ import annotations

from ai.faster_whisper_provider import FasterWhisperProvider
from ai.groq_provider import GroqAnalysisProvider
from ai.summarization import AnalysisProvider
from ai.transcription import TranscriptionProvider
from config import Settings


class ProviderConfigurationError(RuntimeError):
    """Raised when the configured analysis backend cannot be used."""


def build_transcription_provider(settings: Settings) -> TranscriptionProvider:
    """Return the local speech-recognition provider."""

    return FasterWhisperProvider(settings)


def build_analysis_provider(settings: Settings) -> AnalysisProvider | None:
    """Return the analysis provider for the configured backend, or ``None``.

    Raises :class:`ProviderConfigurationError` when the backend is known but
    misconfigured, or unknown entirely.  Failing at startup keeps the error
    visible instead of surfacing it mid-meeting.
    """

    backend = (settings.analysis_backend or "none").strip().lower()
    if backend == "none":
        return None
    if backend == "groq":
        if not settings.allow_cloud_analysis:
            raise ProviderConfigurationError(
                "The Groq analysis backend requires explicit opt-in. Set "
                "TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS=1 after confirming that "
                "meeting transcripts may be sent to Groq."
            )
        return GroqAnalysisProvider(settings)
    if backend == "ollama":
        raise ProviderConfigurationError(
            "The 'ollama' analysis backend is not supported in this build. "
            "Use TEAMMEETINGAI_ANALYSIS_BACKEND=groq or 'none'."
        )
    raise ProviderConfigurationError(
        f"Unknown analysis backend '{backend}'. Supported values: none, groq."
    )


def describe_analysis_mode(settings: Settings, provider: object) -> str:
    """Return a short human-readable description of the analysis mode."""

    if provider is None:
        return "transcription only (no analysis backend configured)"
    model = getattr(provider, "model_name", "") or "configured model"
    if getattr(provider, "is_cloud", False):
        return f"cloud analysis via {model} (the transcript leaves this computer)"
    return f"local analysis via {model}"
