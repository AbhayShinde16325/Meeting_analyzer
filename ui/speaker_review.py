"""Speaker review dialog: confirm, correct, or reject voice-match suggestions.

Voice similarity never names anyone by itself.  This dialog lists the
meeting's diarized speakers with the model's suggestions and lets the user:

* confirm a suggestion (or pick a different profile / create a new one),
* reject a label ("this speaker is nobody we track"),
* reset back to the model's suggestion,
* rename an existing profile.

Confirmed assignments are what the transcript view uses to display profile
names; the underlying profiles improve from confirmed meetings only.

Layout: the picker and the decision buttons are separate rows so nothing
overlaps at the minimum dialog width.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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

_NEW_PROFILE = "__new__"


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
        self._busy = False

        self.setWindowTitle("Review speakers")
        self.setMinimumWidth(720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        self.intro_label = QLabel(
            "Speakers below were separated by diarization. Voice matching "
            "only suggests a profile — a name appears in the transcript "
            "after you confirm it."
        )
        self.intro_label.setWordWrap(True)
        self.intro_label.setObjectName("muted")
        layout.addWidget(self.intro_label)

        # Row 1: the speakers table takes all available space.
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

        # Row 2: profile picker for the selected speaker (full width).
        picker_row = QHBoxLayout()
        picker_row.setSpacing(8)
        self.profile_combo_label = QLabel("Assign to profile:")
        picker_row.addWidget(self.profile_combo_label)
        self.profile_combo = QComboBox()
        self.profile_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.profile_combo.setMinimumContentsLength(24)
        picker_row.addWidget(self.profile_combo, 1)
        layout.addLayout(picker_row)

        # Row 3: decision buttons, one verb each, left-aligned.
        decision_row = QHBoxLayout()
        decision_row.setSpacing(8)
        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.setObjectName("primaryButton")
        self.confirm_button.setToolTip(
            "Attach this speaker's voice print to the selected profile and "
            "show that name in the transcript."
        )
        self.confirm_button.clicked.connect(self._confirm_assignment)
        decision_row.addWidget(self.confirm_button)

        self.reject_button = QPushButton("Reject")
        self.reject_button.setToolTip(
            "Mark this speaker as somebody we do not track; clears any match."
        )
        self.reject_button.clicked.connect(self._reject_assignment)
        decision_row.addWidget(self.reject_button)

        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip(
            "Undo your decision; back to the model's suggestion."
        )
        self.reset_button.clicked.connect(self._reset_assignment)
        decision_row.addWidget(self.reset_button)
        decision_row.addStretch(1)
        layout.addLayout(decision_row)

        self.hint_label = QLabel("Select a speaker to review its match.")
        self.hint_label.setWordWrap(True)
        self.hint_label.setObjectName("muted")
        layout.addWidget(self.hint_label)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

        # Row 4: profile management and close.  Voice profiling itself runs
        # automatically as part of transcription — no manual trigger here.
        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)
        self.rename_button = QPushButton("Rename profile...")
        self.rename_button.setToolTip("Change the display name of a profile.")
        self.rename_button.clicked.connect(self._rename_profile)
        controls_row.addWidget(self.rename_button)
        controls_row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self.close)
        controls_row.addWidget(buttons)
        layout.addLayout(controls_row)

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
        same_voice = speaker.get("same_voice_labels") or []
        candidates = speaker.get("candidates") or []
        if not candidates:
            if same_voice:
                return f"same voice as {same_voice[0]}"
            return "-"
        best = candidates[0]
        confidence = float(best.get("confidence") or 0.0)
        name = str(best.get("display_name", ""))
        return f"{name} ({confidence:.0%})"

    def _render_profiles(self) -> None:
        self.profile_combo.clear()
        self.profile_combo.addItem("Choose a profile...", "")
        for profile in self.profiles:
            self.profile_combo.addItem(
                str(profile.get("display_name", "")), str(profile.get("id"))
            )
        self.profile_combo.addItem("New profile...", _NEW_PROFILE)

    def _selected_speaker(self) -> dict[str, Any] | None:
        row = self.speakers_table.currentRow()
        if row < 0 or row >= len(self.speakers):
            return None
        return self.speakers[row]

    def _selected_profile_id(self) -> str | None:
        data = self.profile_combo.currentData()
        return str(data) if data else None

    # -- controls ------------------------------------------------------------

    def _update_controls(self) -> None:
        speaker = self._selected_speaker()
        editable = self._can_process and speaker is not None and not self._busy
        has_selection = speaker is not None
        self.profile_combo_label.setVisible(has_selection)
        self.profile_combo.setVisible(has_selection)
        self.confirm_button.setEnabled(editable)
        self.reject_button.setEnabled(editable)
        self.reset_button.setEnabled(
            editable and str(speaker.get("status") or "") in ("confirmed", "rejected")  # type: ignore[union-attr]
        )
        self.rename_button.setEnabled(
            self._can_process and bool(self.profiles) and not self._busy
        )
        if self._busy:
            return
        if speaker is None:
            self.hint_label.setText(
                "Select a speaker above to review its match."
                if self.speakers
                else "No diarized speakers in this transcript yet. Run "
                "transcription (and voice profiling) first."
            )
            return
        same_voice = speaker.get("same_voice_labels") or []
        if same_voice:
            twins = ", ".join(str(label) for label in same_voice)
            self.hint_label.setText(
                f"This label sounds like the same voice as {twins}. "
                "Diarization split one person into several labels — assign "
                "both labels to the same profile (or a new one) to merge "
                "them in the transcript."
            )
        else:
            self.hint_label.setText(
                "Confirm stores this speaker's voice print under the selected "
                "profile and shows that name in the transcript."
            )

    def _show_notice(self, message: str) -> None:
        self.notice_label.setText(message)
        self.notice_label.setVisible(bool(message))

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.speakers_table.setEnabled(not busy)
        self.profile_combo.setEnabled(not busy)
        self.confirm_button.setEnabled(not busy)
        self.reject_button.setEnabled(not busy)
        self.reset_button.setEnabled(not busy)
        self.rename_button.setEnabled(not busy)
        if message:
            self.hint_label.setText(message)
        if not busy:
            self._update_controls()

    # -- actions ---------------------------------------------------------------

    def _confirm_assignment(self) -> None:
        speaker = self._selected_speaker()
        if speaker is None or worker_running(self._action_worker):
            return
        profile_id = self._selected_profile_id()
        fields: dict[str, Any] = {"status": "confirmed"}
        if profile_id == _NEW_PROFILE:
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
        if profile_id and profile_id != _NEW_PROFILE:
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
        if not profile_id or profile_id == _NEW_PROFILE:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename profile", "New name:", text=current
        )
        new_name = (new_name or "").strip()
        if not ok or not new_name or new_name == current:
            return
        self._run_action("rename", profile_id=profile_id, display_name=new_name)

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
        self._load()

    def shutdown(self) -> None:
        for worker in (self._load_worker, self._action_worker):
            if worker_running(worker):
                worker.wait(5000)
