"""Offscreen UI smoke and end-to-end widget tests."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from storage import library as ops
from support import valid_analysis


def test_starts_on_login_and_creates_local_profile(window, qapp):
    assert window.stack.currentWidget() is window.login_screen
    window.login_screen.new_profile_edit.setText("UI Test User")
    window.login_screen._continue()
    qapp.processEvents()

    assert window.stack.currentWidget() is window.dashboard_screen
    assert window.current_user is not None
    assert window.dashboard_screen.user_label.text().endswith("UI Test User")


def test_dashboard_lists_and_searches_meetings(
    window, qapp, user, processed_meeting
):
    window._on_profile_selected(user)
    window.dashboard_screen.refresh()
    qapp.processEvents()

    assert window.dashboard_screen.table.rowCount() == 1
    assert window.dashboard_screen.table.item(0, 1).text() == "Recorded test meeting"

    window.dashboard_screen.search_edit.setText("release")
    window.dashboard_screen.refresh()
    assert window.dashboard_screen.table.rowCount() == 1

    window.dashboard_screen.search_edit.setText("no-such-meeting")
    window.dashboard_screen.refresh()
    assert window.dashboard_screen.table.rowCount() == 0


def test_details_renders_processed_meeting(
    window_with_analysis, qapp, user, processed_meeting
):
    window_with_analysis._on_profile_selected(user)
    window_with_analysis._open_meeting(processed_meeting["id"])
    qapp.processEvents()

    screen = window_with_analysis.details_screen
    assert screen.title_label.text() == "Recorded test meeting"
    assert "Complete" in screen.meta_label.text()
    assert "The team agreed" in screen.overview_browser.toPlainText()
    assert "Alice" in screen.transcript_browser.toPlainText()
    assert screen.actions_table.rowCount() == 1
    assert screen.transcribe_button.isEnabled()
    assert screen.transcribe_button.text() == "Re-transcribe"
    assert screen.analyse_button.isEnabled()
    assert screen.analyse_button.text() == "Re-run analysis"
    if screen._player is not None:
        assert screen.play_button.isEnabled()


def test_details_buttons_for_fresh_recording(
    window, qapp, db, settings, user, wav_factory
):
    meeting = ops.create_meeting(db, user["id"], "Fresh recording")
    path = Path(meeting["meeting_dir"]) / "audio.wav"
    wav_factory(path, seconds=0.25)
    ops.finish_recording(
        db,
        meeting["id"],
        relative_path=path.name,
        format="wav",
        duration_seconds=0.25,
        size_bytes=path.stat().st_size,
        sample_rate=48000,
        channels=2,
    )

    window._on_profile_selected(user)
    window._open_meeting(meeting["id"])
    qapp.processEvents()

    screen = window.details_screen
    # No analysis backend is configured in the default test window.
    assert screen.transcribe_button.isEnabled()
    assert screen.transcribe_button.text() == "Transcribe recording"
    assert not screen.analyse_button.isEnabled()


def test_details_missing_file_disables_transcribe(
    window, qapp, db, settings, user
):
    meeting = ops.create_meeting(db, user["id"], "Missing audio meeting")
    ops.save_transcript(
        db,
        meeting["id"],
        text="We agreed to ship the release.",
        segments=[{"start": 0.0, "end": 1.0, "text": "We agreed to ship the release."}],
        language="en",
    )
    ops.finish_recording(
        db,
        meeting["id"],
        relative_path=f"{meeting['id']}.wav",
        format="wav",
        duration_seconds=1.0,
        size_bytes=1000,
        sample_rate=48000,
        channels=2,
    )

    window._on_profile_selected(user)
    window._open_meeting(meeting["id"])
    qapp.processEvents()

    screen = window.details_screen
    assert not screen.play_button.isEnabled()
    assert not screen.transcribe_button.isEnabled()
    # No analysis backend in the default window, so analysis is unavailable.
    assert not screen.analyse_button.isEnabled()
    assert "missing" in screen.notice_label.text().lower()


def test_details_analyse_works_without_recording_file(
    window_with_analysis, qapp, db, settings, user
):
    """Analysis needs only the transcript, so it works without the audio."""

    meeting = ops.create_meeting(db, user["id"], "Transcript only meeting")
    ops.save_transcript(
        db,
        meeting["id"],
        text="We agreed to ship the release.",
        segments=[{"start": 0.0, "end": 1.0, "text": "We agreed to ship the release."}],
        language="en",
    )
    ops.finish_recording(
        db,
        meeting["id"],
        relative_path=f"{meeting['id']}.wav",
        format="wav",
        duration_seconds=1.0,
        size_bytes=1000,
        sample_rate=48000,
        channels=2,
    )

    window_with_analysis._on_profile_selected(user)
    window_with_analysis._open_meeting(meeting["id"])
    qapp.processEvents()

    screen = window_with_analysis.details_screen
    assert not screen.play_button.isEnabled()
    assert not screen.transcribe_button.isEnabled()
    assert screen.analyse_button.isEnabled()
    assert screen.analyse_button.text() == "Analyse transcript"
    assert "missing" in screen.notice_label.text().lower()


# ---------------------------------------------------------------------------
# Re-transcribe confirmation
# ---------------------------------------------------------------------------


class _FakeQMessageBox:
    # Reuse the real enum so flag arithmetic (Yes | No) keeps working.
    StandardButton = QMessageBox.StandardButton
    last_title: str | None = None

    @staticmethod
    def question(_parent, title, _text, *_args, **_kwargs):
        _FakeQMessageBox.last_title = title
        return QMessageBox.StandardButton.Yes


def test_retranscribe_confirm_dialog_yes_starts_retranscription(
    window_with_analysis, qapp, user, processed_meeting, monkeypatch
):
    monkeypatch.setattr("ui.meeting_details.QMessageBox", _FakeQMessageBox)
    screen = window_with_analysis.details_screen
    window_with_analysis._on_profile_selected(user)
    window_with_analysis._open_meeting(processed_meeting["id"])
    qapp.processEvents()

    emitted: list[str] = []
    screen.transcribe_requested.connect(emitted.append)
    screen._request_transcription()

    assert _FakeQMessageBox.last_title == "Re-transcribe meeting"
    assert emitted == [processed_meeting["id"]]


def test_retranscribe_confirm_dialog_no_keeps_transcript(
    window_with_analysis, qapp, user, processed_meeting, monkeypatch
):
    class NoBox(_FakeQMessageBox):
        @staticmethod
        def question(*_args, **_kwargs):
            return QMessageBox.StandardButton.No

    monkeypatch.setattr("ui.meeting_details.QMessageBox", NoBox)
    screen = window_with_analysis.details_screen
    window_with_analysis._on_profile_selected(user)
    window_with_analysis._open_meeting(processed_meeting["id"])
    qapp.processEvents()

    emitted: list[str] = []
    screen.transcribe_requested.connect(emitted.append)
    screen._request_transcription()

    assert emitted == []
    details = ops.get_meeting_details(
        window_with_analysis.library, processed_meeting["id"]
    )
    assert details["transcript"] is not None
    assert details["summary"] is not None


def test_fresh_transcription_does_not_ask_for_confirmation(
    window_with_analysis, qapp, db, settings, user, wav_factory, monkeypatch
):
    meeting = ops.create_meeting(db, user["id"], "Fresh UI meeting")
    path = Path(meeting["meeting_dir"]) / "audio.wav"
    wav_factory(path, seconds=0.25)
    ops.finish_recording(
        db,
        meeting["id"],
        relative_path=path.name,
        format="wav",
        duration_seconds=0.25,
        size_bytes=path.stat().st_size,
        sample_rate=48000,
        channels=2,
    )

    asked: list[str] = []

    class AskBox(_FakeQMessageBox):
        @staticmethod
        def question(_parent, title, *_args, **_kwargs):
            asked.append(title)
            return QMessageBox.StandardButton.No

    monkeypatch.setattr("ui.meeting_details.QMessageBox", AskBox)
    screen = window_with_analysis.details_screen
    window_with_analysis._on_profile_selected(user)
    window_with_analysis._open_meeting(meeting["id"])
    qapp.processEvents()

    emitted: list[str] = []
    screen.transcribe_requested.connect(emitted.append)
    screen._request_transcription()

    assert asked == []
    assert emitted == [meeting["id"]]


# ---------------------------------------------------------------------------
# Speaker labels in the transcript view
# ---------------------------------------------------------------------------


def test_details_renders_speaker_labels(window, qapp, db, user):
    meeting = ops.create_meeting(db, user["id"], "Labelled meeting")
    ops.save_transcript(
        db,
        meeting["id"],
        text="Me: hello\nRemote: hi there",
        segments=[
            {"start": 0.0, "end": 1.0, "text": "hello", "speaker": "Me"},
            {"start": 1.5, "end": 2.0, "text": "hi there", "speaker": "Remote"},
        ],
        language="en",
    )

    window._on_profile_selected(user)
    window._open_meeting(meeting["id"])
    qapp.processEvents()

    text = window.details_screen.transcript_browser.toPlainText()
    assert "[00:00] Me:" in text
    assert "[00:01] Remote:" in text


# ---------------------------------------------------------------------------
# Cancel button
# ---------------------------------------------------------------------------


def test_details_cancel_button_visible_only_while_processing(
    window_with_analysis, qapp, user, processed_meeting
):
    screen = window_with_analysis.details_screen
    window_with_analysis._on_profile_selected(user)
    window_with_analysis._open_meeting(processed_meeting["id"])
    qapp.processEvents()

    assert not screen.cancel_button.isVisibleTo(screen)

    screen.set_processing(True, "Transcribing...")
    assert screen.cancel_button.isVisibleTo(screen)
    assert not screen.transcribe_button.isEnabled()
    assert not screen.analyse_button.isEnabled()

    emitted: list[str] = []
    screen.cancel_requested.connect(
        lambda: emitted.append(screen._meeting_id or "")
    )
    screen._request_cancel()
    assert emitted == [processed_meeting["id"]]

    screen.set_processing(False)
    assert not screen.cancel_button.isVisibleTo(screen)


def test_details_processing_cancelled_returns_to_idle(
    window_with_analysis, qapp, user, processed_meeting
):
    screen = window_with_analysis.details_screen
    window_with_analysis._on_profile_selected(user)
    window_with_analysis._open_meeting(processed_meeting["id"])
    screen.set_processing(True, "Transcribing...")
    screen.processing_cancelled()
    qapp.processEvents()

    assert not screen._processing
    assert not screen.cancel_button.isVisibleTo(screen)
    assert "cancelled" in screen.status_label.text().lower()
    # The meeting keeps its previous state.
    details = ops.get_meeting_details(
        window_with_analysis.library, processed_meeting["id"]
    )
    assert details["status"] == "completed"


def test_details_handles_missing_recording_file(window, qapp, db, settings, user):
    meeting = ops.create_meeting(db, user["id"], "Missing audio meeting")
    ops.finish_recording(
        db,
        meeting["id"],
        relative_path=f"{meeting['id']}.wav",
        format="wav",
        duration_seconds=2.0,
        size_bytes=1000,
        sample_rate=48000,
        channels=2,
    )

    window._on_profile_selected(user)
    window._open_meeting(meeting["id"])
    qapp.processEvents()

    screen = window.details_screen
    assert not screen.play_button.isEnabled()
    assert not screen.transcribe_button.isEnabled()
    assert not screen.analyse_button.isEnabled()
    assert "missing" in screen.notice_label.text().lower()


def test_details_shows_no_action_items_message(
    window, qapp, db, user, processed_meeting
):
    analysis = valid_analysis()
    analysis["action_items"] = []
    ops.save_analysis(db, processed_meeting["id"], analysis, model=None)

    window._on_profile_selected(user)
    window._open_meeting(processed_meeting["id"])
    qapp.processEvents()

    screen = window.details_screen
    assert screen.actions_table.rowCount() == 0
    assert "No action items" in screen.no_actions_label.text()


def test_recording_screen_validates_title(window, qapp, user):
    window._on_profile_selected(user)
    window._start_recording()
    qapp.processEvents()

    screen = window.recording_screen
    screen.title_edit.clear()
    screen._start_recording()
    assert "title" in screen.error_label.text().lower()
    assert screen._state == "idle"


def test_dashboard_delete_removes_meeting_and_file(
    window, qapp, db, settings, user, processed_meeting, monkeypatch
):
    meeting_dir = Path(processed_meeting["meeting_dir"])
    recording_path = meeting_dir / "audio.wav"
    assert recording_path.exists()

    window._on_profile_selected(user)
    window.dashboard_screen.refresh()
    window.dashboard_screen.table.selectRow(0)

    monkeypatch.setattr(
        "ui.dashboard.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    window.dashboard_screen._delete_selected()
    qapp.processEvents()

    assert ops.get_meeting(db, processed_meeting["id"]) is None
    assert not recording_path.exists()
    assert window.dashboard_screen.table.rowCount() == 0


def test_details_tabs_are_summary_and_transcript(window):
    """The analysis and the action items share one Summary tab."""

    screen = window.details_screen
    labels = [screen.tabs.tabText(i) for i in range(screen.tabs.count())]
    assert labels == ["Summary", "Transcript"]
    # The analysis browser and the action-item table live in the same tab.
    assert screen.overview_browser.parentWidget() is screen.actions_table.parentWidget()
