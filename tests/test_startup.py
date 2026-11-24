"""Startup and environment tests for backend-first launcher."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import main
from main import _configure_logging, _frontend_hint, _load_local_env


def test_load_local_env_sets_new_keys(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TMAI_TEST_NEW_KEY=hello\nTMAI_TEST_QUOTED=\"quoted\"\n", encoding="utf-8")
    monkeypatch.delenv("TMAI_TEST_NEW_KEY", raising=False)
    monkeypatch.delenv("TMAI_TEST_QUOTED", raising=False)
    assert _load_local_env(env_file) is True
    assert os.environ["TMAI_TEST_NEW_KEY"] == "hello"
    assert os.environ["TMAI_TEST_QUOTED"] == "quoted"


def test_load_local_env_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TMAI_TEST_KEEP=from-file\n", encoding="utf-8")
    monkeypatch.setenv("TMAI_TEST_KEEP", "from-env")
    assert _load_local_env(env_file) is True
    assert os.environ["TMAI_TEST_KEEP"] == "from-env"


def test_configure_logging_writes_operational_log(settings):
    _configure_logging(settings)
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert handlers
    log_path = settings.data_dir / "teammeetingai.log"
    logging.getLogger("tmai.startup-test").info("operational smoke entry")
    assert log_path.is_file()
    for handler in handlers:
        root.removeHandler(handler)
        handler.close()


def test_main_has_no_legacy_ui_symbols():
    for obsolete in ("MainWindow", "_apply_light_theme", "_load_stylesheet"):
        assert not hasattr(main, obsolete)


def test_frontend_hint_mentions_client():
    hint = _frontend_hint()
    assert "client" in hint.lower()
