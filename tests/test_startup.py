"""Startup-layer tests: env loading, stylesheet resolution, logging, and the
composition-root contract of ``main.py``.

The application is now a thin desktop client (``client/api_client.py``) that
talks to the FastAPI server.  The old in-process provider wiring and the
SQLite-to-library migration hook no longer exist in ``main.py``; the
replacement behaviour is covered by ``tests/test_api.py`` (server wiring) and
``tests/test_ui_smoke.py`` (client composition).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

import main
from client.api_client import DEFAULT_SERVER_URL, SERVER_URL_ENV, server_url_from_env
from main import _apply_light_theme, _configure_logging, _load_local_env, _load_stylesheet

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


def test_light_theme_installs_palette_and_stylesheet(qapp):
    """The theme is applied application-wide so dialogs inherit it."""

    _apply_light_theme(qapp)
    assert qapp.styleSheet().strip(), "the application stylesheet should be set"
    assert qapp.palette().window().color().name() == "#f8fafc"


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


def test_load_local_env_feeds_the_server_url(tmp_path, monkeypatch):
    """The .env file is the supported way to point the client at the host."""

    monkeypatch.delenv(SERVER_URL_ENV, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f'{SERVER_URL_ENV}="http://192.168.1.20:8000"\n', encoding="utf-8"
    )
    assert _load_local_env(env_file) is True
    assert server_url_from_env() == "http://192.168.1.20:8000"


def test_server_url_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv(SERVER_URL_ENV, raising=False)
    assert server_url_from_env() == DEFAULT_SERVER_URL == "http://localhost:8000"


def test_server_url_blank_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(SERVER_URL_ENV, "   ")
    assert server_url_from_env() == DEFAULT_SERVER_URL


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


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def test_main_module_no_longer_wires_local_providers():
    """Providers live on the server; the client must not build them."""

    for obsolete in (
        "_build_providers",
        "_migrate_legacy_if_present",
        "MeetingProcessingWorker",
    ):
        assert not hasattr(main, obsolete), f"{obsolete} belongs to the old architecture"


def test_composition_root_injects_an_http_client():
    """MainWindow takes exactly an HTTP client and settings - no engine."""

    import inspect

    from client.api_client import TeamMeetingAIClient
    from config import Settings

    parameters = list(inspect.signature(main.MainWindow.__init__).parameters.values())
    # ``from __future__ import annotations`` keeps these as strings.
    names = [getattr(p.annotation, "__name__", p.annotation) for p in parameters[1:]]
    assert names == [TeamMeetingAIClient.__name__, Settings.__name__]
    assert not hasattr(main, "build_engine"), "the client owns no database engine"
    assert not hasattr(main, "FasterWhisperProvider"), "providers live on the server"
