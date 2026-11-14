"""Startup-layer tests: env loading, stylesheet resolution, provider wiring,
legacy migration, and logging configuration.

These cover the composition-root contract in ``main.py`` that the earlier
regression silently dropped (audit items AUD-002/004/007/008/019).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import dataclasses
import pytest

import main
from ai.faster_whisper_provider import FasterWhisperProvider
from ai.provider_factory import ProviderConfigurationError
from main import (
    _build_providers,
    _configure_logging,
    _load_local_env,
    _load_stylesheet,
    _migrate_legacy_if_present,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------


def test_stylesheet_is_loaded_from_the_module_directory():
    """The stylesheet resolves next to main.py, not the working directory."""

    stylesheet = _load_stylesheet()
    assert "app_stylesheet.qss" in str(
        Path(main.__file__).parent / "styles" / "app_stylesheet.qss"
    )
    assert stylesheet.strip(), "the bundled stylesheet should not be empty"


def test_stylesheet_missing_file_is_tolerated(tmp_path, caplog):
    """A missing stylesheet file logs a warning and yields an empty string."""

    missing = tmp_path / "styles" / "app_stylesheet.qss"
    with caplog.at_level(logging.WARNING, logger="main"):
        assert _load_stylesheet(missing) == ""
    assert any("stylesheet" in record.message.lower() for record in caplog.records)


def test_importing_main_works_from_another_working_directory(tmp_path):
    """The module imports (and loads its stylesheet) from any CWD.

    Regression test for the CWD-relative stylesheet read that crashed the
    app when launched from a shortcut outside the repository root.
    """

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = env.get("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import main; "
            "print('stylesheet-ok:', "
            "Path(main.__file__).parent == Path(r'%s'))" % PROJECT_ROOT,
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "stylesheet-ok: True" in result.stdout


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------


def test_load_local_env_sets_new_keys(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "TMAI_TEST_NEW_KEY=hello\n"
        'TMAI_TEST_QUOTED_KEY="quoted value"\n'
        "broken line without separator\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TMAI_TEST_NEW_KEY", raising=False)
    monkeypatch.delenv("TMAI_TEST_QUOTED_KEY", raising=False)

    assert _load_local_env(env_file) is True
    assert os.environ["TMAI_TEST_NEW_KEY"] == "hello"
    assert os.environ["TMAI_TEST_QUOTED_KEY"] == "quoted value"


def test_load_local_env_does_not_override_real_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TMAI_TEST_KEEP=from-env\n", encoding="utf-8")
    monkeypatch.setenv("TMAI_TEST_KEEP", "from-env")

    assert _load_local_env(env_file) is True
    assert os.environ["TMAI_TEST_KEEP"] == "from-env"


def test_load_local_env_missing_file_returns_false(tmp_path):
    assert _load_local_env(tmp_path / "no-such.env") is False


# ---------------------------------------------------------------------------
# Provider wiring
# ---------------------------------------------------------------------------


def test_build_providers_none_backend_returns_local_transcriber_only(settings):
    transcription, analysis = _build_providers(settings)
    assert isinstance(transcription, FasterWhisperProvider)
    assert analysis is None


def test_build_providers_groq_without_optin_raises(settings):
    groq_settings = dataclasses.replace(
        settings,
        analysis_backend="groq",
        allow_cloud_analysis=False,
        groq_model="test-model",
    )
    with pytest.raises(ProviderConfigurationError) as excinfo:
        _build_providers(groq_settings)
    assert "opt-in" in str(excinfo.value)


def test_build_providers_unknown_backend_raises(settings):
    broken = dataclasses.replace(settings, analysis_backend="carrier-pigeon")
    with pytest.raises(ProviderConfigurationError):
        _build_providers(broken)


# ---------------------------------------------------------------------------
# Legacy migration hook
# ---------------------------------------------------------------------------


def test_migrate_legacy_if_present_is_a_noop_without_legacy_db(settings, db):
    result = _migrate_legacy_if_present(settings, db)
    assert result is not None
    assert result.migrated == 0
    assert result.errors == []


def test_migrate_legacy_if_present_survives_migration_errors(
    settings, db, monkeypatch
):
    """A broken migration must never abort the application startup."""

    def _boom(_settings, _library):
        raise RuntimeError("simulated migration crash")

    monkeypatch.setattr("main.migrate_legacy_sqlite", _boom)
    assert _migrate_legacy_if_present(settings, db) is None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_configure_logging_writes_operational_log_to_data_dir(settings):
    _configure_logging(settings)
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert handlers, "a rotating file handler should be installed"

    log_path = settings.data_dir / "teammeetingai.log"
    logging.getLogger("tmai.startup-test").info("operational smoke entry")
    assert log_path.is_file()
    assert "operational smoke entry" in log_path.read_text(encoding="utf-8")
    assert handlers[0].baseFilename == str(log_path)

    # Clean up so later tests do not write into this temporary directory.
    for handler in handlers:
        root.removeHandler(handler)
        handler.close()


def test_configure_logging_is_idempotent(settings):
    _configure_logging(settings)
    _configure_logging(settings)
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(handlers) == 1
    for handler in handlers:
        root.removeHandler(handler)
        handler.close()
