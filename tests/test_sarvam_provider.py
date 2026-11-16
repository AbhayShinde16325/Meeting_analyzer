"""Sarvam provider tests (mocked SDK; no network, no real API key)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai.sarvam_provider import (
    MODEL_NAME,
    SARVAM_KEY_ENV,
    SarvamConfigurationError,
    SarvamTranscriptionProvider,
)
from ai.transcription import TranscriptionCancelled, TranscriptionError
from config import Settings

_SAMPLE_PAYLOAD = {
    "request_id": "req-1",
    "transcript": "We will ship the release on Friday",
    "language_code": "en-IN",
    "diarized_transcript": {
        "entries": [
            {
                "transcript": "We will ship the release on Friday",
                "start_time_seconds": 0.0,
                "end_time_seconds": 2.5,
                "speaker_id": "0",
            }
        ]
    },
}


class FakeJob:
    """Mimics the documented sarvamai batch job handle."""

    def __init__(self, *, fail: str | None = None, payload: dict | None = None) -> None:
        self.calls: list[str] = []
        self._fail = fail
        self._payload = payload or _SAMPLE_PAYLOAD

    def upload_files(self, file_paths) -> None:
        self.calls.append(f"upload:{Path(file_paths[0]).name}")
        if self._fail == "upload":
            raise ConnectionError("network down")

    def start(self) -> None:
        self.calls.append("start")

    def get_status(self) -> dict:
        self.calls.append("status")
        return {"job_state": "Completed", "error_message": ""}

    def download_outputs(self, output_dir: str) -> None:
        self.calls.append("download")
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / "0.json").write_text(
            json.dumps(self._payload), encoding="utf-8"
        )


class FakeClient:
    def __init__(self, job: FakeJob) -> None:
        self._job = job
        self.calls: list[dict] = []

    @property
    def speech_to_text_job(self):
        return self

    def create_job(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._job, Exception):
            raise self._job
        return self._job


def make_settings(tmp_path, monkeypatch):
    monkeypatch.setenv(SARVAM_KEY_ENV, "test-key")
    return Settings(data_dir=tmp_path, recordings_dir=tmp_path / "recordings")


class TestConfiguration:
    def test_without_key_raises_configuration_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SARVAM_KEY_ENV, raising=False)
        settings = Settings(data_dir=tmp_path, recordings_dir=tmp_path)
        provider = SarvamTranscriptionProvider(settings)
        with pytest.raises(SarvamConfigurationError):
            provider.transcribe(tmp_path / "whatever.wav")

    def test_transliteration_mode_is_requested(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, monkeypatch)
        job = FakeJob()
        client = FakeClient(job)
        provider = SarvamTranscriptionProvider(settings, client=client, poll_interval=0.01)
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF....")
        provider.transcribe(audio)
        request = client.calls[0]
        assert request["model"] == MODEL_NAME
        assert request["mode"] == "translit"
        assert request["with_diarization"] is True


class TestSuccessPath:
    def test_full_workflow_and_result(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, monkeypatch)
        job = FakeJob()
        client = FakeClient(job)
        provider = SarvamTranscriptionProvider(settings, client=client, poll_interval=0.01)
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF....")
        progress: list[tuple[float, str]] = []
        result = provider.transcribe(audio, progress_cb=lambda p, m: progress.append((p, m)))

        assert job.calls[0].startswith("upload:audio.wav")
        assert "start" in job.calls
        assert "download" in job.calls
        assert result.text == "We will ship the release on Friday"
        assert result.language == "en-IN"
        assert result.segments[0]["text"] == "We will ship the release on Friday"
        assert result.segments[0]["speaker"] == "Speaker 0"
        assert any(p > 0 for p, _ in progress)

    def test_timestamps_fallback_without_diarization(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, monkeypatch)
        payload = {
            "transcript": "hello there",
            "language_code": None,
            "timestamps": {
                "chunks": ["hello there"],
                "start_time_seconds": [0.5],
                "end_time_seconds": [1.4],
            },
        }
        job = FakeJob(payload=payload)
        provider = SarvamTranscriptionProvider(
            settings, client=FakeClient(job), poll_interval=0.01
        )
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"x")
        result = provider.transcribe(audio)
        assert result.language is None
        assert result.segments == [{"start": 0.5, "end": 1.4, "text": "hello there"}]


class TestFailurePaths:
    def test_upload_failure_becomes_transcription_error(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, monkeypatch)
        job = FakeJob(fail="upload")
        provider = SarvamTranscriptionProvider(
            settings, client=FakeClient(job), poll_interval=0.01
        )
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"x")
        with pytest.raises(TranscriptionError):
            provider.transcribe(audio)

    def test_failed_job_becomes_transcription_error(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, monkeypatch)
        job = FakeJob()
        job.get_status = lambda: {"job_state": "Failed", "error_message": "bad audio"}
        provider = SarvamTranscriptionProvider(
            settings, client=FakeClient(job), poll_interval=0.01
        )
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"x")
        with pytest.raises(TranscriptionError, match="failed job"):
            provider.transcribe(audio)

    def test_empty_transcript_is_an_error(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, monkeypatch)
        payload = {"transcript": "", "language_code": None}
        provider = SarvamTranscriptionProvider(
            settings, client=FakeClient(FakeJob(payload=payload)), poll_interval=0.01
        )
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"x")
        with pytest.raises(TranscriptionError, match="empty"):
            provider.transcribe(audio)

    def test_timeout_becomes_transcription_error(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, monkeypatch)
        job = FakeJob()
        job.get_status = lambda: {"job_state": "Running", "error_message": ""}
        provider = SarvamTranscriptionProvider(
            settings, client=FakeClient(job), poll_interval=0.01, timeout=0.05
        )
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"x")
        with pytest.raises(TranscriptionError, match="did not finish"):
            provider.transcribe(audio)

    def test_cancellation_aborts_without_fallback(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, monkeypatch)
        job = FakeJob()
        client = FakeClient(job)
        provider = SarvamTranscriptionProvider(settings, client=client, poll_interval=0.01)
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"x")
        with pytest.raises(TranscriptionCancelled):
            provider.transcribe(audio, cancel_cb=lambda: True)
        # Upload/start never happened: cancelled before job creation.
        assert client.calls == []
