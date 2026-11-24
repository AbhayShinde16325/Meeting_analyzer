"""Tests for the project-local .env loader used at startup."""

from __future__ import annotations

import os

import main


def test_env_file_sets_missing_variables(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        "TEAMMEETINGAI_TEST_MISSING=hello\n"
        "TEAMMEETINGAI_TEST_SPACED = padded \n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    try:
        assert main._load_local_env() is True
        assert os.environ["TEAMMEETINGAI_TEST_MISSING"] == "hello"
        assert os.environ["TEAMMEETINGAI_TEST_SPACED"] == "padded"
    finally:
        os.environ.pop("TEAMMEETINGAI_TEST_MISSING", None)
        os.environ.pop("TEAMMEETINGAI_TEST_SPACED", None)


def test_existing_environment_variables_are_not_overridden(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TEAMMEETINGAI_TEST_KEEP=from-file\n", encoding="utf-8"
    )
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setenv("TEAMMEETINGAI_TEST_KEEP", "from-environment")
    try:
        assert main._load_local_env() is True
        assert os.environ["TEAMMEETINGAI_TEST_KEEP"] == "from-environment"
    finally:
        os.environ.pop("TEAMMEETINGAI_TEST_KEEP", None)


def test_missing_env_file_returns_false_without_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    assert main._load_local_env() is False


def test_env_loader_anchors_to_source_file():
    """The real project .env is found next to main.py, not via the CWD."""

    assert (main.__file__ is not None)
    env_path = main.Path(main.__file__).resolve().parent / ".env"
    assert env_path.name == ".env"
