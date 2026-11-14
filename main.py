"""TeamMeetingAI application entry point and composition root.

The application is local-first and file-based: every meeting lives in a folder
inside a user-chosen **library directory** (audio WAVs, ``transcript.md``,
``analysis.md``, and ``meeting.json``).  Transcription always runs locally
through Faster-Whisper (``ai/faster_whisper_provider.py``).  Meeting analysis
is selected at startup with ``TEAMMEETINGAI_ANALYSIS_BACKEND``:

* ``none`` (default) - transcription only; meetings stop at status
  ``transcribed``.
* ``groq`` - cloud analysis via the Groq API, gated behind
  ``TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS=1`` because the transcript leaves the
  computer.

Startup order matters: the optional ``.env`` file is loaded first, then
settings are read, then the meeting library is opened (running the one-time
legacy-SQLite migration if needed), and finally the transcription/analysis
providers are built so a misconfigured backend fails visibly at startup
instead of mid-meeting.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor, QCloseEvent, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
)

from ai.provider_factory import (
    ProviderConfigurationError,
    build_analysis_provider,
    build_transcription_provider,
    describe_analysis_mode,
)
from ai.summarization import (
    AnalysisProvider,
    MeetingSummarizer,
    SummarizationError,
)
from ai.transcription import (
    NoSpeechError,
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionService,
)
from config import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_VERSION,
    Settings,
    ensure_directories,
    load_settings,
    save_library_dir,
    validate_library_dir,
)
from storage import library as ops
from storage.library import LibraryError, MeetingLibrary
from storage.migration import MigrationResult, migrate_legacy_sqlite
from ui.dashboard import DashboardScreen
from ui.library_setup import LibrarySetupDialog
from ui.login import LoginScreen
from ui.meeting_details import MeetingDetailsScreen
from ui.recorder import RecordingScreen
from ui.settings import LibrarySettingsDialog

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
    pick up a dark Windows theme.  This prevents dark text on dark backgrounds
    in the transcript/overview panes and any other unstyled control.
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
    # the main window (first-run library chooser, message boxes, file
    # dialogs) are styled consistently with the rest of the UI.
    app.setStyleSheet(_STYLESHEET)


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


def _build_providers(
    settings: Settings,
) -> tuple[TranscriptionProvider, AnalysisProvider | None]:
    """Build the transcription and analysis providers for *settings*.

    Raises :class:`ProviderConfigurationError` when the configured analysis
    backend cannot be used; ``main()`` surfaces that at startup.
    """

    return (
        build_transcription_provider(settings),
        build_analysis_provider(settings),
    )


def _migrate_legacy_if_present(
    settings: Settings, library: MeetingLibrary
) -> MigrationResult | None:
    """Run the one-time legacy SQLite migration; never fail the startup."""

    try:
        result = migrate_legacy_sqlite(settings, library)
    except Exception:
        logger.exception("Legacy database migration failed; continuing without it.")
        return None
    if result.migrated or result.errors:
        logger.info(
            "Legacy migration finished: %s migrated, %s skipped, %s error(s).",
            result.migrated,
            result.skipped,
            len(result.errors),
        )
    return result


def main() -> int:
    """Run the application. Returns the exit code."""

    _load_local_env()
    app = QApplication(sys.argv)
    _apply_light_theme(app)

    settings = load_settings()
    ensure_directories(settings)
    _configure_logging(settings)
    logger.info("TeamMeetingAI %s starting.", APP_VERSION)

    # --- library directory -------------------------------------------------
    # First launch asks for the location.  Subsequent launches read the saved
    # value from ``config.json`` (or the ``TEAMMEETINGAI_LIBRARY_DIR``
    # environment variable, which always wins).
    if settings.library_dir is None:
        dialog = LibrarySetupDialog()
        if dialog.exec() != LibrarySetupDialog.DialogCode.Accepted:
            return 1
        library_dir = validate_library_dir(dialog.chosen_path())
        save_library_dir(settings.data_dir, library_dir)
        settings = dataclasses.replace(settings, library_dir=library_dir)

    # --- meeting library ---------------------------------------------------
    try:
        lib = ops.MeetingLibrary(settings)
    except LibraryError as exc:
        logger.error("Meeting library unavailable: %s", exc)
        QMessageBox.critical(None, "Meeting library unavailable", str(exc))
        return 1

    _migrate_legacy_if_present(settings, lib)

    # --- AI providers (fail visibly at startup, never mid-meeting) ---------
    try:
        transcription_provider, analysis_provider = _build_providers(settings)
    except ProviderConfigurationError as exc:
        logger.error("Analysis backend configuration error: %s", exc)
        QMessageBox.critical(None, "Configuration error", str(exc))
        return 1

    # --- main window -------------------------------------------------------
    window = MainWindow(
        lib,
        settings,
        transcription_provider=transcription_provider,
        analysis_provider=analysis_provider,
    )
    window.show()
    return app.exec()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(
        self,
        library: MeetingLibrary,
        settings: Settings,
        *,
        transcription_provider: TranscriptionProvider | None = None,
        analysis_provider: AnalysisProvider | None = None,
    ) -> None:
        super().__init__()
        self.library = library
        self.settings = settings
        self.current_user: dict[str, Any] | None = None
        self._worker: MeetingProcessingWorker | None = None

        self.transcription_service = TranscriptionService(
            settings, provider=transcription_provider
        )
        self.summarizer = MeetingSummarizer(settings, provider=analysis_provider)
        self._analysis_available = analysis_provider is not None

        self.setWindowTitle(f"{APP_DISPLAY_NAME} {APP_VERSION}")
        self.resize(1180, 780)
        self.setMinimumSize(960, 620)
        self.setStyleSheet(_STYLESHEET)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.login_screen = LoginScreen(library)
        self.dashboard_screen = DashboardScreen(library, settings)
        self.recording_screen = RecordingScreen(library, settings)
        self.details_screen = MeetingDetailsScreen(
            library,
            settings,
            analysis_available=self._analysis_available,
            cloud_analysis=bool(getattr(analysis_provider, "is_cloud", False)),
        )

        self.stack.addWidget(self.login_screen)
        self.stack.addWidget(self.dashboard_screen)
        self.stack.addWidget(self.recording_screen)
        self.stack.addWidget(self.details_screen)
        self.stack.setCurrentWidget(self.login_screen)

        self.login_screen.profile_selected.connect(self._on_profile_selected)
        self.dashboard_screen.start_recording_requested.connect(self._start_recording)
        self.dashboard_screen.meeting_opened.connect(self._open_meeting)
        self.dashboard_screen.meeting_deleted.connect(self._on_meeting_deleted)
        self.dashboard_screen.library_requested.connect(self._change_library)
        self.recording_screen.recording_saved.connect(self._on_recording_saved)
        self.recording_screen.cancelled.connect(self._show_dashboard)
        self.details_screen.back_requested.connect(self._show_dashboard)
        self.details_screen.transcribe_requested.connect(self._transcribe_meeting)
        self.details_screen.analysis_requested.connect(self._analyse_meeting)
        self.details_screen.cancel_requested.connect(self._cancel_processing)

        if getattr(analysis_provider, "is_cloud", False):
            self.statusBar().showMessage(
                "Ready. Cloud analysis is enabled: transcripts are sent to an "
                "external service. Audio stays on this computer."
            )
        else:
            self.statusBar().showMessage(
                "Ready. " + describe_analysis_mode(settings, analysis_provider) + "."
            )

    # -- profile selection --------------------------------

    def _on_profile_selected(self, user: dict[str, Any]) -> None:
        self.stack.setCurrentWidget(self.dashboard_screen)
        self.set_user(user)

    def set_user(self, user: dict[str, Any]) -> None:
        self.current_user = user
        self.dashboard_screen.set_user(user)
        self.recording_screen.set_user(user)
        self.details_screen.set_user(user)

    # -- navigation ---------------------------------------

    def _change_library(self) -> None:
        """Show the library dialog and switch libraries if accepted."""

        dialog = LibrarySettingsDialog(self.library.library_dir, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen_path()
        if not str(chosen).strip():
            return
        self._apply_library_change(chosen)

    def _apply_library_change(self, new_dir: Path | str) -> bool:
        """Point the whole application at a different meeting library.

        Guards refuse the switch while a recording or processing task is
        running.  The new library is opened and validated first; ``config.json``
        is only updated after every screen was successfully re-pointed, so a
        failure never leaves the app and its settings in disagreement.
        """

        if self.recording_screen._state != "idle":
            QMessageBox.warning(
                self,
                "Cannot change the library",
                "A recording is in progress. Stop or discard it before "
                "changing the meeting library.",
            )
            return False
        if self._worker is not None and self._worker.isRunning():
            # No modal here: a blocking dialog would freeze the UI thread
            # while processing runs; the status bar carries the message.
            self.statusBar().showMessage(
                "Cannot change the library: meeting processing is running. "
                "Cancel it or wait for it to finish first.",
                10_000,
            )
            return False

        try:
            resolved = validate_library_dir(new_dir)
        except ValueError as exc:
            QMessageBox.critical(self, "Invalid library folder", str(exc))
            return False

        candidate_settings = dataclasses.replace(self.settings, library_dir=resolved)
        try:
            new_library = MeetingLibrary(candidate_settings)
        except (LibraryError, OSError) as exc:
            QMessageBox.critical(self, "Could not open the new library", str(exc))
            return False

        try:
            save_library_dir(self.settings.data_dir, resolved)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not save library directory",
                f"Could not save the new library location.\n{exc}",
            )
            return False

        self.settings = candidate_settings
        self.library = new_library
        self.login_screen.set_library(new_library)
        self.dashboard_screen.set_library(new_library)
        self.recording_screen.set_library(new_library, candidate_settings)
        self.details_screen.set_library(new_library)

        # Keep the session when the same profile exists in the new library;
        # otherwise return to the profile-selection screen.
        matched: dict[str, Any] | None = None
        if self.current_user is not None:
            name = str(self.current_user.get("display_name", "")).lower()
            for profile in new_library.list_users():
                if str(profile.get("display_name", "")).lower() == name:
                    matched = profile
                    break

        if matched is None:
            self.current_user = None
            self.dashboard_screen.current_user = None
            self.recording_screen.current_user = None
            self.details_screen.current_user = None
            self.stack.setCurrentWidget(self.login_screen)
        else:
            self.set_user(matched)
            self.dashboard_screen.refresh()
            self.stack.setCurrentWidget(self.dashboard_screen)
        return True

    # -- recording ----------------------------------------

    def _start_recording(self) -> None:
        if self.current_user is None:
            return
        self.recording_screen.prepare_new()
        self.stack.setCurrentWidget(self.recording_screen)

    # -- meeting operations -----------------------------

    def _open_meeting(self, meeting_id: str) -> None:
        self.stack.setCurrentWidget(self.details_screen)
        self.details_screen.show_meeting(meeting_id)

    def _on_meeting_deleted(self, _meeting_id: str) -> None:
        self.statusBar().showMessage("Meeting deleted from this computer.")

    def _on_recording_saved(self, meeting_id: str) -> None:
        """Called when a recording is saved successfully."""
        self.statusBar().showMessage(f"Recording saved: {meeting_id}")
        self.dashboard_screen.refresh()
        self._show_dashboard()

    def _show_dashboard(self) -> None:
        """Return to the dashboard screen."""
        self.stack.setCurrentWidget(self.dashboard_screen)

    # -- processing ---------------------------------------

    def _transcribe_meeting(self, meeting_id: str) -> None:
        """Transcribe a meeting's recording locally in the background."""

        self._start_processing(meeting_id, stage="transcribe")

    def _analyse_meeting(self, meeting_id: str) -> None:
        """Analyse a meeting's saved transcript in the background."""

        self._start_processing(meeting_id, stage="analyse")

    def _cancel_processing(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def _start_processing(self, meeting_id: str, *, stage: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        try:
            details = ops.get_meeting_details(self.library, meeting_id)
        except Exception as exc:
            self._show_error(f"Could not load the meeting: {exc}")
            return
        if details is None:
            self._show_error("That meeting could not be found.")
            return

        # Re-transcription replaces the existing transcript and deletes the
        # saved analysis; the details screen already asked for confirmation.
        transcript = details.get("transcript") or {}
        force = bool(str(transcript.get("text") or "").strip())

        worker = MeetingProcessingWorker(
            self.library,
            self.settings,
            meeting_id,
            self.transcription_service,
            self.summarizer,
            stage=stage,
            force=force,
        )
        worker.progress.connect(self._on_processing_progress)
        worker.completed.connect(self._on_processing_completed)
        worker.failed.connect(self._on_processing_failed)
        worker.cancelled.connect(self._on_processing_cancelled)
        worker.finished.connect(self._clear_processing_worker)
        self._worker = worker
        self.details_screen.set_processing(
            True,
            "Transcribing locally..." if stage == "transcribe"
            else "Analysing transcript...",
        )
        worker.start()

    def _on_processing_progress(self, message: str) -> None:
        self.details_screen.set_processing(True, message)

    def _on_processing_completed(self, _meeting_id: str) -> None:
        self.details_screen.processing_finished()
        self.dashboard_screen.refresh()
        self.statusBar().showMessage("Meeting processing complete.")

    def _on_processing_failed(self, message: str) -> None:
        self.details_screen.processing_failed(message)
        self.dashboard_screen.refresh()
        self.statusBar().showMessage("Meeting processing failed.")

    def _on_processing_cancelled(self) -> None:
        self.details_screen.processing_cancelled()
        self.statusBar().showMessage("Meeting processing cancelled.")

    def _clear_processing_worker(self) -> None:
        self._worker = None

    # -- status bar ---------------------------------------

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "Error", message)

    # -- shutdown -----------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """Cancel in-flight work cleanly when the window is closed."""

        recording = self.recording_screen._state in ("recording", "finalising")
        processing = self._worker is not None and self._worker.isRunning()
        if recording or processing:
            answer = QMessageBox.question(
                self,
                "Quit TeamMeetingAI",
                "A recording or a processing task is still running.\n\n"
                "Quit anyway? An active recording will be discarded and "
                "processing will be cancelled.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if processing and self._worker is not None:
                self._worker.cancel()
                self._worker.wait(10_000)
        self.recording_screen.shutdown()
        self.details_screen.shutdown()
        event.accept()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Processing worker
# ---------------------------------------------------------------------------


class _StageError(RuntimeError):
    """A processing stage cannot start (bad preconditions for that stage)."""


class MeetingProcessingWorker(QThread):
    """Transcribe and analyse a saved meeting without blocking the UI thread.

    Stages:

    * ``auto`` - transcribe the recording when no transcript exists yet, then
      run analysis when an analysis backend is configured.  A meeting that is
      already transcribed simply gets analysed (or completes as-is).
    * ``transcribe`` - transcribe only.  Refuses when a transcript exists
      unless ``force`` is set (re-transcription), in which case the saved
      analysis is deleted together with the replaced transcript.
    * ``analyse`` - analyse the saved transcript only.

    Cancellation is cooperative: the meeting returns to its previous status
    and no partial transcript or analysis is saved.
    """

    completed = Signal(str)
    failed = Signal(str)
    progress = Signal(str)
    cancelled = Signal()

    _VALID_STAGES = ("auto", "transcribe", "analyse")
    _MISSING_TRACK_SENTINEL = "__separated_track_missing__.wav"

    def __init__(
        self,
        library: MeetingLibrary,
        settings: Settings,
        meeting_id: str,
        transcription_service: TranscriptionService,
        summarizer: MeetingSummarizer,
        *,
        stage: str = "auto",
        force: bool = False,
    ) -> None:
        super().__init__()
        if stage not in self._VALID_STAGES:
            raise ValueError(
                f"Unknown processing stage {stage!r}. "
                f"Expected one of: {', '.join(self._VALID_STAGES)}."
            )
        self.library = library
        self.settings = settings
        self.meeting_id = meeting_id
        self.transcription_service = transcription_service
        self.summarizer = summarizer
        self.stage = stage
        self.force = bool(force)
        self._cancel_event = threading.Event()

    # -- control ----------------------------------------------------------

    def cancel(self) -> None:
        """Ask the running stage to stop at the next cancellation check."""

        self._cancel_event.set()

    def _cancel_cb(self) -> bool:
        return self._cancel_event.is_set()

    def _progress_cb(self, fraction: float, message: str) -> None:
        percent = int(max(0.0, min(1.0, fraction)) * 100)
        self.progress.emit(f"{percent}% - {message}")

    # -- worker body -------------------------------------------------------

    def run(self) -> None:  # noqa: C901 - one linear pipeline per stage
        meeting_id = self.meeting_id
        try:
            details = ops.get_meeting_details(self.library, meeting_id)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        if details is None:
            self.failed.emit("That meeting could not be found.")
            return
        if self._cancel_event.is_set():
            self.cancelled.emit()
            return

        previous_status = str(details.get("status", "recorded"))
        previous_error = details.get("error_message")
        transcript = details.get("transcript") or {}
        has_transcript = bool(str(transcript.get("text") or "").strip())
        recording = details.get("recording") or {}

        try:
            if self.stage == "analyse":
                if not has_transcript:
                    raise _StageError(
                        "This meeting has no transcript to analyse. "
                        "Transcribe the recording first."
                    )
                self._analyse(meeting_id, str(transcript.get("text", "")))
            else:
                if self.stage == "transcribe" and has_transcript and not self.force:
                    raise _StageError(
                        "This meeting already has a transcript. Re-transcribing "
                        "will replace it and delete the saved analysis."
                    )
                should_transcribe = not has_transcript or (
                    self.stage == "transcribe" and self.force
                )
                if should_transcribe:
                    self._transcribe(meeting_id, recording)
                    details = ops.get_meeting_details(self.library, meeting_id) or {}
                    transcript = details.get("transcript") or {}
                    has_transcript = bool(str(transcript.get("text") or "").strip())
                if self.stage != "transcribe" and has_transcript:
                    if self.summarizer.provider_configured:
                        self._analyse(meeting_id, str(transcript.get("text", "")))
            self.completed.emit(meeting_id)
        except TranscriptionCancelled:
            self._restore(meeting_id, previous_status, previous_error)
            self.cancelled.emit()
        except (TranscriptionError, NoSpeechError, SummarizationError, _StageError) as exc:
            self._fail(meeting_id, str(exc))
        except Exception as exc:
            logger.exception("Processing failed for meeting %s.", meeting_id)
            self._fail(meeting_id, str(exc) or "Meeting processing failed.")

    # -- stages ------------------------------------------------------------

    def _transcribe(self, meeting_id: str, recording: dict[str, Any]) -> None:
        audio_name = recording.get("audio")
        if not audio_name:
            raise _StageError("This meeting has no recording file to transcribe.")
        try:
            audio = self.library.resolve_meeting_file(meeting_id, str(audio_name))
        except (LookupError, ValueError) as exc:
            raise TranscriptionError(
                "The recording file is missing or unreadable."
            ) from exc
        if not audio.is_file():
            raise TranscriptionError("The recording file is missing or unreadable.")

        mic = self._track_path(meeting_id, recording.get("mic"))
        system = self._track_path(meeting_id, recording.get("system"))
        missing = audio.parent / self._MISSING_TRACK_SENTINEL

        ops.set_meeting_status(self.library, meeting_id, "transcribing")
        result = self.transcription_service.transcribe_meeting(
            audio,
            mic if mic is not None else missing,
            system if system is not None else missing,
            progress_cb=self._progress_cb,
            cancel_cb=self._cancel_cb,
        )
        if self._cancel_event.is_set():
            raise TranscriptionCancelled("Transcription was cancelled.")

        if self.force:
            # The new transcript must never disagree with the old analysis.
            ops.clear_analysis(self.library, meeting_id)
        ops.save_transcript(
            self.library,
            meeting_id,
            text=result.text,
            segments=result.segments,
            language=result.language,
        )
        logger.info("Transcript saved for meeting %s.", meeting_id)

    def _analyse(self, meeting_id: str, transcript_text: str) -> None:
        if not self.summarizer.provider_configured:
            raise _StageError(
                "No analysis backend is configured. Set "
                "TEAMMEETINGAI_ANALYSIS_BACKEND to enable meeting analysis."
            )
        ops.set_meeting_status(self.library, meeting_id, "analyzing")
        analysis = self.summarizer.analyze(
            transcript_text, progress_cb=self._progress_cb
        )
        if self._cancel_event.is_set():
            raise TranscriptionCancelled("Analysis was cancelled.")
        ops.save_analysis(
            self.library,
            meeting_id,
            analysis,
            model=self.summarizer.provider_model_name,
        )
        logger.info("Analysis saved for meeting %s.", meeting_id)

    # -- helpers -----------------------------------------------------------

    def _track_path(self, meeting_id: str, name: Any) -> Path | None:
        """Resolve a separated-track file, or None when absent on disk."""

        if not name:
            return None
        try:
            candidate = self.library.resolve_meeting_file(meeting_id, str(name))
        except (LookupError, ValueError):
            return None
        return candidate if candidate.is_file() else None

    def _restore(self, meeting_id: str, status: str, error_message: Any) -> None:
        """Return the meeting to its pre-processing status after a cancel."""

        try:
            ops.set_meeting_status(
                self.library,
                meeting_id,
                status,
                error_message=error_message if isinstance(error_message, str) else None,
            )
        except Exception:
            logger.warning("Could not restore status for meeting %s.", meeting_id)

    def _fail(self, meeting_id: str, message: str) -> None:
        try:
            ops.mark_meeting_failed(self.library, meeting_id, message)
        except Exception:
            pass
        self.failed.emit(message)


# Load the application stylesheet from a path anchored to this file so the
# current working directory never matters (shortcuts, launchers, packaging).
_STYLESHEET = _load_stylesheet()


if __name__ == "__main__":
    sys.exit(main())
