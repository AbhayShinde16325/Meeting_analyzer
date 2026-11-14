"""Library settings dialog and in-app library switching tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QMessageBox

from storage import library as ops
from storage.library import count_library_meetings
from support import valid_analysis
from ui.settings import LibrarySettingsDialog


# ---------------------------------------------------------------------------
# count_library_meetings helper
# ---------------------------------------------------------------------------


def test_count_library_meetings_counts_only_meeting_folders(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    assert count_library_meetings(library) == 0

    meeting = library / "2026-01-01 10-00 Sync [abcd1234]"
    meeting.mkdir()
    (meeting / "meeting.json").write_text("{}", encoding="utf-8")
    (library / "plain-folder").mkdir()
    stray = library / "loose.json"
    stray.write_text("{}", encoding="utf-8")

    assert count_library_meetings(library) == 1
    assert count_library_meetings(library / "missing-dir") == 0


# ---------------------------------------------------------------------------
# LibrarySettingsDialog
# ---------------------------------------------------------------------------


def test_dialog_previews_candidate_folder(qapp, db, user, recorded_meeting, tmp_path):
    dialog = LibrarySettingsDialog(db.library_dir)

    # The prefilled current library reports itself as current.
    assert "current" in dialog.info_label.text().lower()

    empty = tmp_path / "brand-new-library"
    dialog.path_edit.setText(str(empty))
    assert "new library" in dialog.info_label.text().lower()

    # A folder holding one meeting shows the count.
    other = tmp_path / "other-library"
    other.mkdir()
    meeting = other / "meeting-folder"
    meeting.mkdir()
    (meeting / "meeting.json").write_text("{}", encoding="utf-8")
    dialog.path_edit.setText(str(other))
    assert "1 meeting" in dialog.info_label.text().lower()

    dialog.path_edit.setText(str(db.library_dir))
    dialog._accept()
    # The current library is rejected as a "change".
    assert dialog._chosen is None
    assert "already" in dialog.error_label.text().lower()


def test_dialog_accepts_validated_new_folder(qapp, db, tmp_path):
    dialog = LibrarySettingsDialog(db.library_dir)
    new_dir = tmp_path / "chosen-library"
    dialog.path_edit.setText(str(new_dir))
    dialog._accept()
    assert dialog._chosen == new_dir.resolve()


def test_dialog_rejects_unwritable_folder(qapp, tmp_path, db):
    blocker = tmp_path / "occupied.txt"
    blocker.write_text("x", encoding="utf-8")
    dialog = LibrarySettingsDialog(db.library_dir)
    dialog.path_edit.setText(str(blocker))
    dialog._accept()
    assert dialog._chosen is None
    assert dialog.error_label.text()


def test_dialog_open_folder_uses_desktop_service(qapp, db, monkeypatch):
    opened: list[QUrl] = []

    class _FakeDesktopServices:
        @staticmethod
        def openUrl(url: QUrl) -> None:
            opened.append(url)

    monkeypatch.setattr("ui.settings.QDesktopServices", _FakeDesktopServices)
    dialog = LibrarySettingsDialog(db.library_dir)
    dialog._open_current()
    assert len(opened) == 1
    assert Path(opened[0].toLocalFile()) == db.library_dir


# ---------------------------------------------------------------------------
# In-app library switching
# ---------------------------------------------------------------------------


def _make_second_library(settings) -> Path:
    new_dir = Path(settings.data_dir) / "second-library"
    new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


def _library_with_profile(settings, new_dir: Path, display_name: str):
    """Build a MeetingLibrary for *new_dir* containing one named profile."""

    import dataclasses

    from storage.library import MeetingLibrary

    second = MeetingLibrary(dataclasses.replace(settings, library_dir=new_dir))
    ops.get_or_create_user(second, display_name)
    return second


def _read_config(settings) -> dict:
    path = Path(settings.data_dir) / "config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_switch_updates_config_and_all_screens(
    window_with_analysis, qapp, db, settings, user, monkeypatch
):
    window = window_with_analysis
    window._on_profile_selected(user)

    # The same-named profile exists in the new library, so the session is kept.
    new_dir = _make_second_library(settings)
    _library_with_profile(settings, new_dir, user["display_name"])

    assert window._apply_library_change(new_dir) is True

    assert window.library.library_dir == new_dir.resolve()
    assert window.settings.library_dir == new_dir.resolve()
    for screen in (
        window.login_screen,
        window.dashboard_screen,
        window.recording_screen,
        window.details_screen,
    ):
        assert screen.library is window.library
    assert window.recording_screen.settings is window.settings

    config = _read_config(settings)
    assert Path(config["library_dir"]) == new_dir.resolve()

    # Logged in, on the dashboard, showing the (empty) new library.
    assert window.current_user is not None
    assert window.stack.currentWidget() is window.dashboard_screen
    assert window.dashboard_screen.table.rowCount() == 0


def test_switch_to_library_without_profile_logs_out(
    window_with_analysis, qapp, db, settings, user
):
    window = window_with_analysis
    window._on_profile_selected(user)

    new_dir = _make_second_library(settings)
    assert window._apply_library_change(new_dir) is True

    assert window.current_user is None
    assert window.stack.currentWidget() is window.login_screen
    assert window.login_screen.profile_combo.count() == 1  # placeholder only


def test_switch_blocked_while_recording(
    window_with_analysis, qapp, db, settings, user, monkeypatch
):
    window = window_with_analysis
    window._on_profile_selected(user)
    window.recording_screen._state = "recording"

    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(
            lambda _parent, title, _text, *_a, **_k: warnings.append(title)
            or QMessageBox.StandardButton.Ok
        ),
    )

    new_dir = _make_second_library(settings)
    assert window._apply_library_change(new_dir) is False
    assert warnings == ["Cannot change the library"]
    assert window.library is db
    assert "library_dir" not in _read_config(settings)


def test_switch_blocked_while_processing(
    window_with_analysis, qapp, db, settings, user
):
    window = window_with_analysis
    window._on_profile_selected(user)

    class _FakeWorker:
        def isRunning(self) -> bool:
            return True

    window._worker = _FakeWorker()
    new_dir = _make_second_library(settings)

    assert window._apply_library_change(new_dir) is False
    assert window.library is db
    assert window.current_user == user


def test_switch_to_unusable_folder_keeps_old_library(
    window_with_analysis, qapp, db, settings, user, monkeypatch
):
    window = window_with_analysis
    window._on_profile_selected(user)

    blocker = Path(settings.data_dir) / "occupied.txt"
    blocker.write_text("x", encoding="utf-8")
    errors: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(
            lambda *_a, **_k: errors.append("critical")
            or QMessageBox.StandardButton.Ok
        ),
    )

    assert window._apply_library_change(blocker) is False
    assert errors
    assert window.library is db
    assert window.current_user == user


def test_switch_clears_open_meeting_in_details(
    window_with_analysis, qapp, db, settings, user, processed_meeting
):
    window = window_with_analysis
    window._on_profile_selected(user)
    window._open_meeting(processed_meeting["id"])
    qapp.processEvents()
    assert window.details_screen._details is not None

    # The new library has a profile with the same name, so we stay logged in;
    # the details screen must not keep showing the old library's meeting.
    new_dir = _make_second_library(settings)
    _library_with_profile(settings, new_dir, user["display_name"])
    assert window._apply_library_change(new_dir) is True

    assert window.details_screen._details is None
    assert window.details_screen._meeting_id is None
    assert not window.details_screen.open_folder_button.isEnabled()


def test_details_open_folder_opens_meeting_dir(
    window, qapp, user, processed_meeting, monkeypatch
):
    opened: list[QUrl] = []

    class _FakeDesktopServices:
        @staticmethod
        def openUrl(url: QUrl) -> None:
            opened.append(url)

    monkeypatch.setattr(
        "ui.meeting_details.QDesktopServices", _FakeDesktopServices
    )
    window._on_profile_selected(user)
    window._open_meeting(processed_meeting["id"])
    qapp.processEvents()

    screen = window.details_screen
    assert screen.open_folder_button.isEnabled()
    screen._open_meeting_folder()
    assert len(opened) == 1
    assert (
        Path(opened[0].toLocalFile()) == Path(processed_meeting["meeting_dir"])
    )


def test_details_open_folder_missing_dir_shows_notice(
    window, qapp, user, processed_meeting, monkeypatch
):
    opened: list[QUrl] = []

    class _FakeDesktopServices:
        @staticmethod
        def openUrl(url: QUrl) -> None:
            opened.append(url)

    monkeypatch.setattr(
        "ui.meeting_details.QDesktopServices", _FakeDesktopServices
    )
    window._on_profile_selected(user)
    window._open_meeting(processed_meeting["id"])
    qapp.processEvents()

    screen = window.details_screen
    import shutil

    shutil.rmtree(processed_meeting["meeting_dir"])

    # The loaded details still point at the deleted folder; opening it must
    # show a notice and disable the action instead of launching Explorer.
    screen._open_meeting_folder()
    assert opened == []
    assert not screen.open_folder_button.isEnabled()
    assert "could not be found" in screen.notice_label.text().lower()
