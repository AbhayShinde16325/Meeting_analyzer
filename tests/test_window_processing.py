"""Window-level processing tests.

These drive ``MainWindow._transcribe_meeting`` / ``_analyse_meeting`` /
``_cancel_processing`` end to end with fake providers — the regression net
for the empty-slot bug that made the whole product inert (AUD-001/010).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMessageBox

from storage import library as ops
from support import BlockingFakeTranscriptionProvider

_WORKER_TIMEOUT = 10.0


def _wait_for_worker(window, qapp, timeout: float = _WORKER_TIMEOUT) -> None:
    """Pump the event loop until the window's processing worker is done."""

    deadline = time.monotonic() + timeout
    while window._worker is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert window._worker is None, "processing worker did not finish in time"


def _wait_for_provider_started(window, qapp, timeout: float = _WORKER_TIMEOUT):
    """Pump the event loop until the blocking fake provider has started."""

    provider = window.transcription_service._provider
    deadline = time.monotonic() + timeout
    while not provider.started.is_set() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert provider.started.is_set(), "blocking provider never started"
    return provider


# ---------------------------------------------------------------------------
# Transcription from the window
# ---------------------------------------------------------------------------


def test_window_transcribes_recorded_meeting(
    window_with_analysis, qapp, db, recorded_meeting, monkeypatch
):
    window = window_with_analysis
    meeting_id = recorded_meeting["id"]

    calls: list[tuple[bool, str]] = []
    monkeypatch.setattr(
        window.details_screen,
        "set_processing",
        lambda active, message="": calls.append((active, message)),
    )

    window._transcribe_meeting(meeting_id)
    _wait_for_worker(window, qapp)

    details = ops.get_meeting_details(db, meeting_id)
    assert details["status"] == "transcribed"
    assert (
        details["transcript"]["text"]
        == "Alice: We agreed to ship the release on Friday."
    )
    assert details["summary"] is None  # stage=transcribe never analyses

    meeting_dir = Path(recorded_meeting["meeting_dir"])
    assert (meeting_dir / "transcript.md").is_file()

    # Progress and busy-state reached the details screen.
    assert any(active for active, _ in calls)
    assert any(
        "fake transcription progress" in message for _, message in calls
    )


def test_window_analyses_transcribed_meeting(
    window_with_analysis, qapp, db, user, processed_meeting
):
    window = window_with_analysis
    meeting_id = processed_meeting["id"]
    window._on_profile_selected(user)

    # The user is viewing this meeting while the analysis runs.
    window._open_meeting(meeting_id)
    qapp.processEvents()

    window._analyse_meeting(meeting_id)
    _wait_for_worker(window, qapp)

    details = ops.get_meeting_details(db, meeting_id)
    assert details["status"] == "completed"
    assert details["summary"]["overview"] == "The team agreed to ship the release."
    assert details["action_items"], "action items should be saved"

    meeting_dir = Path(processed_meeting["meeting_dir"])
    assert (meeting_dir / "analysis.md").is_file()

    # The details screen re-rendered the saved analysis.
    assert window.details_screen._details["summary"]["overview"] == (
        "The team agreed to ship the release."
    )
    # The dashboard reflects the completed meeting.
    assert window.dashboard_screen.table.rowCount() == 1


def test_window_retranscribe_replaces_transcript_and_clears_analysis(
    window_with_analysis, qapp, db, processed_meeting
):
    """Re-transcribing an already-transcribed meeting forces the replace."""

    window = window_with_analysis
    meeting_id = processed_meeting["id"]
    before = ops.get_meeting_details(db, meeting_id)
    assert before["summary"] is not None  # precondition

    window._transcribe_meeting(meeting_id)
    _wait_for_worker(window, qapp)

    details = ops.get_meeting_details(db, meeting_id)
    assert details["status"] == "transcribed"
    assert details["summary"] is None
    assert details["action_items"] == []
    assert details["transcript"]["text"] == (
        "Alice: We agreed to ship the release on Friday."
    )


def test_window_ignores_overlapping_processing_request(
    window_with_analysis, qapp, db, recorded_meeting
):
    """A second start while a worker runs must not spawn a second worker."""

    window = window_with_analysis
    meeting_id = recorded_meeting["id"]
    window.transcription_service._provider = BlockingFakeTranscriptionProvider()

    window._transcribe_meeting(meeting_id)
    _wait_for_provider_started(window, qapp)
    first_worker = window._worker

    window._transcribe_meeting(meeting_id)
    assert window._worker is first_worker

    window._cancel_processing()
    _wait_for_worker(window, qapp)


# ---------------------------------------------------------------------------
# Cancellation from the window
# ---------------------------------------------------------------------------


def test_window_cancel_processing_restores_meeting(
    window_with_analysis, qapp, db, recorded_meeting
):
    window = window_with_analysis
    meeting_id = recorded_meeting["id"]
    window.transcription_service._provider = BlockingFakeTranscriptionProvider()

    window._transcribe_meeting(meeting_id)
    _wait_for_provider_started(window, qapp)

    window._cancel_processing()
    _wait_for_worker(window, qapp)

    details = ops.get_meeting_details(db, meeting_id)
    assert details["status"] == "recorded"  # restored pre-processing status
    assert details["transcript"] is None

    # The details screen returned to its idle state.
    assert window.details_screen._processing is False
    assert not window.details_screen.cancel_button.isVisible()
    assert window.details_screen.status_label.text() == "Processing cancelled."


def test_window_cancel_without_worker_is_harmless(window_with_analysis):
    """Cancelling when nothing runs must not raise."""

    assert window_with_analysis._worker is None
    window_with_analysis._cancel_processing()


def test_window_close_event_cancels_running_worker(
    window_with_analysis, qapp, db, recorded_meeting, monkeypatch
):
    window = window_with_analysis
    meeting_id = recorded_meeting["id"]
    window.transcription_service._provider = BlockingFakeTranscriptionProvider()

    questions: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(
            lambda _parent, title, _text, *_a, **_k: questions.append(title)
            or QMessageBox.StandardButton.Yes
        ),
    )

    window._transcribe_meeting(meeting_id)
    _wait_for_provider_started(window, qapp)

    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    assert questions, "the user should be asked before quitting mid-work"

    _wait_for_worker(window, qapp)
    details = ops.get_meeting_details(db, meeting_id)
    assert details["status"] == "recorded"
    assert details["transcript"] is None


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


class _FakePlayer:
    """Records setSource/play/stop calls instead of touching the backend."""

    def __init__(self, state=None):
        self._state = state
        self.sources: list[str] = []
        self.play_calls = 0
        self.stop_calls = 0

    def playbackState(self):
        return self._state

    def setSource(self, url):
        self.sources.append(url.toString())

    def play(self):
        self.play_calls += 1

    def stop(self):
        self.stop_calls += 1


def test_play_button_is_connected_to_toggle_playback(qapp, db, settings, monkeypatch):
    from ui.meeting_details import MeetingDetailsScreen

    toggles: list[bool] = []
    monkeypatch.setattr(
        MeetingDetailsScreen, "_toggle_playback", lambda self: toggles.append(True)
    )
    screen = MeetingDetailsScreen(db, settings)
    screen.play_button.setEnabled(True)
    screen.play_button.click()
    assert toggles == [True]


def test_toggle_playback_plays_existing_audio(
    qapp, db, settings, recorded_meeting, monkeypatch
):
    from ui.meeting_details import MeetingDetailsScreen

    screen = MeetingDetailsScreen(db, settings)
    fake_player = _FakePlayer()
    monkeypatch.setattr(screen, "_player", fake_player, raising=False)
    screen._details = ops.get_meeting_details(db, recorded_meeting["id"])

    screen._toggle_playback()
    assert fake_player.play_calls == 1
    assert fake_player.stop_calls == 0
    assert fake_player.sources and fake_player.sources[0].endswith("audio.wav")


def test_toggle_playback_stops_when_already_playing(
    qapp, db, settings, recorded_meeting, monkeypatch
):
    from PySide6.QtMultimedia import QMediaPlayer

    from ui.meeting_details import MeetingDetailsScreen

    screen = MeetingDetailsScreen(db, settings)
    fake_player = _FakePlayer(state=QMediaPlayer.PlaybackState.PlayingState)
    monkeypatch.setattr(screen, "_player", fake_player, raising=False)
    screen._details = dict(recorded_meeting)

    screen._toggle_playback()
    assert fake_player.stop_calls == 1
    assert fake_player.play_calls == 0


def test_toggle_playback_missing_file_shows_notice(
    qapp, db, settings, user, monkeypatch
):
    from ui.meeting_details import MeetingDetailsScreen

    meeting = ops.create_meeting(db, user["id"], "Missing audio meeting")
    screen = MeetingDetailsScreen(db, settings)
    monkeypatch.setattr(screen, "_player", _FakePlayer(), raising=False)
    # A meeting whose recording points at a file that does not exist on disk.
    screen._details = {
        "id": meeting["id"],
        "recording": {"audio": "audio.wav"},
    }

    screen._toggle_playback()
    assert "missing" in screen.notice_label.text().lower()
