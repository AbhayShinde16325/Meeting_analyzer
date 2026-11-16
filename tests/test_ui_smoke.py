"""UI end-to-end tests: real windows against a real HTTP server.

These tests exercise the full client stack — PySide6 screens, QThread workers,
the httpx client, and the live FastAPI server on PostgreSQL — the same way a
user runs the application.  Only the AI providers and the background job
runner on the server are faked; tests drive the pipeline synchronously.
"""

from __future__ import annotations

import uuid
import zipfile
from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from support import wait_until, write_pcm_wav


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_meeting(client, user, *, title="UI test meeting"):
    """Upload a small recording through the client (host-only endpoint)."""

    audio_dir = Path(client.base_url.replace(":", "_"))  # unused path guard
    del audio_dir
    import tempfile

    work_dir = Path(tempfile.mkdtemp(prefix="tma-seed-"))
    try:
        audio = write_pcm_wav(work_dir / "audio.wav", seconds=0.3)
        meeting = client.upload_recording(
            title=title,
            user_id=str(user["id"]),
            audio=audio,
            filename="audio.wav",
            duration_ms=300,
        )
    finally:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)
    return meeting


def _queued_transcription_job(client, meeting_id):
    jobs = client.list_jobs(meeting_id)
    queued = [
        job for job in jobs
        if job["job_type"] == "transcription" and job["status"] == "queued"
    ]
    assert queued, f"Expected a queued transcription job, got {jobs}"
    return queued[0]


def _seed_processed_meeting(server, client, user, *, title="Processed meeting"):
    """Upload a recording and run transcription + analysis synchronously."""

    meeting = _seed_meeting(client, user, title=title)
    server.run_job(_queued_transcription_job(client, meeting["id"])["id"])
    analysis_job = client.analyse(meeting["id"])
    server.run_job(analysis_job["id"])
    return meeting


def _login(window, user):
    window._on_profile_selected(user)


# ---------------------------------------------------------------------------
# Startup / server check
# ---------------------------------------------------------------------------


def test_window_starts_on_login_screen_and_reports_server(window, qapp):
    from PySide6.QtWidgets import QApplication

    assert window.stack.currentWidget() is window.login_screen
    assert wait_until(
        lambda: window.statusBar().currentMessage().startswith("Ready")
    )
    QApplication.instance().processEvents()


# ---------------------------------------------------------------------------
# Login screen
# ---------------------------------------------------------------------------


def test_login_lists_server_profiles(window, client, user):
    screen = window.login_screen
    assert wait_until(lambda: screen.profile_combo.count() >= 2)
    labels = [
        screen.profile_combo.itemText(i) for i in range(screen.profile_combo.count())
    ]
    assert "Test User" in labels


def test_login_continues_with_selected_profile(window, client, user):
    screen = window.login_screen
    assert wait_until(lambda: screen.profile_combo.count() >= 2)
    index = screen.profile_combo.findData(str(user["id"]))
    assert index >= 0
    screen.profile_combo.setCurrentIndex(index)

    selected: list = []
    screen.profile_selected.connect(selected.append)
    screen._continue()

    assert len(selected) == 1
    assert str(selected[0]["id"]) == str(user["id"])


def test_login_creates_new_profile(window, client):
    screen = window.login_screen
    screen.new_profile_edit.setText("Fresh Tester")
    selected: list = []
    screen.profile_selected.connect(selected.append)
    screen._continue()

    assert wait_until(lambda: bool(selected))
    assert selected[0]["display_name"] == "Fresh Tester"
    names = [u["display_name"] for u in client.list_users()]
    assert "Fresh Tester" in names


def test_main_window_login_flow_reaches_dashboard(window, client, user):
    screen = window.login_screen
    assert wait_until(lambda: screen.profile_combo.count() >= 2)
    screen.new_profile_edit.setText(user["display_name"])
    screen._continue()
    assert wait_until(
        lambda: window.stack.currentWidget() is window.dashboard_screen
    )
    assert window.client.profile_id == str(user["id"])
    assert window.dashboard_screen.user_label.text().endswith(user["display_name"])


# ---------------------------------------------------------------------------
# Dashboard: list, search, delete
# ---------------------------------------------------------------------------


def test_dashboard_lists_meetings(window, client, user):
    meeting = _seed_meeting(client, user, title="Recorded UI meeting")
    _login(window, user)

    table = window.dashboard_screen.table
    assert wait_until(lambda: table.rowCount() == 1)
    assert table.item(0, 1).text() == "Recorded UI meeting"
    status = table.item(0, 3).text()
    assert status == "Recorded"  # ingest queued transcription server-side


def test_dashboard_search_filters(window, client, user):
    _seed_meeting(client, user, title="Release planning")
    _seed_meeting(client, user, title="Budget review")
    _login(window, user)

    table = window.dashboard_screen.table
    assert wait_until(lambda: table.rowCount() == 2)

    window.dashboard_screen.search_edit.setText("Budget")
    assert wait_until(lambda: table.rowCount() == 1)
    assert table.item(0, 1).text() == "Budget review"

    window.dashboard_screen.search_edit.setText("zzz-no-match")
    assert wait_until(lambda: not window.dashboard_screen.empty_label.isHidden())


def test_dashboard_delete_is_two_step_and_permanent(
    window, client, user, monkeypatch
):
    meeting = _seed_meeting(client, user, title="Doomed meeting")
    _login(window, user)
    table = window.dashboard_screen.table
    assert wait_until(lambda: table.rowCount() == 1)
    table.selectRow(0)

    asked: list[str] = []

    def fake_question(*_args, **_kwargs):
        asked.append("confirm")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr("ui.dashboard.QMessageBox.question", fake_question)
    screen = window.dashboard_screen

    # First press only arms the destructive button.
    screen._delete_selected()
    assert screen.delete_button.text() == "Confirm delete?"
    assert client.get_meeting(meeting["id"])["id"] == meeting["id"]

    # Second press confirms; the meeting is gone on the server.
    screen._delete_selected()
    assert wait_until(lambda: table.rowCount() == 0)
    assert asked == ["confirm"]
    from client.api_client import ApiClientError

    with pytest.raises(ApiClientError) as excinfo:
        client.get_meeting(meeting["id"])
    assert excinfo.value.status_code == 404


def test_dashboard_delete_second_press_within_window(
    window, client, user, monkeypatch
):
    """Disarm happens only via the timeout, not a different meeting arm."""

    meeting = _seed_meeting(client, user, title="Armed meeting")
    _login(window, user)
    table = window.dashboard_screen.table
    assert wait_until(lambda: table.rowCount() == 1)
    table.selectRow(0)
    screen = window.dashboard_screen

    screen._delete_selected()
    assert screen._delete_armed_for == meeting["id"]
    # A disarm call for another meeting id must not clear the current arm.
    screen._disarm_delete("some-other-id")
    assert screen._delete_armed_for == meeting["id"]


def test_dashboard_status_footer(window, client, user):
    _seed_meeting(client, user)
    _login(window, user)
    assert wait_until(
        lambda: window.dashboard_screen.status_label.text().startswith("Server:")
    )
    text = window.dashboard_screen.status_label.text()
    assert "1 meetings" in text
    assert "database" in text


# ---------------------------------------------------------------------------
# Meeting details
# ---------------------------------------------------------------------------


def test_details_renders_transcript_summary_and_actions(
    window, server, client, user
):
    meeting = _seed_processed_meeting(server, client, user)
    _login(window, user)
    window._open_meeting(meeting["id"])
    screen = window.details_screen

    assert wait_until(
        lambda: "Fake primary transcript content."
        in screen.transcript_browser.toPlainText()
    )
    assert wait_until(
        lambda: "The team agreed to ship the release."
        in screen.overview_browser.toPlainText()
    )
    assert screen.actions_table.rowCount() == 1
    assert screen.actions_table.item(0, 0).text() == "Ship the release"
    assert screen.tabs.tabText(0) == "Summary"


def test_details_unprocessed_meeting_offers_transcription(
    window, client, user
):
    meeting = _seed_meeting(client, user, title="Raw meeting")
    _login(window, user)
    window._open_meeting(meeting["id"])
    screen = window.details_screen

    assert wait_until(
        lambda: "not been processed" in screen.overview_browser.toPlainText()
    )
    assert screen.transcribe_button.isEnabled()
    assert screen.transcribe_button.text() == "Transcribe recording"
    assert not screen.analyse_button.isEnabled()
    assert not screen.play_button.isEnabled() or screen.play_button.isEnabled()


def test_details_attachments_tab_download(window, client, user, tmp_path, monkeypatch):
    meeting = _seed_meeting(client, user)
    client.upload_attachment(
        meeting["id"], _make_attachment(tmp_path, "notes.txt", b"attached text")
    )
    _login(window, user)
    window._open_meeting(meeting["id"])
    screen = window.details_screen

    assert wait_until(lambda: screen.attachments_table.rowCount() == 1)
    assert screen.attachments_table.item(0, 0).text() == "notes.txt"

    opened: list[str] = []
    monkeypatch.setattr(
        "ui.meeting_details.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )
    screen.attachments_table.selectRow(0)
    screen._open_selected_attachment()

    assert wait_until(lambda: bool(opened))
    cache_dir = window.settings.data_dir / "audio-cache"
    files = list(cache_dir.glob("*-notes.txt"))
    assert files and files[0].read_text(encoding="utf-8") == "attached text"


def test_details_retranscribe_confirm_then_cancel(
    window, server, client, user, monkeypatch
):
    meeting = _seed_processed_meeting(server, client, user)
    _login(window, user)
    window._open_meeting(meeting["id"])
    screen = window.details_screen
    assert wait_until(lambda: screen.transcribe_button.text() == "Re-transcribe")

    asked: list[str] = []

    def fake_question(_parent, title, *_args, **_kwargs):
        asked.append(title)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr("ui.meeting_details.QMessageBox.question", fake_question)
    screen._request_transcription()

    assert asked == ["Re-transcribe meeting"]
    assert wait_until(lambda: screen._processing)
    assert wait_until(lambda: "queued" in screen.status_label.text().lower())
    assert screen.cancel_button.isVisibleTo(screen)

    screen._request_cancel()
    assert wait_until(lambda: "cancellation requested" in screen.status_label.text().lower())
    assert not screen.cancel_button.isVisibleTo(screen)


def test_details_fresh_transcription_needs_no_confirm(
    window, client, user, monkeypatch
):
    meeting = _seed_meeting(client, user)
    _login(window, user)
    window._open_meeting(meeting["id"])
    screen = window.details_screen
    assert wait_until(lambda: screen.transcribe_button.isEnabled())

    asked: list[str] = []

    def fake_question(_parent, title, *_args, **_kwargs):
        asked.append(title)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr("ui.meeting_details.QMessageBox.question", fake_question)
    screen._request_transcription()

    assert asked == []  # no re-transcription confirmation for fresh meetings
    assert wait_until(lambda: screen._processing)


def test_details_export_zip(window, server, client, user, tmp_path, monkeypatch):
    meeting = _seed_processed_meeting(server, client, user)
    _login(window, user)
    window._open_meeting(meeting["id"])
    screen = window.details_screen
    assert wait_until(lambda: screen.export_button.isEnabled())

    target = tmp_path / "export.zip"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "")),
    )
    opened: list[str] = []
    monkeypatch.setattr(
        "ui.meeting_details.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )
    screen._export_meeting()

    assert wait_until(lambda: target.is_file() and zipfile.is_zipfile(target))
    archive = zipfile.ZipFile(target)
    names = archive.namelist()
    assert "meeting.json" in names
    assert "transcripts/primary.txt" in names


# ---------------------------------------------------------------------------
# Recording screen
# ---------------------------------------------------------------------------


def test_recording_screen_disabled_without_host_capability(window, qapp):
    screen = window.recording_screen
    screen.set_can_record(False)
    assert not screen.start_button.isEnabled()
    screen._start_recording()
    assert "host" in screen.error_label.text().lower()
    assert screen._state == "idle"


def test_stop_worker_uploads_recording(window, client, user, tmp_path):
    """The stop worker finalises the local WAV and uploads it to the server."""

    from audio.recorder import RecordingResult
    from ui.recorder import _StopWorker

    work_dir = tmp_path / "capture"
    work_dir.mkdir()
    audio = write_pcm_wav(work_dir / "audio.wav", seconds=0.3)

    class _FakeRecorder:
        def stop(self) -> RecordingResult:
            return RecordingResult(
                meeting_id="local",
                relative_path="audio.wav",
                absolute_path=audio,
                duration_seconds=0.3,
                size_bytes=audio.stat().st_size,
                sample_rate=48000,
                channels=2,
            )

    _login(window, user)
    worker = _StopWorker(
        window.client, str(user["id"]), "Uploaded meeting", _FakeRecorder(), work_dir
    )
    results: list = []
    worker.completed.connect(lambda meeting, message: results.append((meeting, message)))
    worker.start()
    assert wait_until(lambda: bool(results))

    meeting, message = results[0]
    assert message is None
    assert meeting["title"] == "Uploaded meeting"
    titles = [m["title"] for m in client.list_meetings()]
    assert "Uploaded meeting" in titles
    # The temporary capture directory is cleaned up after upload.
    assert not work_dir.exists()


# ---------------------------------------------------------------------------
# System status dialog
# ---------------------------------------------------------------------------


def test_system_status_dialog_renders_server_health(window, client, user):
    from ui.system_status import SystemStatusDialog

    _seed_meeting(client, user)
    dialog = SystemStatusDialog(window.client, window)
    try:
        assert wait_until(
            lambda: "meetings stored on the server" in dialog.summary_label.text()
        )
        assert dialog.headroom_bar.isVisibleTo(dialog)
        assert "Last backup" in dialog.backup_label.text() or "No backup" in (
            dialog.backup_label.text()
        )
    finally:
        dialog.shutdown()
        dialog.close()


def _make_attachment(directory: Path, filename: str, payload: bytes) -> Path:
    path = directory / filename
    path.write_bytes(payload)
    return path
