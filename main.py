"""TeamMeetingAI application entry point and composition root.

The desktop client talks to the team's meeting server over HTTP
(``client/api_client.py``).  It never touches PostgreSQL or the filesystem
library; recordings are captured locally on this machine and uploaded when
the user stops recording.

Startup order: the optional ``.env`` file is loaded first (it can carry
``TEAMMEETINGAI_SERVER_URL``), settings and logging are configured, the HTTP
client is built, and the server's availability is checked in the background so
the login screen can warn early when the meeting server is unreachable.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor, QCloseEvent, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
)

from client.api_client import (
    ApiClientError,
    ApiConnectionError,
    TeamMeetingAIClient,
    server_url_from_env,
)
from config import (
    APP_DISPLAY_NAME,
    APP_VERSION,
    Settings,
    ensure_directories,
    load_settings,
)
from ui.dashboard import DashboardScreen
from ui.login import LoginScreen
from ui.meeting_details import MeetingDetailsScreen
from ui.qt_utils import worker_running
from ui.recorder import RecordingScreen
from ui.system_status import SystemStatusDialog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


def _load_local_env(env_file: Path | None = None) -> bool:
    """Load optional .env file if present; return True if file was valid."""

    env_file = env_file if env_file is not None else (
        Path(__file__).parent / ".env"
    )
    if not env_file.is_file():
        return False
    import os
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in os.environ:
                # Existing env var found - don't override, but file is valid
                continue
            os.environ[key] = value
    except Exception:
        return False
    return True


def _configure_logging(settings: Settings) -> None:
    """Send operational logs to the app data directory.

    The log contains operational events and meeting IDs only - never audio,
    transcript text, passwords, tokens, or other sensitive content.
    """

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        handler = RotatingFileHandler(
            settings.data_dir / "teammeetingai.log",
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _apply_light_theme(app: QApplication) -> None:
    """Force a light, system-theme-independent appearance.

    The application has no dark mode: the stylesheet uses fixed colours and
    this installs a matching light palette (with the Fusion style, which fully
    honours palettes) so widgets that are not styled by the stylesheet cannot
    pick up a dark Windows theme.
    """

    app.setStyle("Fusion")

    palette = QPalette()
    window_text = QColor("#1e293b")
    palette.setColor(QPalette.ColorRole.Window, QColor("#f8fafc"))
    palette.setColor(QPalette.ColorRole.WindowText, window_text)
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f1f5f9"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#1e293b"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, window_text)
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, window_text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#1d4ed8"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#1d4ed8"))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor("#1e40af"))

    disabled = QPalette.ColorGroup.Disabled
    muted = QColor("#94a3b8")
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(disabled, role, muted)
    app.setPalette(palette)

    # The stylesheet is applied application-wide so dialogs created before
    # the main window (message boxes, file dialogs) are styled consistently.
    app.setStyleSheet(_load_stylesheet())


def _load_stylesheet(path: Path | None = None) -> str:
    """Read the stylesheet next to this file so the CWD does not matter."""

    path = path if path is not None else (
        Path(__file__).resolve().parent / "styles" / "app_stylesheet.qss"
    )
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Stylesheet not found at %s; continuing without it.", path)
        return ""


class _ServerCheckWorker(QThread):
    """Background server reachability/capability check at startup."""

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


def main() -> int:
    """Run the application. Returns the exit code."""

    _load_local_env()
    app = QApplication(sys.argv)
    _apply_light_theme(app)

    settings = load_settings()
    ensure_directories(settings)
    _configure_logging(settings)
    logger.info("TeamMeetingAI %s starting.", APP_VERSION)

    client = TeamMeetingAIClient(server_url_from_env())
    window = MainWindow(client, settings)
    window.show()
    return app.exec()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(
        self, client: TeamMeetingAIClient, settings: Settings
    ) -> None:
        super().__init__()
        self.client = client
        self.settings = settings
        self.current_user: dict[str, Any] | None = None
        self._server_check: _ServerCheckWorker | None = None
        self._status_dialog: SystemStatusDialog | None = None

        self.setWindowTitle(f"{APP_DISPLAY_NAME} {APP_VERSION}")
        self.resize(1180, 780)
        self.setMinimumSize(960, 620)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.login_screen = LoginScreen(client)
        self.dashboard_screen = DashboardScreen(client)
        self.recording_screen = RecordingScreen(client, settings)
        self.details_screen = MeetingDetailsScreen(client, settings)

        self.stack.addWidget(self.login_screen)
        self.stack.addWidget(self.dashboard_screen)
        self.stack.addWidget(self.recording_screen)
        self.stack.addWidget(self.details_screen)
        self.stack.setCurrentWidget(self.login_screen)

        self.login_screen.profile_selected.connect(self._on_profile_selected)
        self.dashboard_screen.start_recording_requested.connect(self._start_recording)
        self.dashboard_screen.meeting_opened.connect(self._open_meeting)
        self.dashboard_screen.meeting_deleted.connect(self._on_meeting_deleted)
        self.dashboard_screen.system_status_requested.connect(self._show_system_status)
        self.recording_screen.recording_saved.connect(self._on_recording_saved)
        self.recording_screen.cancelled.connect(self._show_dashboard)
        self.details_screen.back_requested.connect(self._show_dashboard)
        self.details_screen.meeting_updated.connect(self.dashboard_screen.refresh)

        self.statusBar().showMessage(
            f"Connecting to the meeting server at {client.base_url}..."
        )
        self._server_check = _ServerCheckWorker(client)
        self._server_check.completed.connect(self._on_server_check)
        self._server_check.finished.connect(self._server_check.deleteLater)
        self._server_check.start()

    # -- server capability ----------------------------------------------

    def _on_server_check(self, status: object, error: object) -> None:
        # The worker is deliberately kept referenced: ``completed`` fires from
        # inside ``run()``, so dropping the last reference here would let Python
        # destroy a still-running QThread, which Qt turns into a process abort
        # (fail-fast).  ``shutdown()``/``_detach_server_check`` release it.
        if error:
            self.statusBar().showMessage(
                f"Meeting server unreachable ({error}).", 10_000
            )
            return
        assert isinstance(status, dict)
        can_record = bool(status.get("can_record"))
        self.recording_screen.set_can_record(can_record)
        if can_record:
            self.statusBar().showMessage(
                "Ready. Recordings are uploaded to the server and transcribed "
                "through Sarvam (cloud)."
            )
        else:
            self.statusBar().showMessage(
                "Ready (view-only client). Recording runs on the meeting "
                "host computer."
            )

    # -- profile selection ----------------------------------------------

    def _on_profile_selected(self, user: dict[str, Any]) -> None:
        self.set_user(user)
        self.stack.setCurrentWidget(self.dashboard_screen)

    def set_user(self, user: dict[str, Any]) -> None:
        self.current_user = user
        self.client.set_profile(str(user.get("id")))
        self.dashboard_screen.set_user(user)
        self.recording_screen.set_user(user)
        self.details_screen.current_user = user

    # -- navigation ------------------------------------------------------

    def _start_recording(self) -> None:
        if self.current_user is None:
            return
        self.recording_screen.prepare_new()
        self.stack.setCurrentWidget(self.recording_screen)

    def _open_meeting(self, meeting_id: str) -> None:
        self.dashboard_screen.leave_screen()
        self.stack.setCurrentWidget(self.details_screen)
        self.details_screen.show_meeting(meeting_id)

    def _on_meeting_deleted(self, _meeting_id: str) -> None:
        self.statusBar().showMessage("Meeting deleted on the server.", 10_000)

    def _on_recording_saved(self, meeting_id: str) -> None:
        self.statusBar().showMessage(
            f"Recording uploaded ({meeting_id}). Transcription queued.", 10_000
        )
        self.dashboard_screen.refresh()
        self.dashboard_screen.refresh_status()
        self._show_dashboard()

    def _show_dashboard(self) -> None:
        self.stack.setCurrentWidget(self.dashboard_screen)
        self.dashboard_screen.refresh()

    def _show_system_status(self) -> None:
        if self._status_dialog is None:
            self._status_dialog = SystemStatusDialog(self.client, self)
        self._status_dialog.exec()

    # -- shutdown --------------------------------------------------------

    def shutdown(self) -> None:
        """Release background threads before this window is destroyed.

        Destroying a running ``QThread`` aborts the process (Qt fail-fast), so
        the startup server check is detached from the window first and every
        screen is asked to stop its own workers.  ``closeEvent`` and the test
        harness both go through here.
        """

        self._detach_server_check()
        self.recording_screen.shutdown()
        self.details_screen.shutdown()
        self.dashboard_screen.shutdown()
        self.login_screen.shutdown()

    def _detach_server_check(self) -> None:
        """Stop waiting on the startup check without killing a live thread."""

        worker = self._server_check
        self._server_check = None
        if worker is None or not worker_running(worker):
            return
        # Usually already finished (one fast HTTP call); give it a moment.
        worker.wait(1500)
        if worker_running(worker):
            # Hand ownership to the application so destroying this window
            # cannot delete a thread that is still running.
            app = QApplication.instance()
            if app is not None:
                worker.setParent(app)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Cancel in-flight work cleanly when the window is closed."""

        recording = self.recording_screen.is_recording
        processing = self.details_screen._processing
        if recording or processing:
            answer = QMessageBox.question(
                self,
                "Quit TeamMeetingAI",
                "A recording or a server job is still active.\n\n"
                "Quit anyway? An active recording will be discarded and the "
                "server job keeps running until it is cancelled.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.shutdown()
        event.accept()
        super().closeEvent(event)


if __name__ == "__main__":
    sys.exit(main())
