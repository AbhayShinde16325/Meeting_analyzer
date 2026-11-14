"""First-run dialog for choosing the meeting library directory.

Everything the application keeps — audio, transcripts, analysis, and local
profiles — lives inside the chosen library.  The selection is validated for
write access before it is accepted and is then persisted in the application
data directory.
"""

from __future__ import annotations

from pathlib import Path

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


class LibrarySetupDialog(QDialog):
    """Ask for (and validate) the meeting library folder."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chosen: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setWindowTitle("Choose your meeting library")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        heading = QLabel("Where should meetings be stored?")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        explanation = QLabel(
            "TeamMeetingAI keeps everything on this computer. Every meeting "
            "becomes a folder containing the recording (audio.wav), the "
            "transcript (transcript.md), and the meeting notes "
            "(analysis.md).\n\nChoose a folder where these meeting folders "
            "will live - for example a Documents subfolder. You can change "
            "this later by editing the library location in the application "
            "settings."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Choose a folder...")
        self.path_edit.setMinimumHeight(34)
        browse = QToolButton()
        browse.setText("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

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

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Choose the meeting library folder",
            self.path_edit.text() or str(Path.home()),
        )
        if chosen:
            self.path_edit.setText(chosen)
        self._show_error("")

    def _accept(self) -> None:
        raw = self.path_edit.text().strip()
        if not raw:
            self._show_error("Choose a folder for the meeting library.")
            return
        try:
            self._chosen = validate_library_dir(raw)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.accept()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def chosen_path(self) -> Path:
        """The validated library directory chosen by the user."""

        return self._chosen or Path()
