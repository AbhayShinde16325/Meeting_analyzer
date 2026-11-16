"""Fallback-wrapper tests: Sarvam primary, Whisper fallback, no fallback on
cancellation, and both-failures behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.fallback_provider import FallbackTranscriptionProvider
from ai.transcription import (
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionResult,
)


class StubProvider:
    provider_name = "stub"

    def __init__(self, *, name: str, result=None, error=None, cancel=False):
        self.provider_name = name
        self.model_name = f"{name}-model"
        self.result = result
        self.error = error
        self.cancel = cancel
        self.calls = 0

    def transcribe(self, audio_path, *, language=None, progress_cb=None, cancel_cb=None):
        self.calls += 1
        if self.cancel:
            raise TranscriptionCancelled("cancelled")
        if self.error is not None:
            raise self.error
        return self.result


RESULT = TranscriptionResult(
    text="hello", language="en", segments=[{"start": 0.0, "end": 1.0, "text": "hello"}]
)


class TestPrimarySuccess:
    def test_no_fallback_when_primary_succeeds(self, tmp_path):
        primary = StubProvider(name="sarvam", result=RESULT)
        fallback = StubProvider(name="whisper", result=RESULT)
        wrapped = FallbackTranscriptionProvider(primary, fallback)
        result = wrapped.transcribe(tmp_path / "a.wav")
        assert result.text == "hello"
        assert primary.calls == 1
        assert fallback.calls == 0
        assert wrapped.fallback_info() == (None, None)


class TestFallbackOnFailure:
    def test_fallback_runs_and_is_recorded(self, tmp_path):
        primary = StubProvider(name="sarvam", error=TranscriptionError("boom"))
        fallback = StubProvider(name="whisper", result=RESULT)
        wrapped = FallbackTranscriptionProvider(primary, fallback)
        result = wrapped.transcribe(tmp_path / "a.wav")
        assert result.text == "hello"
        assert primary.calls == 1
        assert fallback.calls == 1
        fallback_from, reason = wrapped.fallback_info()
        assert fallback_from == "sarvam"
        assert "TranscriptionError" in reason

    def test_non_transcription_errors_also_fallback(self, tmp_path):
        primary = StubProvider(name="sarvam", error=KeyError("x"))
        fallback = StubProvider(name="whisper", result=RESULT)
        wrapped = FallbackTranscriptionProvider(primary, fallback)
        wrapped.transcribe(tmp_path / "a.wav")
        assert wrapped.fallback_info()[0] == "sarvam"


class TestNoFallbackOnCancellation:
    def test_cancellation_is_never_retried(self, tmp_path):
        primary = StubProvider(name="sarvam", cancel=True)
        fallback = StubProvider(name="whisper", result=RESULT)
        wrapped = FallbackTranscriptionProvider(primary, fallback)
        with pytest.raises(TranscriptionCancelled):
            wrapped.transcribe(tmp_path / "a.wav")
        assert fallback.calls == 0


class TestBothFail:
    def test_both_failing_raises_combined_error(self, tmp_path):
        primary = StubProvider(name="sarvam", error=TranscriptionError("outage"))
        fallback = StubProvider(name="whisper", error=TranscriptionError("no engine"))
        wrapped = FallbackTranscriptionProvider(primary, fallback)
        with pytest.raises(TranscriptionError, match="both providers"):
            wrapped.transcribe(tmp_path / "a.wav")
