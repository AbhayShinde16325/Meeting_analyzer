"""Shared test helpers: synthetic WAVs, fake AI providers, and UI pump."""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from ai.transcription import TranscriptionCancelled, TranscriptionResult


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.02,
) -> bool:
    """Pump the Qt event loop until ``predicate`` is true (UI worker joins).

    QThread workers deliver their ``completed`` signals through queued
    connections, so tests must let the event loop run between thread
    completion and slot execution.  Returns False on timeout.
    """

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(interval)
    return False


def write_pcm_wav(
    path: Path,
    *,
    seconds: float = 0.2,
    channels: int = 2,
    sample_rate: int = 48_000,
    amplitude: float = 0.1,
    frequency: float = 440.0,
) -> Path:
    """Write a small valid 16-bit PCM WAV file for filesystem/UI tests."""

    frames = max(1, int(seconds * sample_rate))
    t = np.arange(frames, dtype=np.float32) / float(sample_rate)
    tone = (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)
    samples = np.repeat(tone.reshape(-1, 1), channels, axis=1)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as destination:
        destination.setnchannels(channels)
        destination.setsampwidth(2)
        destination.setframerate(sample_rate)
        destination.writeframes(pcm.tobytes())
    return path


class FakeTranscriptionProvider:
    """Provider stub that never touches a real speech engine.

    Accepts (and records) an optional ``cancel_cb`` so tests can verify the
    cancellation wiring, and can raise ``TranscriptionCancelled`` on demand.
    """

    def __init__(
        self,
        result: TranscriptionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or TranscriptionResult(
            text="Alice: We agreed to ship the release on Friday.",
            language="en",
            segments=[
                {
                    "start": 0.0,
                    "end": 2.5,
                    "text": "Alice: We agreed to ship the release on Friday.",
                }
            ],
        )
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.cancelled = False

    def transcribe(
        self,
        audio_path: Path,
        *,
        language=None,
        progress_cb=None,
        cancel_cb=None,
    ):
        self.calls.append(
            {
                "audio_path": Path(audio_path),
                "language": language,
                "cancel_cb": cancel_cb,
            }
        )
        if progress_cb is not None:
            progress_cb(0.5, "fake transcription progress")
        if self.error is not None:
            raise self.error
        if cancel_cb is not None and cancel_cb():
            self.cancelled = True
            raise TranscriptionCancelled("Transcription was cancelled.")
        return self.result


class BlockingFakeTranscriptionProvider:
    """Provider stub that blocks until released or cancelled.

    The provider polls ``cancel_cb`` while blocked, so a worker cancellation
    makes the pending transcription raise :class:`TranscriptionCancelled`
    without any real engine or timing dependence beyond short polls.
    """

    def __init__(self, result: TranscriptionResult | None = None) -> None:
        self.result = result or TranscriptionResult(
            text="Blocking provider transcript.",
            language="en",
            segments=[{"start": 0.0, "end": 1.0, "text": "Blocking provider transcript."}],
        )
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[dict[str, Any]] = []
        self.observed_cancel_cb = None

    def transcribe(
        self,
        audio_path: Path,
        *,
        language=None,
        progress_cb=None,
        cancel_cb=None,
    ):
        self.calls.append(
            {
                "audio_path": Path(audio_path),
                "language": language,
                "cancel_cb": cancel_cb,
            }
        )
        self.observed_cancel_cb = cancel_cb
        self.started.set()
        while True:
            if cancel_cb is not None and cancel_cb():
                raise TranscriptionCancelled("Transcription was cancelled.")
            if self.release.wait(timeout=0.01):
                return self.result


class FakeAnalysisProvider:
    """Provider stub that returns preconfigured responses in order."""

    def __init__(self, responses: list[str | Mapping[str, Any] | Exception]) -> None:
        if not responses:
            raise ValueError("At least one fake response is required.")
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def analyze(self, *, system_prompt: str, user_prompt: str):
        self.calls.append(
            {"system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def valid_analysis(
    *,
    overview: str = "The team agreed to ship the release.",
    task: str = "Ship the release",
    owner: str | None = None,
    deadline: str | None = "Friday",
) -> dict[str, Any]:
    return {
        "overview": overview,
        "key_points": ["Release is ready"],
        "decisions": ["Ship on Friday"],
        "action_items": [
            {
                "task": task,
                "owner": owner,
                "deadline": deadline,
                "evidence": "We agreed to ship the release on Friday.",
            }
        ],
        "open_questions": [],
        "risks_or_blockers": [],
        "follow_ups": ["Confirm deployment window"],
    }
