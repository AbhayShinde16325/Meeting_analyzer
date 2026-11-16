"""Profile selection screen.

A profile is a server-side label that groups meetings by the person using the
client.  Profiles are fetched from the server over HTTP; nothing is stored
locally except the session choice.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from client.api_client import ApiClientError, ApiConnectionError, TeamMeetingAIClient
from ui.qt_utils import wait_for_workers, worker_running


class _ProfilesWorker(QThread):
    """Fetch profiles (and optionally create one) without blocking the UI."""

    completed = Signal(list, object)  # users, error message or None

    def __init__(self, client: TeamMeetingAIClient, new_name: str | None = None) -> None:
        super().__init__()
        self.client = client
        self.new_name = new_name

    def run(self) -> None:
        try:
            if self.new_name:
                user = self.client.get_or_create_user(self.new_name)
                self.completed.emit([user], None)
            else:
                self.completed.emit(self.client.list_users(), None)
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit([], str(exc))
        except Exception as exc:  # unexpected - show something actionable
            self.completed.emit([], f"Unexpected error: {type(exc).__name__}")


class LoginScreen(QWidget):
    """Choose (or create) a profile, then continue to the dashboard."""

    profile_selected = Signal(object)  # user dict

    def __init__(self, client: TeamMeetingAIClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._worker: _ProfilesWorker | None = None
        # Creating a profile uses its own worker so a click is not swallowed
        # while the initial profile list is still loading.
        self._create_worker: _ProfilesWorker | None = None
        self._users: list[dict] = []
        self._build_ui()
        self.refresh_profiles()

    def set_client(self, client: TeamMeetingAIClient) -> None:
        self.client = client
        self.refresh_profiles()

    # -- UI -----------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 40, 48, 40)
        layout.setSpacing(16)

        title = QLabel("TeamMeetingAI")
        title.setObjectName("appTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel(
            "Meetings are stored on your team's meeting server.\n"
            "Recordings are transcribed through Sarvam (cloud) after upload."
        )
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setObjectName("subtitle")

        card = QFrame()
        card.setObjectName("card")
        card.setMaximumWidth(520)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 26, 28, 26)
        card_layout.setSpacing(12)

        profile_label = QLabel("Choose your profile")
        profile_label.setObjectName("sectionTitle")

        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumHeight(36)
        self.profile_combo.setToolTip("Select your profile or create a new one.")

        new_label = QLabel("Or create a new profile")
        new_label.setObjectName("muted")

        self.new_profile_edit = QLineEdit()
        self.new_profile_edit.setPlaceholderText("Your name")
        self.new_profile_edit.setToolTip("Enter your name for a new profile.")
        self.new_profile_edit.setMinimumHeight(36)
        self.new_profile_edit.returnPressed.connect(self._continue)

        self.continue_button = QPushButton("Continue")
        self.continue_button.setMinimumHeight(40)
        self.continue_button.setObjectName("primaryButton")
        self.continue_button.setToolTip("Continue to the meeting dashboard.")
        self.continue_button.clicked.connect(self._continue)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("errorText")
        self.error_label.setVisible(False)

        consent = QLabel(
            "Only record meetings with the consent of all participants and in "
            "line with your organization's policies. Audio is sent to the "
            "meeting server and to Sarvam's cloud service for transcription."
        )
        consent.setWordWrap(True)
        consent.setObjectName("consentText")

        card_layout.addWidget(profile_label)
        card_layout.addWidget(self.profile_combo)
        card_layout.addWidget(new_label)
        card_layout.addWidget(self.new_profile_edit)
        card_layout.addWidget(self.continue_button)
        card_layout.addWidget(self.error_label)
        card_layout.addSpacing(6)
        card_layout.addWidget(consent)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(12)
        layout.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

    # -- data ---------------------------------------------------------------

    def refresh_profiles(self) -> None:
        """Load the profile list from the server in the background."""

        if worker_running(self._worker):
            return
        self.continue_button.setEnabled(False)
        self._worker = _ProfilesWorker(self.client)
        self._worker.completed.connect(self._on_profiles_loaded)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_profiles_loaded(self, users: list, error: str | None) -> None:
        self.continue_button.setEnabled(True)
        self._users = list(users)
        current = self.profile_combo.currentData()
        self.profile_combo.clear()
        self.profile_combo.addItem("Select a profile...", None)
        if error:
            self._show_error(
                f"Could not load profiles from the server.\n{error}"
            )
            return
        for user in users:
            self.profile_combo.addItem(str(user["display_name"]), str(user["id"]))
        if current is not None:
            index = self.profile_combo.findData(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        if self.profile_combo.count() == 1:
            self.new_profile_edit.setFocus()
        else:
            self.profile_combo.setFocus()

    def _continue(self) -> None:
        if worker_running(self._create_worker):
            return
        name = self.new_profile_edit.text().strip()
        if not name:
            # Continuing with an existing profile selected in the combo box.
            selected_id = self.profile_combo.currentData()
            if selected_id:
                for user in self._users:
                    if str(user.get("id")) == str(selected_id):
                        self.profile_selected.emit(user)
                        return
            self._show_error("Choose a profile or enter a new name.")
            return
        self._show_error("")
        self.continue_button.setEnabled(False)
        self._create_worker = _ProfilesWorker(self.client, new_name=name)
        self._create_worker.completed.connect(self._on_continue_result)
        self._create_worker.finished.connect(self._create_worker.deleteLater)
        self._create_worker.start()

    def _on_continue_result(self, users: list, error: str | None) -> None:
        self.continue_button.setEnabled(True)
        if error:
            self._show_error(error)
            return
        if not users:
            self._show_error("Choose a profile or enter a new name.")
            return
        self.new_profile_edit.clear()
        self.profile_selected.emit(users[0])

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def shutdown(self) -> None:
        # Never let one of these QThreads be destroyed while it is running:
        # Qt aborts the process (fail-fast) in that case.
        wait_for_workers([self._worker, self._create_worker])
