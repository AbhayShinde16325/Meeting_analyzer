"""Pytest configuration and shared fixtures.

The suite runs against a temporary application data directory and an offscreen
Qt platform so no real meetings, recordings, or windows are touched.

Two backend tiers exist:

* PostgreSQL tests (schema, Large Objects, API, UI end to end) are opt-in via
  ``TEST_DATABASE_URL`` and run against a fresh, migrated scratch database.
* Pure unit tests (recorder, providers, config) need no database.

UI tests drive the real PySide6 windows against a live uvicorn server
(``tests/ui_server.py``) whose providers and background runner are faked.
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

# Load TEST_DATABASE_URL from the project .env once per pytest session, so
# the PostgreSQL tier runs without per-shell environment setup.  A
# TEST_DATABASE_URL already present in the environment always wins, and the
# value is never printed.
if not os.environ.get("TEST_DATABASE_URL"):
    _env_file = PROJECT_ROOT / ".env"
    if _env_file.is_file():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            if _line.startswith("DATABASE_URL="):
                os.environ["TEST_DATABASE_URL"] = _line.split("=", 1)[1].strip()
                break

import pytest  # noqa: E402

from config import ensure_directories, load_settings  # noqa: E402

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


# ---------------------------------------------------------------------------
# PostgreSQL fixtures (opt-in; skipped unless TEST_DATABASE_URL is set)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_engine():
    """An engine on a fresh, fully migrated scratch PostgreSQL database."""

    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL not set; PostgreSQL tests skipped")
    from pg_harness import create_scratch_database, drop_scratch_database, migrate_to_head

    url = create_scratch_database()
    migrate_to_head(url)
    from sqlalchemy import create_engine

    engine = create_engine(url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()
        drop_scratch_database(url)


@pytest.fixture
def pg(pg_engine):
    """A clean database connection: all tables truncated before each test."""

    from pg_harness import truncate_all

    truncate_all(pg_engine)
    with pg_engine.connect() as conn:
        yield conn


_CONFIG_ENV_VARS = (
    "TEAMMEETINGAI_DATA_DIR",
    "TEAMMEETINGAI_LIBRARY_DIR",
    "TEAMMEETINGAI_SERVER_URL",
    "TEAMMEETINGAI_HOST_TOKEN",
    "TEAMMEETINGAI_SAMPLE_RATE",
    "TEAMMEETINGAI_WHISPER_MODEL",
    "TEAMMEETINGAI_WHISPER_DEVICE",
    "TEAMMEETINGAI_WHISPER_COMPUTE_TYPE",
    "TEAMMEETINGAI_WHISPER_LANGUAGE",
    "TEAMMEETINGAI_WHISPER_INITIAL_PROMPT",
    "TEAMMEETINGAI_WHISPER_BEAM_SIZE",
    "TEAMMEETINGAI_WHISPER_VAD_MIN_SILENCE_MS",
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
    # Deliver still-queued worker signals while the widgets are alive; a
    # completion slot running after deletion would touch dead C++ objects and
    # crash the interpreter instead of failing the test.
    app.processEvents()

    import gc

    gc.collect()
    for widget in app.topLevelWidgets():
        if widget.isVisible():
            widget.close()
        widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()


@pytest.fixture
def wav_factory():
    """Write small valid 16-bit PCM WAV files into a test-owned directory."""

    from support import write_pcm_wav

    def _make(path, **kwargs):
        return write_pcm_wav(Path(path), **kwargs)

    return _make


@pytest.fixture
def settings(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for name in _CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(data_dir))
    loaded = load_settings()
    ensure_directories(loaded)
    return loaded


# ---------------------------------------------------------------------------
# Live server + HTTP client + windows (PostgreSQL required)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ui_server(pg_engine):
    """A real uvicorn server on a local port, wired to the scratch database."""

    from ui_server import start_ui_server

    server = start_ui_server(str(pg_engine.url))
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def _clean_db(pg_engine):
    """Truncate every table before each server-backed test."""

    from pg_harness import truncate_all

    truncate_all(pg_engine)


@pytest.fixture
def client(_clean_db, ui_server):
    from client.api_client import TeamMeetingAIClient

    return TeamMeetingAIClient(base_url=ui_server.base_url)


@pytest.fixture
def server(ui_server):
    """The running server handle, so tests can drive jobs synchronously."""

    return ui_server


@pytest.fixture
def user(client):
    """A profile on the server, plus a second known profile for searches."""

    return client.get_or_create_user("Test User")


@pytest.fixture
def window(qapp, client, settings, user):
    """The composed MainWindow against the live test server.

    ``user`` is requested before the window is built so the login screen's
    initial profile fetch sees a populated server (a deployed server always
    has at least one profile).
    """

    from main import MainWindow

    main_window = MainWindow(client, settings)
    try:
        yield main_window
    finally:
        # Same shutdown path as closing the window: stops every screen worker
        # and detaches the startup server check before the C++ objects die.
        main_window.shutdown()
        # Drain worker completions while the widgets still exist, then delete.
        qapp.processEvents()
        main_window.deleteLater()
