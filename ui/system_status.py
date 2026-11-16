"""System status dialog: server storage, schema and backup health.

Reads ``GET /system/status`` from the server; shows what the operator needs
(database size, free disk space on the host, last nightly backup, schema
version).  Recording is only possible on the host machine, which the dialog
explains to remote clients.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from client.api_client import ApiClientError, ApiConnectionError, TeamMeetingAIClient
from ui.dashboard import format_datetime
from ui.qt_utils import worker_running


def _format_size(size: int | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# Three-times-headroom guidance from the operations plan: warn below 3x the
# current database size (a conservative proxy for the next backup cycle).
_HEADROOM_RATIO = 3.0


class _StatusWorker(QThread):
    completed = Signal(object, object)

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


class SystemStatusDialog(QDialog):
    """Server storage and backup health, refreshed on open."""

    def __init__(
        self, client: TeamMeetingAIClient, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.setWindowTitle("System status")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(10)

        self.summary_label = QLabel("Reading server status...")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("muted")
        layout.addWidget(self.detail_label)

        self.headroom_bar = QProgressBar()
        self.headroom_bar.setRange(0, 100)
        self.headroom_bar.setVisible(False)
        self.headroom_format_note = QLabel("")
        self.headroom_format_note.setObjectName("muted")
        self.headroom_format_note.setVisible(False)
        layout.addWidget(self.headroom_bar)
        layout.addWidget(self.headroom_format_note)

        self.backup_label = QLabel("")
        self.backup_label.setWordWrap(True)
        layout.addWidget(self.backup_label)

        note = QLabel(
            "Recordings and transcripts live in PostgreSQL on the meeting "
            "server; nightly backups run pg_dump (including Large Objects). "
            "Recording requires the host computer's microphone and speakers."
        )
        note.setWordWrap(True)
        note.setObjectName("consentText")
        layout.addWidget(note)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self.close)
        layout.addWidget(buttons)

        self._worker: _StatusWorker | None = None
        self._load()

    def _load(self) -> None:
        if worker_running(self._worker):
            return
        self._worker = _StatusWorker(self.client)
        self._worker.completed.connect(self._on_loaded)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_loaded(self, status: object, error: object) -> None:
        self._worker = None
        if error:
            self.summary_label.setText(f"Server status unavailable.\n\n{error}")
            return
        assert isinstance(status, dict)
        self._render(status)

    def _render(self, status: dict[str, Any]) -> None:
        db_size = int(status.get("db_size_bytes") or 0)
        free = int(status.get("disk_free_bytes") or 0)
        self.summary_label.setText(
            f"{status.get('meeting_count', 0)} meetings stored on the server, "
            f"database size {_format_size(db_size)}."
        )
        headroom = db_size * _HEADROOM_RATIO
        enough = free >= headroom
        self.detail_label.setText(
            f"Free disk space on the host: {_format_size(free)} — "
            + (
                "sufficient for the next backup cycle."
                if enough
                else f"below the recommended {_format_size(int(headroom))} "
                "headroom (3x database size). Free space or extend storage."
            )
        )
        ratio = min(100, int(free * 100 / headroom) if headroom else 100)
        self.headroom_bar.setValue(ratio)
        self.headroom_bar.setVisible(True)
        self.headroom_format_note.setVisible(True)
        self.headroom_format_note.setText(
            "Storage headroom (100% = 3x database size free)"
        )

        last_backup = status.get("last_backup") or {}
        if last_backup:
            backup_text = (
                f"Last backup: {last_backup.get('status', 'unknown')} at "
                f"{format_datetime(last_backup.get('finished_at'))}"
            )
            if last_backup.get("size_bytes"):
                backup_text += f" ({_format_size(last_backup.get('size_bytes'))})"
            if last_backup.get("error"):
                backup_text += f"\nError: {last_backup.get('error')}"
        else:
            backup_text = (
                "No backup has run yet. Nightly pg_dump is configured on the "
                "server; check the operations documentation."
            )
        self.backup_label.setText(backup_text)

    def shutdown(self) -> None:
        if worker_running(self._worker):
            self._worker.wait(5000)
