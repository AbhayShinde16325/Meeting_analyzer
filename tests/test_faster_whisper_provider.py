"""Tests for the local Faster-Whisper transcription provider (fully mocked)."""

from __future__ import annotations

import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from ai.faster_whisper_provider import FasterWhisperProvider
from ai.transcription import TranscriptionError, TranscriptionResult


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class FakeInfo:
    def __init__(self, duration: float, language: str | None) -> None:
        self.duration = duration
        self.language = language


class FakeWhisperModel:
    instances: list["FakeWhisperModel"] = []

    def __init__(
        self,
        model_size_or_path,
        device=None,
        compute_type=None,
        download_root=None,
        cpu_threads=None,
    ):
        self.model_size_or_path = model_size_or_path
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.cpu_threads = cpu_threads
        self.transcribe_calls: list[tuple[str, dict]] = []
        self.segments: list[FakeSegment] = [
            FakeSegment(0.0, 2.0, " Hello there. "),
            FakeSegment(2.5, 4.0, "Second line."),
            FakeSegment(4.5, 5.0, "   "),
        ]
        self.info = FakeInfo(5.0, "en")
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.transcribe_calls.append((str(audio), kwargs))
        return iter(list(self.segments)), self.info


def install_fake_faster_whisper(monkeypatch):
    FakeWhisperModel.instances.clear()
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return module


@pytest.fixture
def provider(settings):
    return FasterWhisperProvider(settings)


def test_construction_is_lazy_without_package(monkeypatch, settings):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    # Constructing must not import faster_whisper or load a model.
    FasterWhisperProvider(settings)


def test_missing_package_gives_clear_error(provider, settings, monkeypatch, wav_factory):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    path = wav_factory(settings.recordings_dir / "sample.wav")
    with pytest.raises(TranscriptionError, match="pip install faster-whisper"):
        provider.transcribe(path)


def test_model_settings_are_passed(provider, settings, monkeypatch, wav_factory):
    install_fake_faster_whisper(monkeypatch)
    path = wav_factory(settings.recordings_dir / "sample.wav")
    provider.transcribe(path)

    model = FakeWhisperModel.instances[-1]
    assert model.model_size_or_path == "small"
    assert model.device == "cpu"
    assert model.compute_type == "int8"
    assert model.download_root == str(settings.data_dir / "models" / "whisper")
    assert model.cpu_threads is None


def test_transcribe_options_and_result(provider, settings, monkeypatch, wav_factory):
    install_fake_faster_whisper(monkeypatch)
    path = wav_factory(settings.recordings_dir / "sample.wav")
    result = provider.transcribe(path, language="es")

    model = FakeWhisperModel.instances[-1]
    call_path, kwargs = model.transcribe_calls[0]
    assert call_path == str(path)
    assert kwargs["beam_size"] == 5
    assert kwargs["vad_filter"] is True
    assert kwargs["condition_on_previous_text"] is True
    assert kwargs["language"] == "es"

    assert isinstance(result, TranscriptionResult)
    assert result.language == "en"
    assert result.text == "Hello there.\nSecond line."
    assert result.segments == [
        {"start": 0.0, "end": 2.0, "text": "Hello there."},
        {"start": 2.5, "end": 4.0, "text": "Second line."},
    ]


def test_language_defaults_to_settings_value(settings, monkeypatch, wav_factory):
    install_fake_faster_whisper(monkeypatch)
    provider = FasterWhisperProvider(replace(settings, whisper_language="de"))
    path = wav_factory(settings.recordings_dir / "sample.wav")
    provider.transcribe(path)
    _, kwargs = FakeWhisperModel.instances[-1].transcribe_calls[0]
    assert kwargs["language"] == "de"


def test_auto_detect_omits_language(settings, monkeypatch, wav_factory):
    install_fake_faster_whisper(monkeypatch)
    path = wav_factory(settings.recordings_dir / "sample.wav")
    FasterWhisperProvider(settings).transcribe(path)
    _, kwargs = FakeWhisperModel.instances[-1].transcribe_calls[0]
    assert "language" not in kwargs


def test_progress_reports_fraction_of_duration(provider, settings, monkeypatch, wav_factory):
    install_fake_faster_whisper(monkeypatch)
    path = wav_factory(settings.recordings_dir / "sample.wav")
    progress: list[float] = []
    provider.transcribe(
        path, progress_cb=lambda fraction, _message: progress.append(fraction)
    )
    # Segment ends 2.0 and 4.0 -> 0.4 and 0.8 of the 5 second duration.
    assert progress[0] == pytest.approx(0.05)
    assert progress[1] == pytest.approx(0.4)
    assert progress[2] == pytest.approx(0.8)


def test_model_is_reused_across_calls(provider, settings, monkeypatch, wav_factory):
    install_fake_faster_whisper(monkeypatch)
    path = wav_factory(settings.recordings_dir / "sample.wav")
    provider.transcribe(path)
    provider.transcribe(path)
    assert len(FakeWhisperModel.instances) == 1


def test_local_model_directory_skips_download_root(settings, monkeypatch, wav_factory, tmp_path):
    install_fake_faster_whisper(monkeypatch)
    local_model = tmp_path / "models" / "whisper-local"
    local_model.mkdir(parents=True)
    provider = FasterWhisperProvider(
        replace(settings, whisper_model_size=str(local_model))
    )
    path = wav_factory(settings.recordings_dir / "sample.wav")
    provider.transcribe(path)

    model = FakeWhisperModel.instances[-1]
    assert model.model_size_or_path == str(local_model)
    assert model.download_root is None


def test_failed_model_load_is_wrapped(settings, monkeypatch):
    module = install_fake_faster_whisper(monkeypatch)

    def failing_model(*args, **kwargs):
        raise RuntimeError("bad model")

    module.WhisperModel = failing_model
    provider = FasterWhisperProvider(settings)
    with pytest.raises(TranscriptionError, match="Could not load"):
        provider.transcribe(Path("nonexistent.wav"))


def test_model_name_reports_configured_model(settings, monkeypatch):
    install_fake_faster_whisper(monkeypatch)
    provider = FasterWhisperProvider(replace(settings, whisper_model_size="base"))
    assert provider.model_name == "base"
    assert provider.is_cloud is False


# ---------------------------------------------------------------------------
# Silent-track skipping (speed: never run Whisper on captured silence)
# ---------------------------------------------------------------------------


def test_silent_wav_is_skipped_without_loading_the_model(
    provider, settings, monkeypatch, wav_factory, caplog
):
    import logging

    install_fake_faster_whisper(monkeypatch)
    silent_path = wav_factory(
        settings.recordings_dir / "silent.wav", amplitude=0.0
    )

    with caplog.at_level(logging.INFO, logger="ai.faster_whisper_provider"):
        result = provider.transcribe(silent_path)

    assert isinstance(result, TranscriptionResult)
    assert result.text == ""
    assert result.segments == []
    assert result.language is None
    assert FakeWhisperModel.instances == [], "the model must never be loaded"
    assert any("silent" in record.message.lower() for record in caplog.records)


def test_quiet_but_audible_audio_is_still_transcribed(
    provider, settings, monkeypatch, wav_factory
):
    install_fake_faster_whisper(monkeypatch)
    quiet_path = wav_factory(
        settings.recordings_dir / "quiet.wav", amplitude=0.02
    )

    result = provider.transcribe(quiet_path)

    assert result.text == "Hello there.\nSecond line."
    assert len(FakeWhisperModel.instances) == 1


def test_unreadable_file_is_not_treated_as_silent(
    provider, settings, tmp_path, monkeypatch
):
    """Non-WAV content must fall through to the model, not be 'skipped'."""

    install_fake_faster_whisper(monkeypatch)
    bogus = tmp_path / "bogus.wav"
    bogus.write_bytes(b"not a wav file at all")

    provider.transcribe(bogus)
    assert FakeWhisperModel.instances, (
        "a file that cannot be inspected must not be skipped as silence"
    )
