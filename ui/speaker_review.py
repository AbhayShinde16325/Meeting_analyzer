"""Speaker review dialog: confirm, correct, or reject voice-match suggestions.

Voice similarity never names anyone by itself.  This dialog lists the
meeting's diarized speakers with the model's suggestions and lets the user:

* confirm a suggestion (or pick a different profile / create a new one),
* reject a label ("this speaker is nobody we track"),
* reset back to the model's suggestion,
* rename an existing profile.

Confirmed assignments are what the transcript view uses to display profile
names; the underlying profiles improve from confirmed meetings only.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.api_client import ApiClientError, ApiConnectionError, TeamMeetingAIClient
from ui.qt_utils import worker_running


class _SpeakersWorker(QThread):
    """Fetch the meeting's speakers and the existing profiles."""

    completed = Signal(object, object, object)  # speakers, profiles, error

    def __init__(self, client: TeamMeetingAIClient, meeting_id: str) -> None:
        super().__init__()
        self.client = client
        self.meeting_id = meeting_id

    def run(self) -> None:
        try:
            speakers = self.client.list_meeting_speakers(self.meeting_id)
            profiles = self.client.list_speaker_profiles()
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit(None, None, str(exc))
        except Exception as exc:
            self.completed.emit(None, None, f"Unexpected error: {type(exc).__name__}")
        else:
            self.completed.emit(speakers, profiles, None)


class _SpeakerActionWorker(QThread):
    """One-shot mutation: assign/rename/create, off the UI thread."""

    completed = Signal(object, object)  # result, error or None

    def __init__(self, client: TeamMeetingAIClient, action: str, **kwargs: Any) -> None:
        super().__init__()
        self.client = client
        self.action = action
        self.kwargs = kwargs

    def run(self) -> None:
        try:
            if self.action == "assign":
                result = self.client.assign_meeting_speaker(
                    self.kwargs["meeting_id"],
                    self.kwargs["local_label"],
                    self.kwargs["fields"],
                )
            elif self.action == "rename":
                result = self.client.rename_speaker_profile(
                    self.kwargs["profile_id"], self.kwargs["display_name"]
                )
            elif self.action == "profile_job":
                result = self.client.request_profiling(self.kwargs["meeting_id"])
            else:  # pragma: no cover - guarded by caller
                raise ValueError(f"Unknown action {self.action!r}")
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, f"Unexpected error: {type(exc).__name__}: {exc}")
        else:
            self.completed.emit(result, None)


class SpeakerReviewDialog(QDialog):
    """Review and correct speaker-to-profile matches for one meeting."""

    def __init__(
        self,
        client: TeamMeetingAIClient,
        meeting_id: str,
        *,
        can_process: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.meeting_id = meeting_id
        self._can_process = can_process
        self.speakers: list[dict[str, Any]] = []
        self.profiles: list[dict[str, Any]] = []

        self.setWindowTitle("Review speakers")
        self.setMinimumWidth(640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(10)

        intro = QLabel(
            "Speakers below were separated by diarization. Voice matching "
            "only suggests a profile - a name appears in the transcript "
            "after you confirm it."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        layout.addWidget(intro)

        self.speakers_table = QTableWidget(0, 4)
        self.speakers_table.setHorizontalHeaderLabels(
            ["Speaker", "Speech", "Suggested match", "Status"]
        )
        self.speakers_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.speakers_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.speakers_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.speakers_table.verticalHeader().setVisible(False)
        header = self.speakers_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.speakers_table.itemSelectionChanged.connect(self._update_controls)
        layout.addWidget(self.speakers_table, 1)

        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setObjectName("muted")
        layout.addWidget(self.hint_label)

        editor = QHBoxLayout()
        self.profile_combo_label = QLabel("Assign to profile:")
        editor.addWidget(self.profile_combo_label)
        self.profile_combo_label.setVisible(False)
        self.profile_combo = _ProfileCombo()
        editor.addWidget(self.profile_combo, 1)
        self.profile_combo.setVisible(False)
        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.setObjectName("primaryButton")
        self.confirm_button.clicked.connect(self._confirm_assignment)
        editor.addWidget(self.confirm_button)
        self.reject_button = QPushButton("Not this person")
        self.reject_button.setToolTip(
            "Mark this speaker as somebody we do not track; clears any match."
        )
        self.reject_button.clicked.connect(self._reject_assignment)
        editor.addWidget(self.reject_button)
        self.reset_button = QPushButton("Reset suggestion")
        self.reset_button.setToolTip("Undo your decision; back to the model suggestion.")
        self.reset_button.clicked.connect(self._reset_assignment)
        editor.addWidget(self.reset_button)
        layout.addLayout(editor)

        controls = QHBoxLayout()
        self.rename_button = QPushButton("Rename profile...")
        self.rename_button.clicked.connect(self._rename_profile)
        controls.addWidget(self.rename_button)
        self.reprofile_button = QPushButton("Re-run voice profiling")
        self.reprofile_button.setToolTip(
            "Recompute voice prints for this meeting from its audio."
        )
        self.reprofile_button.clicked.connect(self._reprofile)
        controls.addWidget(self.reprofile_button)
        controls.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self.close)
        controls.addWidget(buttons)
        layout.addLayout(controls)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

        self.notice_label = QLabel("")
        self.notice_label.setObjectName("errorText")
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        layout.addWidget(self.notice_label)

        self._load_worker: _SpeakersWorker | None = None
        self._action_worker: _SpeakerActionWorker | None = None
        self._load()

    # -- loading -------------------------------------------------------------

    def _load(self) -> None:
        if worker_running(self._load_worker):
            return
        self._load_worker = _SpeakersWorker(self.client, self.meeting_id)
        self._load_worker.completed.connect(self._on_loaded)
        self._load_worker.finished.connect(self._load_worker.deleteLater)
        self._set_busy(True, "Loading speakers...")
        self._load_worker.start()

    def _on_loaded(self, speakers: object, profiles: object, error: object) -> None:
        self._load_worker = None
        self._set_busy(False)
        if error:
            self._show_notice(f"Could not load speakers: {error}")
            return
        assert isinstance(speakers, list) and isinstance(profiles, list)
        self.speakers = speakers
        self.profiles = profiles
        self._render_speakers()
        self._render_profiles()
        self._update_controls()

    # -- rendering -----------------------------------------------------------

    def _render_speakers(self) -> None:
        self.speakers_table.setRowCount(len(self.speakers))
        for row, speaker in enumerate(self.speakers):
            label = str(speaker.get("local_label", ""))
            suggested = self._suggested_name(speaker)
            speech = speaker.get("speech_seconds") or 0.0
            status = str(speaker.get("status") or "unknown")
            profile_name = speaker.get("profile_name")
            if status == "confirmed" and profile_name:
                status_text = f"Confirmed: {profile_name}"
            elif status == "rejected":
                status_text = "Marked as not tracked"
            elif speaker.get("has_embedding"):
                status_text = "Awaiting review"
            else:
                status_text = "No voice print"
            values = (label, f"{speech:.0f}s", suggested, status_text)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, label)
                self.speakers_table.setItem(row, column, item)
        self.speakers_table.resizeRowsToContents()

    @staticmethod
    def _suggested_name(speaker: dict[str, Any]) -> str:
        candidates = speaker.get("candidates") or []
        if not candidates:
            return "-"
        best = candidates[0]
        confidence = float(best.get("confidence") or 0.0)
        name = str(best.get("display_name", ""))
        return f"{name} ({confidence:.0%})"

    def _render_profiles(self) -> None:
        self.profile_combo.reload(self.profiles)

    def _selected_speaker(self) -> dict[str, Any] | None:
        row = self.speakers_table.currentRow()
        if row < 0 or row >= len(self.speakers):
            return None
        return self.speakers[row]

    # -- controls ------------------------------------------------------------

    def _update_controls(self) -> None:
        speaker = self._selected_speaker()
        editable = self._can_process and speaker is not None
        has_selection = speaker is not None
        self.profile_combo.setVisible(has_selection)
        self.profile_combo_label.setVisible(has_selection)
        self.confirm_button.setEnabled(editable)
        self.reject_button.setEnabled(editable)
        self.reset_button.setEnabled(
            editable
            and str(speaker.get("status") or "") in ("confirmed", "rejected")  # type: ignore[union-attr]
        )
        self.rename_button.setEnabled(self._can_process and bool(self.profiles))
        self.reprofile_button.setEnabled(self._can_process)
        if speaker is None:
            self.hint_label.setText("Select a speaker to review its match.")
        else:
            self.hint_label.setText(
                "Confirm stores this meeting's voice print under the selected "
                "profile and shows the name in the transcript."
            )

    def _show_notice(self, message: str) -> None:
        self.notice_label.setText(message)
        self.notice_label.setVisible(bool(message))

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.confirm_button.setEnabled(not busy)
        self.reject_button.setEnabled(not busy)
        self.speakers_table.setEnabled(not busy)
        if message:
            self.hint_label.setText(message)

    # -- actions ---------------------------------------------------------------

    def _selected_profile_id(self) -> str | None:
        return self.profile_combo.selected_profile_id()

    def _confirm_assignment(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None or worker_running(self._action_worker):
            return
        profile_id = self._selected_profile_id()
        fields: dict[str, Any] = {"status": "confirmed"}
        if profile_id == "__new__":
            name, ok = QInputDialog.getText(
                self, "New speaker profile", "Name for the new profile:"
            )
            name = (name or "").strip()
            if not ok or not name:
                return
            fields["new_profile_name"] = name
        elif profile_id:
            fields["profile_id"] = profile_id
        else:
            QMessageBox.information(
                self, "No profile selected", "Choose a profile or create a new one."
            )
            return
        self._run_action(
            "assign", meeting_id=self.meeting_id,
            local_label=str(speaker["local_label"]), fields=fields,
        )

    def _reject_assignment(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None or worker_running(self._action_worker):
            return
        self._run_action(
            "assign",
            meeting_id=self.meeting_id,
            local_label=str(speaker["local_label"]),
            fields={"status": "rejected"},
        )

    def _reset_assignment(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None or worker_running(self._action_worker):
            return
        self._run_action(
            "assign",
            meeting_id=self.meeting_id,
            local_label=str(speaker["local_label"]),
            fields={"status": "suggested"},
        )

    def _rename_profile(self) -> None:
        if worker_running(self._action_worker) or not self.profiles:
            return
        profile_id = self._selected_profile_id()
        current = None
        if profile_id and profile_id != "__new__":
            for profile in self.profiles:
                if str(profile.get("id")) == profile_id:
                    current = str(profile.get("display_name", ""))
                    break
        if current is None:
            names = [str(p.get("display_name", "")) for p in self.profiles]
            current, ok = QInputDialog.getItem(
                self, "Rename profile", "Profile:", names, 0, False
            )
            if not ok:
                return
            for profile in self.profiles:
                if str(profile.get("display_name", "")) == current:
                    profile_id = str(profile["id"])
                    break
        if not profile_id or profile_id == "__new__":
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename profile", "New name:", text=current
        )
        new_name = (new_name or "").strip()
        if not ok or not new_name or new_name == current:
            return
        self._run_action("rename", profile_id=profile_id, display_name=new_name)

    def _reprofile(self) -> None:
        if worker_running(self._action_worker):
            return
        self._run_action("profile_job", meeting_id=self.meeting_id)

    def _run_action(self, action: str, **kwargs: Any) -> None:
        if worker_running(self._action_worker):
            return
        self._action_worker = _SpeakerActionWorker(self.client, action, **kwargs)
        self._action_worker.completed.connect(
            lambda result, error, action=action: self._on_action_done(action, error)
        )
        self._action_worker.finished.connect(self._action_worker.deleteLater)
        self._set_busy(True, "Saving on the server...")
        self._action_worker.start()

    def _on_action_done(self, action: str, error: object) -> None:
        self._action_worker = None
        if error:
            self._set_busy(False)
            self._show_notice(str(error))
            return
        if action == "profile_job":
            self._set_busy(False)
            self._show_notice(
                "Voice profiling queued. The speaker list updates when the "
                "job finishes."
            )
            return
        self._load()

    def shutdown(self) -> None:
        for worker in (self._load_worker, self._action_worker):
            if worker_running(worker):
                worker.wait(5000)


class _ProfileCombo(QWidget):
    """Profile picker including an explicit 'create new profile' entry."""

    NEW = "__new__"

    def __init__(self, parent: QWidget | None = None) -> None:
        from PySide6.QtWidgets import QComboBox

        super().__init__(parent)
        self._combo = QComboBox(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._combo)

    def reload(self, profiles: list[dict[str, Any]]) -> None:
        self._combo.clear()
        self._combo.addItem("Choose a profile...", "")
        for profile in profiles:
            self._combo.addItem(
                str(profile.get("display_name", "")), str(profile.get("id"))
            )
        self._combo.addItem("New profile...", self.NEW)

    def selected_profile_id(self) -> str | None:
        data = self._combo.currentData()
        return str(data) if data else None
