"""Local employee/profile selection screen.

TeamMeetingAI does not use a cloud account.  A profile is simply a local label
so meetings can be grouped by the person using this computer.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from storage import library as ops
from storage.library import MeetingLibrary


class LoginScreen(QWidget):
    profile_selected = Signal(object)

    def __init__(self, library: MeetingLibrary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self._build_ui()
        self.refresh_profiles()

    def set_library(self, library: MeetingLibrary) -> None:
        """Point the screen at a different library and refresh the list."""

        self.library = library
        self.refresh_profiles()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 40, 48, 40)
        layout.setSpacing(16)

        title = QLabel("TeamMeetingAI")
        title.setObjectName("appTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel(
            "Record and review meetings locally on this computer.\n"
            "No meeting audio or notes are uploaded anywhere."
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

        profile_label = QLabel("Choose your local profile")
        profile_label.setObjectName("sectionTitle")

        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumHeight(36)
        self.profile_combo.setToolTip("Select your local profile or create a new one.")

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
            "line with your organization's policies."
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

    def refresh_profiles(self) -> None:
        current = self.profile_combo.currentData()
        self.profile_combo.clear()
        self.profile_combo.addItem("Select a profile...", None)
        try:
            for user in ops.list_users(self.library):
                self.profile_combo.addItem(user["display_name"], user["id"])
        except Exception:
            self._show_error("Could not load local profiles.")
            return
        if current is not None:
            index = self.profile_combo.findData(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        if self.profile_combo.count() == 1:
            self.new_profile_edit.setFocus()
        else:
            self.profile_combo.setFocus()

    def _continue(self) -> None:
        name = self.new_profile_edit.text().strip()
        try:
            if name:
                user = ops.get_or_create_user(self.library, name)
            else:
                user_id = self.profile_combo.currentData()
                if user_id is None:
                    self._show_error("Choose a profile or enter a new name.")
                    return
                user = ops.get_user(self.library, int(user_id))
                if user is None:
                    self._show_error("That profile could not be found.")
                    self.refresh_profiles()
                    return
        except Exception as exc:
            self._show_error(str(exc) or "Could not open the local profile.")
            return

        self._show_error("")
        self.new_profile_edit.clear()
        self.profile_selected.emit(user)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
