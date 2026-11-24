"""Transcription service contract tests using a fake local provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.transcription import (
    NoSpeechError,
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionResult,
    TranscriptionService,
    _normalise_segments,
)
from support import (
    BlockingFakeTranscriptionProvider,
    FakeTranscriptionProvider,
)


class ReturningProvider:
    def __init__(self, result):
        self.result = result

    def transcribe(self, audio_path, *, language=None, progress_cb=None):
        return self.result


def test_missing_recording_is_rejected_before_provider(settings, tmp_path):
    provider = FakeTranscriptionProvider()
    service = TranscriptionService(settings, provider=provider)
    with pytest.raises(TranscriptionError):
        service.transcribe(tmp_path / "missing.wav")
    assert provider.calls == []


def test_empty_recording_is_rejected(settings, tmp_path):
    path = tmp_path / "empty.wav"
    path.write_bytes(b"RIFF")
    service = TranscriptionService(settings, provider=FakeTranscriptionProvider())
    with pytest.raises(TranscriptionError):
        service.transcribe(path)


def test_successful_transcription_is_normalized(settings, wav_factory):
    path = wav_factory(settings.recordings_dir / "sample.wav")
    provider = FakeTranscriptionProvider(
        result=TranscriptionResult(
            text="  Hello world.  ",
            language="en",
            segments=[
                {"start": "1.25", "end": 2, "text": " Hello world. "},
                {"start": 0, "end": 0, "text": "   "},
                {"start": 3, "end": 4, "text": "Second line."},
            ],
        )
    )
    progress: list[tuple[float, str]] = []
    result = TranscriptionService(settings, provider=provider).transcribe(
        path, progress_cb=lambda fraction, message: progress.append((fraction, message))
    )

    assert result.text == "Hello world."
    assert result.language == "en"
    assert [segment["text"] for segment in result.segments] == [
        "Hello world.",
        "Second line.",
    ]
    assert result.segments[0]["start"] == 1.25
    assert progress and progress[-1][0] == 1.0


def test_unconfigured_provider_raises_clear_error(settings, wav_factory):
    path = wav_factory(settings.recordings_dir / "sample.wav")
    with pytest.raises(TranscriptionError, match="No local transcription provider"):
        TranscriptionService(settings).transcribe(path)


def test_provider_exception_is_wrapped(settings, wav_factory):
    path = wav_factory(settings.recordings_dir / "sample.wav")
    provider = FakeTranscriptionProvider(error=RuntimeError("engine exploded"))
    with pytest.raises(TranscriptionError, match="Transcription failed"):
        TranscriptionService(settings, provider=provider).transcribe(path)


def test_invalid_provider_result_is_rejected(settings, wav_factory):
    path = wav_factory(settings.recordings_dir / "sample.wav")
    service = TranscriptionService(settings, provider=ReturningProvider(object()))
    with pytest.raises(TranscriptionError, match="invalid result"):
        service.transcribe(path)


def test_empty_provider_text_is_rejected(settings, wav_factory):
    path = wav_factory(settings.recordings_dir / "sample.wav")
    service = TranscriptionService(
        settings,
        provider=ReturningProvider(
            TranscriptionResult(text="   ", language="en", segments=[])
        ),
    )
    with pytest.raises(TranscriptionError, match="No speech"):
        service.transcribe(path)


def test_provider_receives_path_and_language(settings, wav_factory):
    path = wav_factory(settings.recordings_dir / "sample.wav")
    provider = FakeTranscriptionProvider()
    TranscriptionService(settings, provider=provider).transcribe(path, language="es")
    assert provider.calls[0]["audio_path"] == path
    assert provider.calls[0]["language"] == "es"


def test_a_meeting_is_transcribed_in_a_single_pass(settings, wav_factory):
    """One recording file means one provider run (no per-track passes)."""

    path = wav_factory(settings.recordings_dir / "meeting.wav")
    provider = FakeTranscriptionProvider()
    result = TranscriptionService(settings, provider=provider).transcribe(path)

    assert len(provider.calls) == 1
    assert provider.calls[0]["audio_path"] == path
    assert [segment["text"] for segment in result.segments] == [
        "Alice: We agreed to ship the release on Friday."
    ]
    # No speaker labels are invented by the service any more.
    assert "speaker" not in result.segments[0]


def test_normalise_segments_preserves_speaker():
    segments = _normalise_segments(
        [
            {"start": 0.0, "end": 1.0, "text": "hello", "speaker": "Me"},
            {"start": 1.0, "end": 2.0, "text": "  ", "speaker": "Remote"},
            {"start": 2.0, "end": 3.0, "text": "no label"},
            {"start": 3.0, "end": 4.0, "text": "blank label", "speaker": "   "},
        ]
    )
    assert segments == [
        {"start": 0.0, "end": 1.0, "text": "hello", "speaker": "Me"},
        {"start": 2.0, "end": 3.0, "text": "no label"},
        {"start": 3.0, "end": 4.0, "text": "blank label"},
    ]


def test_cancelled_transcription_propagates(settings, wav_factory):
    path = wav_factory(settings.recordings_dir / "cancel.wav")
    service = TranscriptionService(
        settings, provider=BlockingFakeTranscriptionProvider()
    )
    with pytest.raises(TranscriptionCancelled):
        service.transcribe(path, cancel_cb=lambda: True)
