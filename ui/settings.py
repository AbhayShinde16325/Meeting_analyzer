"""Library settings dialog: view, open, and change the meeting library.

The dialog only *chooses* a folder; applying the change (guards, persistence,
hot-swap into the screens) is done by ``MainWindow._apply_library_change`` so
all safety checks live in one place.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config import validate_library_dir
from storage.library import count_library_meetings


class LibrarySettingsDialog(QDialog):
    """Show the current meeting library and allow choosing another one."""

    def __init__(
        self, current_dir: Path | str | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._current: Path | None = (
            Path(current_dir) if current_dir is not None else None
        )
        self._chosen: Path | None = None
        self._build_ui()
        self._update_preview()

    def _build_ui(self) -> None:
        self.setWindowTitle("Meeting library")
        self.setMinimumWidth(580)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        heading = QLabel("Meeting library")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        explanation = QLabel(
            "Every meeting is stored as a folder inside the library "
            "(recording, transcript.md, analysis.md, meeting.json).\n\n"
            "Changing the library only points the app at another folder - "
            "existing meetings are not moved. To relocate your meetings, "
            "move the folder in Explorer and then choose the new location "
            "here."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        current_row = QHBoxLayout()
        current_label = QLabel("Current library")
        current_label.setObjectName("muted")
        self.current_label = QLabel(
            str(self._current) if self._current is not None else "(not set)"
        )
        self.current_label.setWordWrap(True)
        self.open_button = QPushButton("Open folder")
        self.open_button.setEnabled(
            self._current is not None and self._current.is_dir()
        )
        self.open_button.clicked.connect(self._open_current)
        current_row.addWidget(current_label)
        current_row.addWidget(self.current_label, 1)
        current_row.addWidget(self.open_button)
        layout.addLayout(current_row)

        change_label = QLabel("Change to a different folder")
        change_label.setObjectName("sectionTitle")
        layout.addWidget(change_label)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Choose a folder...")
        self.path_edit.setMinimumHeight(34)
        if self._current is not None:
            self.path_edit.setText(str(self._current))
        self.path_edit.textChanged.connect(lambda _text: self._update_preview())
        browse = QToolButton()
        browse.setText("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        self.info_label = QLabel("")
        self.info_label.setObjectName("muted")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("errorText")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use this folder")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open_current(self) -> None:
        if self._current is not None and self._current.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._current)))

    def _browse(self) -> None:
        start = self.path_edit.text().strip() or (
            str(self._current) if self._current is not None else str(Path.home())
        )
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the meeting library folder", start
        )
        if chosen:
            self.path_edit.setText(chosen)

    def _update_preview(self) -> None:
        raw = self.path_edit.text().strip()
        if not raw:
            self.info_label.setText("")
            return
        if self._current is not None and Path(raw) == self._current:
            self.info_label.setText("This is the current library.")
            return
        count = count_library_meetings(raw)
        if count:
            self.info_label.setText(
                f"This folder already contains {count} "
                f"meeting{'s' if count != 1 else ''}."
            )
        else:
            self.info_label.setText(
                "This folder is empty (or does not exist yet) - it will "
                "become a new library."
            )

    def _accept(self) -> None:
        raw = self.path_edit.text().strip()
        if not raw:
            self._show_error("Choose a folder for the meeting library.")
            return
        try:
            chosen = validate_library_dir(raw)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        if self._current is not None and chosen == self._current:
            self._show_error("That is already the current library.")
            return
        self._show_error("")
        self._chosen = chosen
        self.accept()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def chosen_path(self) -> Path:
        """The validated library directory chosen by the user."""

        return self._chosen or Path()
