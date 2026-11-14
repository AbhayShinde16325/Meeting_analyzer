"""Transcription service contract tests using a fake local provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai.transcription import (
    NoSpeechError,
    SPEAKER_ME,
    SPEAKER_REMOTE,
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


# ---------------------------------------------------------------------------
# Dual-track transcription (Me / Remote labels)
# ---------------------------------------------------------------------------


class TrackRecordingProvider:
    """Returns a per-file result so dual-track merging can be verified."""

    def __init__(self, results: dict[str, TranscriptionResult | Exception]):
        self.results = results
        self.calls: list[Path] = []

    def transcribe(self, audio_path, *, language=None, progress_cb=None, cancel_cb=None):
        path = Path(audio_path)
        self.calls.append(path)
        for key, value in self.results.items():
            if key in path.name:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected transcription path: {path}")


def _track_paths(settings, wav_factory):
    mixed = wav_factory(settings.recordings_dir / "m.mixed.wav", seconds=0.05)
    mic = wav_factory(settings.recordings_dir / "m.mic.wav", seconds=0.05)
    system = wav_factory(
        settings.recordings_dir / "m.system.wav", seconds=0.05, channels=1
    )
    return mixed, mic, system


def test_transcribe_meeting_labels_and_merges_both_tracks(
    settings, wav_factory
):
    mixed, mic, system = _track_paths(settings, wav_factory)
    provider = TrackRecordingProvider(
        {
            ".mic.": TranscriptionResult(
                text="Later me line.",
                language="en",
                segments=[{"start": 5.0, "end": 6.0, "text": "Later me line."}],
            ),
            ".system.": TranscriptionResult(
                text="Remote answer.",
                language="en",
                segments=[
                    {"start": 1.0, "end": 2.0, "text": "Remote answer."},
                    {"start": 2.2, "end": 2.8, "text": "Second remote line."},
                ],
            ),
        }
    )
    service = TranscriptionService(settings, provider=provider)  # type: ignore[arg-type]
    result = service.transcribe_meeting(mixed, mic, system)

    # Segments are merged in timestamp order across tracks.
    assert [segment["start"] for segment in result.segments] == [1.0, 2.2, 5.0]
    assert [segment["speaker"] for segment in result.segments] == [
        SPEAKER_REMOTE,
        SPEAKER_REMOTE,
        SPEAKER_ME,
    ]
    assert result.text == (
        f"{SPEAKER_REMOTE}: Remote answer.\n"
        f"{SPEAKER_REMOTE}: Second remote line.\n"
        f"{SPEAKER_ME}: Later me line."
    )
    assert result.language == "en"
    assert len(provider.calls) == 2


def test_transcribe_meeting_single_track_keeps_label(settings, wav_factory):
    mixed = wav_factory(settings.recordings_dir / "s.mixed.wav", seconds=0.05)
    mic = wav_factory(settings.recordings_dir / "s.mic.wav", seconds=0.05)
    provider = TrackRecordingProvider(
        {
            ".mic.": TranscriptionResult(
                text="Only me.", language="en",
                segments=[{"start": 0.0, "end": 1.0, "text": "Only me."}],
            ),
        }
    )
    service = TranscriptionService(settings, provider=provider)  # type: ignore[arg-type]
    result = service.transcribe_meeting(mixed, mic, settings.recordings_dir / "missing.wav")

    assert [segment["speaker"] for segment in result.segments] == [SPEAKER_ME]
    assert result.text == f"{SPEAKER_ME}: Only me."


def test_transcribe_meeting_falls_back_to_mixed_file(settings, wav_factory):
    mixed = wav_factory(settings.recordings_dir / "f.mixed.wav", seconds=0.05)
    provider = TrackRecordingProvider(
        {
            ".mixed.": TranscriptionResult(
                text="Mixed speech.", language="en",
                segments=[{"start": 0.0, "end": 1.0, "text": "Mixed speech."}],
            ),
        }
    )
    service = TranscriptionService(settings, provider=provider)  # type: ignore[arg-type]
    result = service.transcribe_meeting(
        mixed,
        settings.recordings_dir / "f.mic.wav",
        settings.recordings_dir / "f.system.wav",
    )

    # The mixed fallback carries no speaker labels.
    assert result.segments == [
        {"start": 0.0, "end": 1.0, "text": "Mixed speech."}
    ]
    assert result.text == "Mixed speech."
    assert provider.calls == [Path(mixed)]


def test_transcribe_meeting_skips_silent_track(settings, wav_factory):
    mixed, mic, system = _track_paths(settings, wav_factory)
    provider = TrackRecordingProvider(
        {
            ".mic.": NoSpeechError("No speech was recognized in the recording."),
            ".system.": TranscriptionResult(
                text="Remote only.", language="en",
                segments=[{"start": 0.5, "end": 1.5, "text": "Remote only."}],
            ),
        }
    )
    service = TranscriptionService(settings, provider=provider)  # type: ignore[arg-type]
    result = service.transcribe_meeting(mixed, mic, system)

    assert [segment["speaker"] for segment in result.segments] == [SPEAKER_REMOTE]
    assert result.text == f"{SPEAKER_REMOTE}: Remote only."


def test_transcribe_meeting_all_tracks_silent_raises(settings, wav_factory):
    mixed, mic, system = _track_paths(settings, wav_factory)
    provider = TrackRecordingProvider(
        {
            ".mic.": NoSpeechError("No speech was recognized in the recording."),
            ".system.": NoSpeechError("No speech was recognized in the recording."),
        }
    )
    service = TranscriptionService(settings, provider=provider)  # type: ignore[arg-type]
    with pytest.raises(NoSpeechError, match="No speech"):
        service.transcribe_meeting(mixed, mic, system)


def test_transcribe_meeting_progress_spans_both_tracks(settings, wav_factory):
    mixed, mic, system = _track_paths(settings, wav_factory)
    provider = TrackRecordingProvider(
        {
            ".mic.": TranscriptionResult(
                text="Me line.", language="en",
                segments=[{"start": 0.0, "end": 1.0, "text": "Me line."}],
            ),
            ".system.": TranscriptionResult(
                text="Remote line.", language="en",
                segments=[{"start": 0.0, "end": 1.0, "text": "Remote line."}],
            ),
        }
    )
    service = TranscriptionService(settings, provider=provider)  # type: ignore[arg-type]
    progress: list[float] = []
    service.transcribe_meeting(
        mixed,
        mic,
        system,
        progress_cb=lambda fraction, _message: progress.append(fraction),
    )

    assert progress and progress[-1] == 1.0
    # The mic track maps to the first half, the system track to the second.
    assert any(fraction < 0.5 for fraction in progress)
    assert any(0.5 <= fraction < 1.0 for fraction in progress)


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


def test_cancelled_dual_track_transcription_propagates(settings, wav_factory):
    mixed, mic, system = _track_paths(settings, wav_factory)
    service = TranscriptionService(
        settings, provider=BlockingFakeTranscriptionProvider()
    )
    with pytest.raises(TranscriptionCancelled):
        service.transcribe_meeting(mixed, mic, system, cancel_cb=lambda: True)
