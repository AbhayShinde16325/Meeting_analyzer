"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
for path in (str(PROJECT_ROOT), str(TESTS_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

# Load TEST_DATABASE_URL from the project .env once per pytest session.
if not os.environ.get("TEST_DATABASE_URL"):
    _env_file = PROJECT_ROOT / ".env"
    if _env_file.is_file():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            if _line.startswith("DATABASE_URL=") or _line.startswith("TEST_DATABASE_URL="):
                key, _, value = _line.partition("=")
                if key.strip() == "TEST_DATABASE_URL" or not os.environ.get("TEST_DATABASE_URL"):
                    os.environ["TEST_DATABASE_URL"] = value.strip().strip('"').strip("'")
                if key.strip() == "TEST_DATABASE_URL":
                    break

import pytest  # noqa: E402

from config import ensure_directories, load_settings  # noqa: E402

_CONFIG_ENV_VARS = (
    "TEAMMEETINGAI_DATA_DIR",
    "TEAMMEETINGAI_LIBRARY_DIR",
    "TEAMMEETINGAI_SERVER_URL",
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


@pytest.fixture(scope="session")
def pg_engine():
    """An engine on a fresh, fully migrated scratch PostgreSQL database."""
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL not set; PostgreSQL tests skipped")
    from pg_harness import create_scratch_database, drop_scratch_database, migrate_to_head
    from sqlalchemy import create_engine

    url = create_scratch_database()
    migrate_to_head(url)
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
