"""Meeting history dashboard with local search."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import Settings
from storage import library as ops
from storage.library import MeetingLibrary

_STATUS_LABELS = {
    "recording": "Recording",
    "recorded": "Recorded",
    "transcribed": "Transcribed",
    "transcribing": "Transcribing...",
    "analyzing": "Analyzing...",
    "completed": "Complete",
    "failed": "Failed",
}

_STATUS_COLORS = {
    "recording": "#c0392b",
    "recorded": "#8a6d3b",
    "transcribing": "#2c6e9e",
    "analyzing": "#2c6e9e",
    "completed": "#2e7d32",
    "failed": "#c0392b",
}

_COLUMNS = ("Date", "Title", "Duration", "Status")


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "--"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "--"
    local = value.replace(tzinfo=timezone.utc).astimezone()
    return local.strftime("%Y-%m-%d %H:%M")


class DashboardScreen(QWidget):
    start_recording_requested = Signal()
    meeting_opened = Signal(str)
    meeting_deleted = Signal(str)
    library_requested = Signal()

    def __init__(
        self, library: MeetingLibrary, settings: Settings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.library = library
        self.settings = settings
        self.current_user: dict[str, Any] | None = None
        self._meetings_by_id: dict[str, dict[str, Any]] = {}

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.refresh)

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        heading = QLabel("Meeting history")
        heading.setObjectName("pageTitle")
        self.user_label = QLabel("")
        self.user_label.setObjectName("muted")
        titles.addWidget(heading)
        titles.addWidget(self.user_label)
        header.addLayout(titles)
        header.addStretch(1)

        self.new_recording_button = QPushButton("Start new recording")
        self.new_recording_button.setObjectName("primaryButton")
        self.new_recording_button.setMinimumHeight(38)
        self.new_recording_button.clicked.connect(self.start_recording_requested.emit)
        header.addWidget(self.new_recording_button)

        self.library_button = QPushButton("Library...")
        self.library_button.setToolTip(
            "View, open, or change the folder where meetings are stored."
        )
        self.library_button.clicked.connect(self.library_requested.emit)
        header.addWidget(self.library_button)
        layout.addLayout(header)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Search titles, summaries, transcripts, and action items..."
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _text: self._search_timer.start())
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.refresh_button)
        layout.addLayout(search_row)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.itemDoubleClicked.connect(lambda _item: self._open_selected())
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel(
            "No meetings yet. Start a new recording to create your first local meeting."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyState")
        layout.addWidget(self.empty_label)

        actions = QHBoxLayout()
        self.open_button = QPushButton("Open details")
        self.open_button.clicked.connect(self._open_selected)
        self.delete_button = QPushButton("Delete meeting")
        self.delete_button.setObjectName("dangerButton")
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addStretch(1)
        actions.addWidget(self.open_button)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)

        self._update_actions()

    def set_library(self, library: MeetingLibrary) -> None:
        """Point the screen at a different library and re-render.

        The caller decides whether the user stays logged in; this only keeps
        the visible state consistent with the new library.
        """

        self.library = library
        self._meetings_by_id.clear()
        self.table.setRowCount(0)
        if self.current_user is not None:
            self.refresh()

    def set_user(self, user: dict[str, Any]) -> None:
        self.current_user = user
        self.user_label.setText(
            f"Local profile: {user.get('display_name', 'Unknown')}"
        )
        self.search_edit.clear()
        self.refresh()

    def refresh(self) -> None:
        if self.current_user is None:
            return
        query = self.search_edit.text().strip() or None
        try:
            # Pick up meeting folders that were added or removed outside the
            # app (the index is rebuilt cheaply from the library folder).
            self.library.scan()
            meetings = ops.list_meetings(
                self.library,
                user_id=int(self.current_user["id"]),
                query=query,
                limit=self.settings.meeting_list_limit,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not load meetings",
                f"The local meeting history could not be read.\n\n{exc}",
            )
            return

        self._meetings_by_id = {meeting["id"]: meeting for meeting in meetings}
        self.table.setRowCount(len(meetings))
        for row, meeting in enumerate(meetings):
            date_item = QTableWidgetItem(format_datetime(meeting.get("started_at")))
            title_item = QTableWidgetItem(str(meeting.get("title", "Untitled")))
            duration_item = QTableWidgetItem(
                format_duration(meeting.get("duration_seconds"))
            )
            status = str(meeting.get("status", ""))
            status_item = QTableWidgetItem(
                _STATUS_LABELS.get(status, status.replace("_", " ").title())
            )
            colour = _STATUS_COLORS.get(status)
            if colour:
                status_item.setForeground(QColor(colour))

            for item in (date_item, title_item, duration_item, status_item):
                item.setData(Qt.ItemDataRole.UserRole, meeting["id"])
            self.table.setItem(row, 0, date_item)
            self.table.setItem(row, 1, title_item)
            self.table.setItem(row, 2, duration_item)
            self.table.setItem(row, 3, status_item)

        self.empty_label.setVisible(not meetings)
        self.table.setVisible(bool(meetings))
        self._update_actions()

    def _selected_meeting_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _update_actions(self) -> None:
        has_selection = self._selected_meeting_id() is not None
        self.open_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)

    def _open_selected(self) -> None:
        meeting_id = self._selected_meeting_id()
        if meeting_id:
            self.meeting_opened.emit(meeting_id)

    def _delete_selected(self) -> None:
        meeting_id = self._selected_meeting_id()
        if not meeting_id:
            return
        meeting = self._meetings_by_id.get(meeting_id, {})
        title = meeting.get("title", "this meeting")
        answer = QMessageBox.question(
            self,
            "Delete meeting",
            f"Delete '{title}'?\n\nThis removes the meeting record, transcript, "
            "analysis, and local recording files. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            ops.delete_meeting(self.library, meeting_id)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not delete meeting",
                f"The meeting could not be fully deleted.\n\n{exc}",
            )
            return

        self.meeting_deleted.emit(meeting_id)
        self.refresh()
