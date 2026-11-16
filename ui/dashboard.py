"""Meeting history dashboard backed by the team server.

Every row comes from ``GET /meetings`` (title, status, duration).  Deletion is
a two-step, confirmed, permanent action: the first press arms the button, the
second press (within a few seconds) deletes the meeting and every attached
file, transcript and summary on the server.  There is no soft delete and no
undo.

A status footer shows the server's storage/backup health, refreshed
periodically while the dashboard is visible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
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

from client.api_client import (
    ApiClientError,
    ApiConnectionError,
    TeamMeetingAIClient,
)
from ui.qt_utils import worker_running

_STATUS_LABELS = {
    "recording": "Recording",
    "recorded": "Recorded",
    "transcribed": "Transcribed",
    "analyzing": "Analyzing...",
    "completed": "Complete",
    "failed": "Failed",
}

_STATUS_COLORS = {
    "recorded": "#8a6d3b",
    "transcribed": "#2c6e9e",
    "analyzing": "#2c6e9e",
    "completed": "#2e7d32",
    "failed": "#c0392b",
}

_COLUMNS = ("Date", "Title", "Duration", "Status")

_ARM_TIMEOUT_MS = 6000


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "--"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_datetime(value: str | datetime | None) -> str:
    if value is None:
        return "--"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone()
    return local.strftime("%Y-%m-%d %H:%M")


def _format_size(size: int | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class _MeetingsWorker(QThread):
    """Fetch the meeting list (and optionally delete) off the UI thread."""

    completed = Signal(list, object)  # meetings, error or None

    def __init__(
        self,
        client: TeamMeetingAIClient,
        query: str | None,
        limit: int,
        *,
        delete_meeting_id: str | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.query = query
        self.limit = limit
        self.delete_meeting_id = delete_meeting_id

    def run(self) -> None:
        try:
            if self.delete_meeting_id is not None:
                self.client.delete_meeting(self.delete_meeting_id)
            meetings = self.client.list_meetings(query=self.query, limit=self.limit)
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit([], str(exc))
        except Exception as exc:
            self.completed.emit([], f"Unexpected error: {type(exc).__name__}: {exc}")
        else:
            self.completed.emit(meetings, None)


class _StatusWorker(QThread):
    """Fetch ``/system/status`` off the UI thread."""

    completed = Signal(object, object)  # status dict, error or None

    def __init__(self, client: TeamMeetingAIClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            self.completed.emit(self.client.system_status(), None)
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, f"Unexpected error: {type(exc).__name__}")


class DashboardScreen(QWidget):
    start_recording_requested = Signal()
    meeting_opened = Signal(str)
    meeting_deleted = Signal(str)
    system_status_requested = Signal()

    def __init__(
        self, client: TeamMeetingAIClient, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.current_user: dict[str, Any] | None = None
        self._meetings_by_id: dict[str, dict[str, Any]] = {}
        self._list_worker: _MeetingsWorker | None = None
        self._status_worker: _StatusWorker | None = None
        self._delete_worker: _MeetingsWorker | None = None
        self._delete_armed_for: str | None = None

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.refresh)

        # Server health footer refresh (cheap; only while visible).
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(30_000)
        self._status_timer.timeout.connect(self.refresh_status)

        # Processing meetings (queued jobs on the server) refresh faster.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(5000)
        self._poll_timer.timeout.connect(self._poll_processing)

        self._build_ui()

    # -- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 16)
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

        self.status_button = QPushButton("System status...")
        self.status_button.setToolTip(
            "Server storage, backup health and recording capability."
        )
        self.status_button.clicked.connect(self.system_status_requested.emit)
        header.addWidget(self.status_button)
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
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.itemDoubleClicked.connect(lambda _item: self._open_selected())
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel(
            "No meetings yet. Start a new recording to create your first meeting."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyState")
        layout.addWidget(self.empty_label)

        actions = QHBoxLayout()
        self.open_button = QPushButton("Open details")
        self.open_button.clicked.connect(self._open_selected)
        self.delete_button = QPushButton("Delete meeting")
        self.delete_button.setObjectName("dangerButton")
        self.delete_button.setToolTip(
            "Permanent deletion: press once to arm, press again to confirm."
        )
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addStretch(1)
        actions.addWidget(self.open_button)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._update_actions()

    # -- data ------------------------------------------------------------

    def set_user(self, user: dict[str, Any]) -> None:
        self.current_user = user
        self.user_label.setText(f"Profile: {user.get('display_name', 'Unknown')}")
        self.search_edit.clear()
        self._disarm_delete()
        self.refresh()
        self.refresh_status()
        self._status_timer.start()

    def leave_screen(self) -> None:
        self._status_timer.stop()
        self._poll_timer.stop()
        self._disarm_delete()

    def refresh(self) -> None:
        if self.current_user is None:
            return
        if worker_running(self._list_worker):
            return
        query = self.search_edit.text().strip() or None
        self._list_worker = _MeetingsWorker(self.client, query, 200)
        self._list_worker.completed.connect(self._on_meetings_loaded)
        self._list_worker.finished.connect(self._list_worker.deleteLater)
        self._list_worker.start()

    def _on_meetings_loaded(self, meetings: list, error: str | None) -> None:
        self._list_worker = None
        if error:
            QMessageBox.critical(
                self,
                "Could not load meetings",
                f"The meeting server could not be reached.\n\n{error}",
            )
            return
        self._meetings_by_id = {str(m["id"]): m for m in meetings}
        self.table.setRowCount(len(meetings))
        processing = False
        for row, meeting in enumerate(meetings):
            date_item = QTableWidgetItem(
                format_datetime(meeting.get("recorded_at"))
            )
            title_item = QTableWidgetItem(str(meeting.get("title", "Untitled")))
            duration_seconds = None
            if meeting.get("duration_ms"):
                duration_seconds = meeting["duration_ms"] / 1000.0
            duration_item = QTableWidgetItem(format_duration(duration_seconds))
            status = str(meeting.get("status", ""))
            status_item = QTableWidgetItem(
                _STATUS_LABELS.get(status, status.replace("_", " ").title())
            )
            colour = _STATUS_COLORS.get(status)
            if colour:
                status_item.setForeground(QColor(colour))
            if status in ("recorded", "transcribed", "analyzing"):
                processing = True

            for item in (date_item, title_item, duration_item, status_item):
                item.setData(Qt.ItemDataRole.UserRole, str(meeting["id"]))
            self.table.setItem(row, 0, date_item)
            self.table.setItem(row, 1, title_item)
            self.table.setItem(row, 2, duration_item)
            self.table.setItem(row, 3, status_item)

        self.empty_label.setVisible(not meetings)
        self.table.setVisible(bool(meetings))
        self._update_actions()
        self._poll_timer.stop()
        if processing:
            self._poll_timer.start()

    def _poll_processing(self) -> None:
        """Refresh the list while any meeting is queued on the server."""

        if self.isVisible():
            self.refresh()

    # -- server status footer --------------------------------------------

    def refresh_status(self) -> None:
        if worker_running(self._status_worker):
            return
        self._status_worker = _StatusWorker(self.client)
        self._status_worker.completed.connect(self._on_status_loaded)
        self._status_worker.finished.connect(self._status_worker.deleteLater)
        self._status_worker.start()

    def _on_status_loaded(self, status: object, error: object) -> None:
        self._status_worker = None
        if error:
            self.status_label.setText(f"Server status unavailable ({error})")
            return
        assert isinstance(status, dict)
        last_backup = status.get("last_backup") or {}
        if last_backup:
            backup_text = (
                f"backup {last_backup.get('status', 'unknown')} "
                f"{format_datetime(last_backup.get('finished_at'))}"
            )
        else:
            backup_text = "no backup recorded yet"
        self.status_label.setText(
            f"Server: {status.get('meeting_count', 0)} meetings, "
            f"{_format_size(status.get('db_size_bytes'))} database, "
            f"{_format_size(status.get('disk_free_bytes'))} free on host, "
            f"{backup_text}  |  schema {status.get('schema_version', '?')}"
        )

    # -- selection / actions ----------------------------------------------

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
        title = str(meeting.get("title", "this meeting"))

        if self._delete_armed_for != meeting_id:
            self._arm_delete(meeting_id, title)
            return

        self._disarm_delete()
        answer = QMessageBox.question(
            self,
            "Delete meeting permanently",
            f"Delete '{title}' permanently?\n\nThis removes the meeting, its "
            "audio, transcript, summary and attachments from the server. "
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_delete(meeting_id)

    def _arm_delete(self, meeting_id: str, title: str) -> None:
        """First press: arm the button and show what will happen."""

        self._delete_armed_for = meeting_id
        self.delete_button.setText("Confirm delete?")
        self.delete_button.setToolTip(
            "Press again within a few seconds to delete permanently."
        )
        self.status_label.setText(
            f"Delete armed for '{title}'. Press the delete button again to "
            "confirm, or wait to cancel."
        )
        # Context-object overload: the timer is dropped when this screen is
        # destroyed instead of firing against deleted C++ widgets.
        QTimer.singleShot(
            _ARM_TIMEOUT_MS, self, lambda: self._disarm_delete(meeting_id)
        )

    def _disarm_delete(self, meeting_id: str | None = None) -> None:
        """Disarm, but never clobber a re-arm for a *different* meeting."""

        if meeting_id is not None and self._delete_armed_for != meeting_id:
            return
        self._delete_armed_for = None
        self.delete_button.setText("Delete meeting")
        self.delete_button.setToolTip(
            "Permanent deletion: press once to arm, press again to confirm."
        )
        if self.isVisible():
            self.status_label.setText("")

    def _run_delete(self, meeting_id: str) -> None:
        if worker_running(self._delete_worker):
            return
        self.open_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.status_label.setText("Deleting the meeting on the server...")
        self._delete_worker = _MeetingsWorker(
            self.client, None, 1, delete_meeting_id=meeting_id
        )
        self._delete_worker.completed.connect(
            lambda meetings, error, mid=meeting_id: self._on_deleted(mid, error)
        )
        self._delete_worker.finished.connect(self._delete_worker.deleteLater)
        self._delete_worker.start()

    def _on_deleted(self, meeting_id: str, error: str | None) -> None:
        self._delete_worker = None
        if error:
            QMessageBox.critical(
                self,
                "Could not delete meeting",
                f"The meeting could not be deleted on the server.\n\n{error}",
            )
        else:
            self.meeting_deleted.emit(meeting_id)
        self._update_actions()
        self.refresh()
        self.refresh_status()

    def shutdown(self) -> None:
        self._status_timer.stop()
        self._poll_timer.stop()
        for worker in (self._list_worker, self._status_worker, self._delete_worker):
            if worker_running(worker):
                worker.wait(5000)
