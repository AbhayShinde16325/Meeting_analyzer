"""Manual meeting recording screen (host machine only).

Recording always requires an explicit Start action and a clear indicator.
Audio is captured locally (microphone + system loopback), finalised into one
WAV file, and then uploaded to the team server in a background thread.  After
upload the server queues transcription: audio is sent to Sarvam's cloud
service, which is disclosed in the consent label below.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from audio.microphone import AudioDevice, AudioDeviceError, list_microphones
from audio.recorder import MeetingRecorder, RecordingResult
from audio.system_audio import list_loopback_devices
from client.api_client import ApiClientError, ApiConnectionError, TeamMeetingAIClient
from config import Settings
from ui.qt_utils import worker_running


class _StopWorker(QThread):
    """Finalise the local WAV and upload it to the server off the UI thread."""

    completed = Signal(object, object)  # meeting dict (or None), message
    failed = Signal(str)

    def __init__(
        self,
        client: TeamMeetingAIClient,
        user_id: str,
        title: str,
        recorder: MeetingRecorder,
        work_dir: Path,
    ) -> None:
        super().__init__()
        self.client = client
        self.user_id = user_id
        self.title = title
        self.recorder = recorder
        self.work_dir = work_dir

    def run(self) -> None:
        meeting: dict[str, Any] | None = None
        failure: str | None = None
        try:
            result: RecordingResult = self.recorder.stop()
            meeting = self.client.upload_recording(
                title=self.title,
                user_id=self.user_id,
                audio=result.absolute_path,
                filename=result.absolute_path.name,
                duration_ms=int(round(result.duration_seconds * 1000)),
                sample_rate=result.sample_rate,
                channels=result.channels,
            )
        except (ApiClientError, ApiConnectionError) as exc:
            failure = str(exc)
        except Exception as exc:
            failure = f"Unexpected error: {type(exc).__name__}: {exc}"
        finally:
            # The temporary capture directory is only needed until upload;
            # the server keeps the audio in PostgreSQL from here on.
            shutil.rmtree(self.work_dir, ignore_errors=True)
        if failure is not None:
            self.failed.emit(failure)
            return
        self.completed.emit(meeting, None)


class RecordingScreen(QWidget):
    """Record locally, then upload the finished WAV to the team server."""

    recording_saved = Signal(str)  # server meeting id
    cancelled = Signal()

    _capture_error = Signal(str)

    def __init__(
        self,
        client: TeamMeetingAIClient,
        settings: Settings,
        parent: QWidget | None = None,
        *,
        can_record: bool = True,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.settings = settings
        self.current_user: dict[str, Any] | None = None
        self._recorder: MeetingRecorder | None = None
        self._work_dir: Path | None = None
        self._stop_worker: _StopWorker | None = None
        self._state = "idle"
        self._microphones: dict[str, AudioDevice] = {}
        self._loopbacks: dict[str, AudioDevice] = {}
        self._can_record = can_record

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(500)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._capture_error.connect(self._on_capture_error)

        self._build_ui()
        self.refresh_devices()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self._go_back)
        title = QLabel("Record a meeting")
        title.setObjectName("pageTitle")
        header.addWidget(self.back_button)
        header.addSpacing(12)
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        consent = QLabel(
            "Recording requires consent from all participants. Start recording "
            "only when everyone has agreed. After you stop, the audio is "
            "uploaded to the team server and sent to Sarvam (cloud) for "
            "transcription. A clear indicator and elapsed timer remain visible "
            "while recording."
        )
        consent.setWordWrap(True)
        consent.setObjectName("consentText")
        layout.addWidget(consent)

        title_row = QHBoxLayout()
        title_label = QLabel("Meeting title")
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("e.g. Weekly product planning")
        self.title_edit.setToolTip("Enter a descriptive name for your meeting.")
        self.title_edit.setMinimumHeight(36)
        title_row.addWidget(title_label)
        title_row.addWidget(self.title_edit, 1)
        layout.addLayout(title_row)

        devices = QGroupBox("Audio devices")
        devices_layout = QGridLayout(devices)
        devices_layout.setContentsMargins(16, 18, 16, 16)
        devices_layout.setHorizontalSpacing(12)
        devices_layout.setVerticalSpacing(10)

        devices_layout.addWidget(QLabel("Microphone"), 0, 0)
        self.microphone_combo = QComboBox()
        self.microphone_combo.setMinimumHeight(32)
        devices_layout.addWidget(self.microphone_combo, 0, 1)

        devices_layout.addWidget(QLabel("Meeting audio (speakers)"), 1, 0)
        self.loopback_combo = QComboBox()
        self.loopback_combo.setMinimumHeight(32)
        devices_layout.addWidget(self.loopback_combo, 1, 1)

        self.refresh_devices_button = QPushButton("Refresh devices")
        self.refresh_devices_button.setToolTip(
            "Refresh the list of available audio devices."
        )
        self.refresh_devices_button.clicked.connect(self.refresh_devices)
        devices_layout.addWidget(self.refresh_devices_button, 2, 1)
        devices_layout.setColumnStretch(1, 1)
        layout.addWidget(devices)

        status_card = QFrame()
        status_card.setObjectName("statusCard")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(20, 18, 20, 18)
        status_layout.setSpacing(6)

        self.indicator_label = QLabel("Idle")
        self.indicator_label.setObjectName("recordingIdle")
        self.indicator_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.elapsed_label = QLabel("00:00:00")
        self.elapsed_label.setObjectName("elapsedTime")
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.status_label = QLabel(
            "Choose the microphone and the speakers used by Google Meet."
        )
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setObjectName("muted")

        status_layout.addWidget(self.indicator_label)
        status_layout.addWidget(self.elapsed_label)
        status_layout.addWidget(self.status_label)
        layout.addWidget(status_card)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("Start recording")
        self.start_button.setObjectName("recordButton")
        self.start_button.setMinimumHeight(46)
        self.start_button.clicked.connect(self._start_recording)

        self.stop_button = QPushButton("Stop recording")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setMinimumHeight(46)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_recording)

        buttons.addStretch(1)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("errorText")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        layout.addStretch(1)

        if not self._can_record:
            self.status_label.setText(
                "Recording is only possible on the meeting host computer "
                "(the machine with the microphone and the meeting audio)."
            )

        self._update_state_ui()

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._state == "recording"

    def set_user(self, user: dict[str, Any]) -> None:
        self.current_user = user

    def set_can_record(self, can_record: bool) -> None:
        """Enable/disable recording (host machine only)."""

        self._can_record = can_record
        if not can_record:
            self.status_label.setText(
                "Recording is only possible on the meeting host computer "
                "(the machine with the microphone and the meeting audio)."
            )
        self._update_state_ui()

    def prepare_new(self) -> None:
        self._recorder = None
        self._work_dir = None
        self.title_edit.clear()
        self._show_error("")
        self.elapsed_label.setText("00:00:00")
        self.status_label.setText(
            "Choose the microphone and the speakers used by Google Meet."
        )
        self._state = "idle"
        self.refresh_devices()
        self._update_state_ui()
        self.title_edit.setFocus()

    def resume_devices(self) -> None:
        self.refresh_devices()

    # -- devices -----------------------------------------------------------

    def refresh_devices(self) -> None:
        self._show_error("")
        self._load_device_list(
            self.microphone_combo, self._microphones, list_microphones, "microphone"
        )
        self._load_device_list(
            self.loopback_combo, self._loopbacks, list_loopback_devices, "speaker"
        )
        self._update_state_ui()

    def _load_device_list(self, combo, cache, loader, kind) -> None:
        previous = combo.currentData()
        combo.clear()
        cache.clear()
        try:
            devices = loader()
        except AudioDeviceError as exc:
            combo.addItem(f"No {kind} devices available", None)
            self._show_error(f"{exc} You can still review past meetings.")
            return
        except Exception as exc:
            combo.addItem(f"Could not list {kind} devices", None)
            self._show_error(f"Could not list {kind} devices: {exc}")
            return

        for device in devices:
            cache[str(device.id)] = device
            combo.addItem(device.label, device.id)
        if previous is not None:
            index = combo.findData(previous)
            if index >= 0:
                combo.setCurrentIndex(index)
        if combo.currentData() is None and combo.count():
            combo.setCurrentIndex(0)

    # -- recording state -----------------------------------------------------

    def _start_recording(self) -> None:
        if self._state != "idle":
            return
        if not self._can_record:
            self._show_error(
                "Recording is only possible on the meeting host computer."
            )
            return
        if self.current_user is None:
            self._show_error("Choose a profile before recording.")
            return
        title = self.title_edit.text().strip()
        if not title:
            self._show_error("Enter a meeting title before starting.")
            self.title_edit.setFocus()
            return
        if self.microphone_combo.currentData() is None:
            self._show_error("No microphone is selected.")
            return
        if self.loopback_combo.currentData() is None:
            self._show_error("No system-audio playback device is selected.")
            return

        self._show_error("")
        # The meeting record is created on the server only after a successful
        # stop+upload; during capture the audio lives in a temporary directory.
        work_dir = Path(tempfile.mkdtemp(prefix="tma-capture-"))
        local_id = str(uuid.uuid4())
        try:
            recorder = MeetingRecorder(
                self.settings,
                microphone_id=str(self.microphone_combo.currentData()),
                system_device_id=str(self.loopback_combo.currentData()),
                on_error=lambda message: self._capture_error.emit(message),
            )
            recorder.start(local_id, work_dir)
        except Exception as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            self._show_error(str(exc) or "Recording could not be started.")
            self._state = "idle"
            self._update_state_ui()
            return

        self._recorder = recorder
        self._work_dir = work_dir
        self._state = "recording"
        self._elapsed_timer.start()
        self._update_elapsed()
        self._update_state_ui()

    def _stop_recording(self) -> None:
        if (
            self._state != "recording"
            or self._recorder is None
            or self._work_dir is None
            or self.current_user is None
        ):
            return
        title = self.title_edit.text().strip()
        if not title:
            title = "Untitled meeting"
        self._state = "finalising"
        self._elapsed_timer.stop()
        self._update_state_ui()
        self.status_label.setText("Finalising and uploading the recording...")

        worker = _StopWorker(
            self.client,
            str(self.current_user["id"]),
            title,
            self._recorder,
            self._work_dir,
        )
        worker.completed.connect(self._on_recording_saved)
        worker.failed.connect(self._on_recording_failed)
        worker.finished.connect(self._clear_stop_worker)
        worker.finished.connect(worker.deleteLater)
        self._stop_worker = worker
        worker.start()

    def _clear_stop_worker(self) -> None:
        self._stop_worker = None

    def _on_recording_saved(self, meeting: object, _message: object) -> None:
        self._recorder = None
        self._work_dir = None
        self._state = "idle"
        self.elapsed_label.setText("00:00:00")
        self.status_label.setText("Recording uploaded. Transcription is queued.")
        self._update_state_ui()
        if isinstance(meeting, dict) and meeting.get("id"):
            self.recording_saved.emit(str(meeting["id"]))

    def _on_recording_failed(self, message: str) -> None:
        self._recorder = None
        self._work_dir = None
        self._state = "idle"
        self.status_label.setText("Recording could not be completed.")
        self._show_error(message or "Recording failed.")
        self._update_state_ui()

    def _on_capture_error(self, message: str) -> None:
        self._show_error(message)
        if self._state == "recording":
            self.status_label.setText("Recording with an audio problem - see below.")

    def _update_elapsed(self) -> None:
        if self._recorder is None:
            return
        elapsed = int(self._recorder.elapsed_seconds)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.elapsed_label.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}")

    def _update_state_ui(self) -> None:
        idle = self._state == "idle"
        recording = self._state == "recording"
        finalising = self._state == "finalising"

        devices_ready = (
            self.microphone_combo.currentData() is not None
            and self.loopback_combo.currentData() is not None
        )
        self.start_button.setEnabled(
            idle and devices_ready and self._can_record
        )
        self.stop_button.setEnabled(recording)
        self.back_button.setEnabled(not finalising)
        self.refresh_devices_button.setEnabled(idle)
        self.title_edit.setEnabled(idle)
        self.microphone_combo.setEnabled(idle)
        self.loopback_combo.setEnabled(idle)

        if recording:
            self.indicator_label.setText("●  RECORDING")
            self.indicator_label.setObjectName("recordingActive")
        elif finalising:
            self.indicator_label.setText("UPLOADING")
            self.indicator_label.setObjectName("recordingFinalising")
        else:
            self.indicator_label.setText("Idle")
            self.indicator_label.setObjectName("recordingIdle")
        # Re-apply the style sheet after changing the dynamic object name.
        self.indicator_label.style().unpolish(self.indicator_label)
        self.indicator_label.style().polish(self.indicator_label)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    # -- navigation ------------------------------------------------------

    def _go_back(self) -> None:
        if self._state == "finalising":
            QMessageBox.information(
                self,
                "Finishing recording",
                "Please wait while the recording is uploaded to the server.",
            )
            return
        if self._state == "recording":
            answer = QMessageBox.question(
                self,
                "Discard recording",
                "Discard the current recording? The audio captured so far will "
                "be deleted and nothing is uploaded.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._discard_recording()
        self.cancelled.emit()

    def _discard_recording(self) -> None:
        work_dir = self._work_dir
        self._elapsed_timer.stop()
        if self._recorder is not None:
            try:
                self._recorder.cancel()
            except Exception:
                pass
        self._recorder = None
        self._work_dir = None
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        self._state = "idle"
        self._update_state_ui()

    def shutdown(self) -> None:
        """Discard an active recording when the application is closing."""

        if self._state == "recording":
            self._discard_recording()
        if worker_running(self._stop_worker):
            self._stop_worker.wait(10_000)
