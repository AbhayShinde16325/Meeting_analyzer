"""Pytest configuration and shared fixtures.

The whole suite runs against a temporary application data directory, a
temporary meeting library directory, and an offscreen Qt platform so no real
meetings, recordings, or windows are touched.

The ``db`` fixture intentionally keeps its historical name (a lot of tests use
it) but now returns a file-backed :class:`~storage.library.MeetingLibrary`
instead of the old SQLAlchemy database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
for path in (str(PROJECT_ROOT), str(TESTS_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest  # noqa: E402

from config import ensure_directories, load_settings  # noqa: E402
from storage import library as ops  # noqa: E402
from storage.library import MeetingLibrary  # noqa: E402
from support import valid_analysis, write_pcm_wav  # noqa: E402

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_CONFIG_ENV_VARS = (
    "TEAMMEETINGAI_DATA_DIR",
    "TEAMMEETINGAI_LIBRARY_DIR",
    "TEAMMEETINGAI_SAMPLE_RATE",
    "TEAMMEETINGAI_WHISPER_MODEL",
    "TEAMMEETINGAI_WHISPER_DEVICE",
    "TEAMMEETINGAI_WHISPER_COMPUTE_TYPE",
    "TEAMMEETINGAI_WHISPER_LANGUAGE",
    "TEAMMEETINGAI_WHISPER_BEAM_SIZE",
    "TEAMMEETINGAI_WHISPER_CPU_THREADS",
    "TEAMMEETINGAI_ANALYSIS_BACKEND",
    "TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS",
    "TEAMMEETINGAI_GROQ_MODEL",
    "TEAMMEETINGAI_GROQ_TIMEOUT",
    "TEAMMEETINGAI_GROQ_API_KEY",
    "TEAMMEETINGAI_OLLAMA_HOST",
    "TEAMMEETINGAI_OLLAMA_MODEL",
    "TEAMMEETINGAI_LLM_TEMPERATURE",
    "TEAMMEETINGAI_LLM_MAX_TRANSCRIPT_CHARS",
)


@pytest.fixture(autouse=True)
def _dispose_top_level_widgets(request):
    """Delete top-level Qt widgets after each test, while the app is alive.

    Without this, some widgets are garbage-collected during interpreter
    shutdown, after QApplication has been destroyed, which crashes the
    process at exit (access violation) even though every test passed.
    This only disposes widgets; it changes no test state or assertions.
    """

    yield
    app = QApplication.instance()
    if app is None:
        return
    import gc

    gc.collect()
    for widget in app.topLevelWidgets():
        if widget.isVisible():
            widget.close()
        widget.deleteLater()
    app.processEvents()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TEAMMEETINGAI_LIBRARY_DIR", str(data_dir / "library"))
    for name in _CONFIG_ENV_VARS:
        if name not in ("TEAMMEETINGAI_DATA_DIR", "TEAMMEETINGAI_LIBRARY_DIR"):
            monkeypatch.delenv(name, raising=False)
    loaded = load_settings()
    ensure_directories(loaded)
    return loaded


@pytest.fixture
def db(settings):
    """A file-backed meeting library inside the temporary data directory."""

    return MeetingLibrary(settings)


@pytest.fixture
def user(db):
    return ops.get_or_create_user(db, "Test User")


@pytest.fixture
def wav_factory():
    return write_pcm_wav


@pytest.fixture
def recorded_meeting(db, user, wav_factory):
    meeting = ops.create_meeting(db, user["id"], "Recorded test meeting")
    meeting_dir = Path(meeting["meeting_dir"])
    audio_path = meeting_dir / "audio.wav"
    wav_factory(audio_path, seconds=0.25)
    ops.finish_recording(
        db,
        meeting["id"],
        relative_path="audio.wav",
        format="wav",
        duration_seconds=0.25,
        size_bytes=audio_path.stat().st_size,
        sample_rate=48000,
        channels=2,
        mic_relative_path="mic.wav",
        system_relative_path="system.wav",
    )
    return meeting


@pytest.fixture
def processed_meeting(db, recorded_meeting):
    ops.save_transcript(
        db,
        recorded_meeting["id"],
        text="Alice: We agreed to ship the release on Friday.",
        segments=[
            {
                "start": 0.0,
                "end": 2.5,
                "text": "Alice: We agreed to ship the release on Friday.",
            }
        ],
        language="en",
    )
    ops.save_analysis(db, recorded_meeting["id"], valid_analysis(), model="fake-model")
    return recorded_meeting


@pytest.fixture
def window(qapp, db, settings):
    from main import MainWindow

    main_window = MainWindow(db, settings)
    try:
        yield main_window
    finally:
        main_window.recording_screen.shutdown()
        main_window.details_screen.shutdown()
        main_window.deleteLater()


@pytest.fixture
def window_with_analysis(qapp, db, settings):
    """A window wired to fake transcription and analysis providers."""

    from main import MainWindow
    from support import FakeAnalysisProvider, FakeTranscriptionProvider

    main_window = MainWindow(
        db,
        settings,
        transcription_provider=FakeTranscriptionProvider(),
        analysis_provider=FakeAnalysisProvider([valid_analysis()]),
    )
    try:
        yield main_window
    finally:
        main_window.recording_screen.shutdown()
        main_window.details_screen.shutdown()
        main_window.deleteLater()
