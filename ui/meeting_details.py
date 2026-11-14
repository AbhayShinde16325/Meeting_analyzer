"""Meeting detail view: playback, transcript, and structured analysis."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config import Settings
from storage import library as ops
from storage.library import MeetingLibrary

try:  # PySide6 multimedia is included in the standard wheel.
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    _MULTIMEDIA_AVAILABLE = True
except Exception:  # pragma: no cover - platform packaging fallback
    QAudioOutput = None  # type: ignore[assignment]
    QMediaPlayer = None  # type: ignore[assignment]
    _MULTIMEDIA_AVAILABLE = False


_STATUS_LABELS = {
    "recording": "Recording in progress",
    "recorded": "Recorded - not processed",
    "transcribed": "Transcribed - analysis pending",
    "transcribing": "Transcribing",
    "analyzing": "Analyzing",
    "completed": "Complete",
    "failed": "Failed",
}


def _format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "Unknown duration"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "Unknown date"
    return value.replace(tzinfo=timezone.utc).astimezone().strftime(
        "%A, %d %B %Y at %H:%M"
    )


def _format_size(size: int | None) -> str:
    if not size:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class MeetingDetailsScreen(QWidget):
    back_requested = Signal()
    transcribe_requested = Signal(str)
    analysis_requested = Signal(str)
    cancel_requested = Signal()

    def __init__(
        self,
        library: MeetingLibrary,
        settings: Settings,
        parent: QWidget | None = None,
        *,
        analysis_available: bool = False,
        cloud_analysis: bool = False,
    ) -> None:
        super().__init__(parent)
        self.library = library
        self.settings = settings
        self._analysis_available = analysis_available
        self._cloud_analysis = cloud_analysis
        self._meeting_id: str | None = None
        self._details: dict[str, Any] | None = None
        self._processing = False
        self._player = None
        self._audio_output = None

        self._build_ui()
        self._setup_player()

    # -- UI construction ----------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        header = QHBoxLayout()
        self.back_button = QPushButton("Back to meetings")
        self.back_button.clicked.connect(self._go_back)
        header.addWidget(self.back_button)
        header.addStretch(1)
        layout.addLayout(header)

        self.title_label = QLabel("Meeting")
        self.title_label.setObjectName("pageTitle")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.meta_label.setWordWrap(True)
        layout.addWidget(self.meta_label)

        self.cloud_warning = QLabel("")
        self.cloud_warning.setObjectName("consentText")
        self.cloud_warning.setWordWrap(True)
        self.cloud_warning.setVisible(False)
        layout.addWidget(self.cloud_warning)

        actions = QHBoxLayout()
        self.play_button = QPushButton("Play recording")
        self.play_button.setToolTip("Play the meeting recording.")
        self.play_button.clicked.connect(self._toggle_playback)
        self.play_button.setEnabled(False)

        self.transcribe_button = QPushButton("Transcribe recording")
        self.transcribe_button.setObjectName("primaryButton")
        self.transcribe_button.setToolTip("Transcribe the recording locally (creates transcript.md).")
        self.transcribe_button.clicked.connect(self._request_transcription)
        self.transcribe_button.setEnabled(False)

        self.analyse_button = QPushButton("Analyse transcript")
        self.analyse_button.setToolTip("Generate meeting notes and action items.")
        self.analyse_button.clicked.connect(self._request_analysis)
        self.analyse_button.setEnabled(False)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._request_cancel)
        self.cancel_button.setVisible(False)

        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.setToolTip("Open the meeting folder with recording and notes.")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._open_meeting_folder)

        actions.addWidget(self.play_button)
        actions.addWidget(self.transcribe_button)
        actions.addWidget(self.analyse_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.open_folder_button)
        layout.addLayout(actions)

        self.notice_label = QLabel("")
        self.notice_label.setWordWrap(True)
        self.notice_label.setObjectName("errorText")
        self.notice_label.setVisible(False)
        layout.addWidget(self.notice_label)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # Summary tab: the meeting analysis with its action items below.
        summary_widget = QWidget()
        summary_layout = QVBoxLayout(summary_widget)
        summary_layout.setContentsMargins(0, 8, 0, 0)
        summary_layout.setSpacing(6)

        self.overview_browser = QTextBrowser()
        self.overview_browser.setOpenExternalLinks(False)
        self.overview_browser.setObjectName("detailBrowser")
        summary_layout.addWidget(self.overview_browser, 3)

        self.actions_heading = QLabel("Action items")
        self.actions_heading.setObjectName("sectionTitle")
        summary_layout.addWidget(self.actions_heading)

        self.no_actions_label = QLabel(
            "No action items were extracted from this meeting."
        )
        self.no_actions_label.setObjectName("muted")
        summary_layout.addWidget(self.no_actions_label)

        self.actions_table = QTableWidget(0, 5)
        self.actions_table.setHorizontalHeaderLabels(
            ["Task", "Owner", "Deadline", "Status", "Evidence"]
        )
        self.actions_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.actions_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.actions_table.verticalHeader().setVisible(False)
        self.actions_table.setWordWrap(True)
        header_view = self.actions_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        summary_layout.addWidget(self.actions_table, 2)

        self.tabs.addTab(summary_widget, "Summary")

        self.transcript_browser = QTextBrowser()
        self.transcript_browser.setOpenExternalLinks(False)
        self.transcript_browser.setObjectName("detailBrowser")
        self.tabs.addTab(self.transcript_browser, "Transcript")

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._render_empty_state("Select a meeting to view its details.")
        self._update_cloud_warning()

    def _update_cloud_warning(self) -> None:
        if self._cloud_analysis:
            self.cloud_warning.setText(
                "Cloud analysis is enabled: the meeting transcript (not the "
                "audio) is sent to an external service for processing and "
                "leaves this computer. Do not enable this for confidential "
                "meetings unless your organisation permits it."
            )
            self.cloud_warning.setVisible(True)
        else:
            self.cloud_warning.setText("")
            self.cloud_warning.setVisible(False)

    def _setup_player(self) -> None:
        if not _MULTIMEDIA_AVAILABLE:
            return
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_player_error)

    # -- loading -------------------------------------------------------

    def show_meeting(self, meeting_id: str) -> None:
        self._meeting_id = meeting_id
        self._processing = False
        self.cancel_button.setVisible(False)
        self.status_label.setText("")
        self.reload()

    def reload(self) -> None:
        if self._meeting_id is None:
            return
        if not self._processing:
            self._show_notice("")
        try:
            details = ops.get_meeting_details(self.library, self._meeting_id)
        except Exception as exc:
            self._show_notice(f"Could not load the meeting: {exc}", error=True)
            return
        if details is None:
            self._show_notice("That meeting could not be found.", error=True)
            self._render_empty_state("Meeting not found.")
            return

        self._details = details
        self.title_label.setText(str(details.get("title", "Untitled meeting")))
        recording = details.get("recording") or {}
        meta_parts = [
            _format_datetime(details.get("started_at")),
            _format_duration(details.get("duration_seconds")),
            _STATUS_LABELS.get(
                str(details.get("status", "")),
                str(details.get("status", "")).replace("_", " ").title(),
            ),
        ]
        size_text = _format_size(recording.get("size_bytes"))
        if size_text:
            meta_parts.append(size_text)
        self.meta_label.setText("  |  ".join(meta_parts))

        self._render_overview(details)
        self._render_transcript(details)
        self._render_actions(details)
        self._update_controls()

        message = details.get("error_message")
        if message and details.get("status") == "failed":
            self._show_notice(str(message), error=True)

    def _render_overview(self, details: dict[str, Any]) -> None:
        summary = details.get("summary")
        transcript = details.get("transcript")
        if not summary:
            if transcript:
                if self._analysis_available:
                    body = (
                        "<p>This meeting has a transcript but has not been "
                        "analysed yet. Click <b>Analyse transcript</b> above "
                        "to create the summary.</p>"
                    )
                else:
                    body = (
                        "<p>This meeting has a transcript. No analysis backend "
                        "is configured, so a summary cannot be generated.</p>"
                    )
            else:
                body = (
                    "<p>This meeting has not been processed yet. Use the "
                    "<b>Transcribe recording</b> button above to create a "
                    "transcript from the recording.</p>"
                )
            self.overview_browser.setHtml(self._wrap_html(body))
            return

        parts: list[str] = []
        overview = html.escape(str(summary.get("overview", ""))).replace("\n", "<br>")
        parts.append(f"<h2>Overview</h2><p>{overview}</p>")

        parts.append(self._render_html_list("Key points", summary.get("key_points")))
        parts.append(self._render_html_list("Decisions", summary.get("decisions")))
        parts.append(
            self._render_html_list("Open questions", summary.get("open_questions"))
        )
        parts.append(
            self._render_html_list(
                "Risks and blockers", summary.get("risks_or_blockers")
            )
        )
        parts.append(
            self._render_html_list("Suggested follow-ups", summary.get("follow_ups"))
        )

        model = summary.get("model")
        if model:
            parts.append(
                f"<p class='muted'>Generated with {html.escape(str(model))}.</p>"
            )
        self.overview_browser.setHtml(self._wrap_html("".join(parts)))

    @staticmethod
    def _render_html_list(title: str, values: Any) -> str:
        if not values:
            return f"<h3>{html.escape(title)}</h3><p class='muted'>None recorded.</p>"
        items = "".join(
            f"<li>{html.escape(str(value))}</li>" for value in values
        )
        return f"<h3>{html.escape(title)}</h3><ul>{items}</ul>"

    @staticmethod
    def _wrap_html(body: str) -> str:
        return (
            "<html><head><style>"
            "body { font-family: 'Segoe UI', sans-serif; font-size: 14px; "
            "line-height: 1.5; color: #20242a; background-color: #ffffff; }"
            "h2 { font-size: 18px; margin-bottom: 6px; color: #1e293b; }"
            "h3 { font-size: 15px; margin-top: 18px; margin-bottom: 4px; "
            "color: #1e293b; }"
            "table { color: #20242a; }"
            "td, th { color: #20242a; }"
            ".muted { color: #667085; }"
            "</style></head><body>" + body + "</body></html>"
        )

    def _render_transcript(self, details: dict[str, Any]) -> None:
        transcript = details.get("transcript")
        if not transcript:
            self.transcript_browser.setHtml(
                self._wrap_html(
                    "<p class='muted'>No transcript is available yet. Process "
                    "the meeting to create one locally.</p>"
                )
            )
            return

        segments = transcript.get("segments") or []
        parts = ["<h2>Transcript</h2>"]
        language = transcript.get("language")
        if language:
            parts.append(f"<p class='muted'>Detected language: {html.escape(str(language))}</p>")
        if segments:
            for segment in segments:
                try:
                    start = _timestamp(float(segment.get("start", 0)))
                except (TypeError, ValueError):
                    start = "00:00"
                text = html.escape(str(segment.get("text", "")))
                speaker = segment.get("speaker")
                if isinstance(speaker, str) and speaker.strip():
                    label = html.escape(speaker.strip())
                    parts.append(f"<p><b>[{start}] {label}:</b> {text}</p>")
                else:
                    parts.append(f"<p><b>[{start}]</b> {text}</p>")
        else:
            text = html.escape(str(transcript.get("text", ""))).replace("\n", "<br>")
            parts.append(f"<p>{text}</p>")
        self.transcript_browser.setHtml(self._wrap_html("".join(parts)))

    def _render_actions(self, details: dict[str, Any]) -> None:
        items = details.get("action_items") or []
        self.actions_table.setRowCount(len(items))
        for row, item in enumerate(items):
            values = (
                item.get("task", ""),
                item.get("owner") or "Not specified",
                item.get("deadline") or "Not specified",
                str(item.get("status", "open")).replace("_", " ").title(),
                item.get("evidence") or "",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.actions_table.setItem(row, column, cell)
        self.actions_table.resizeRowsToContents()
        has_items = bool(items)
        self.actions_heading.setVisible(True)
        self.no_actions_label.setText(
            "No action items were extracted from this meeting."
        )
        self.actions_table.setVisible(has_items)
        self.no_actions_label.setVisible(not has_items)

    def _update_controls(self) -> None:
        details = self._details
        if details is None:
            self.play_button.setEnabled(False)
            self.transcribe_button.setEnabled(False)
            self.analyse_button.setEnabled(False)
            self.open_folder_button.setEnabled(False)
            return

        recording = details.get("recording")
        audio_name = recording.get("audio") if recording else None
        file_exists = bool(
            audio_name
            and self._audio_file_exists(details, str(audio_name))
        )
        self.open_folder_button.setEnabled(self._meeting_folder_exists(details))

        transcript = details.get("transcript") or {}
        has_transcript = bool(str(transcript.get("text") or "").strip())
        has_summary = details.get("summary") is not None
        idle = not self._processing

        self.play_button.setEnabled(
            file_exists and idle and self._player is not None
        )

        # Transcription needs the audio file.  When a transcript already
        # exists the button becomes "Re-transcribe"; the confirmation dialog
        # in _request_transcription warns about the replaced analysis.
        self.transcribe_button.setEnabled(
            recording is not None and file_exists and idle
        )
        self.transcribe_button.setText(
            "Re-transcribe" if has_transcript else "Transcribe recording"
        )
        self.transcribe_button.setToolTip(
            (
                "Transcribe the recording again. This replaces the existing "
                "transcript and deletes the saved analysis."
                if has_transcript
                else ""
            )
        )

        # Analysis needs only the transcript text, so it stays available even
        # when the recording file has been moved or deleted.
        self.analyse_button.setEnabled(
            has_transcript and self._analysis_available and idle
        )
        self.analyse_button.setText(
            "Re-run analysis" if has_summary else "Analyse transcript"
        )
        self.analyse_button.setToolTip(
            "" if self._analysis_available else "No analysis backend is configured."
        )

        if recording and not file_exists and not self._processing:
            self._show_notice(
                "The local recording file is missing. You can still read any "
                "saved transcript and analysis, but audio playback and "
                "transcription are unavailable.",
                error=True,
            )

    def _audio_file_exists(self, details: dict[str, Any], audio_name: str) -> bool:
        try:
            return self.library.resolve_meeting_file(
                str(details["id"]), audio_name
            ).is_file()
        except (LookupError, ValueError):
            return False

    def _meeting_folder_exists(self, details: dict[str, Any]) -> bool:
        folder = details.get("meeting_dir")
        return bool(folder) and Path(str(folder)).is_dir()

    def _open_meeting_folder(self) -> None:
        if self._details is None:
            return
        folder = self._details.get("meeting_dir")
        if not folder:
            return
        path = Path(str(folder))
        if not path.is_dir():
            self._show_notice("The meeting folder could not be found.", error=True)
            self.open_folder_button.setEnabled(False)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def set_library(self, library: MeetingLibrary) -> None:
        """Point the screen at a different library and clear the open meeting."""

        if self._player is not None:
            self._player.stop()
        self.library = library
        self._meeting_id = None
        self._details = None
        self._processing = False
        self.cancel_button.setVisible(False)
        self.status_label.setText("")
        self._render_empty_state("Select a meeting to view its details.")
        self.open_folder_button.setEnabled(False)

    # -- playback ------------------------------------------------------

    def _toggle_playback(self) -> None:
        if self._player is None or self._details is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.stop()
            return

        recording = self._details.get("recording") or {}
        audio_name = recording.get("audio")
        if not audio_name:
            return
        try:
            path: Path = self.library.resolve_meeting_file(
                str(self._details["id"]), str(audio_name)
            )
        except (LookupError, ValueError):
            self._show_notice("The recording path is invalid.", error=True)
            return
        if not path.is_file():
            self._show_notice("The recording file could not be found.", error=True)
            self._update_controls()
            return
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()

    def _on_playback_state_changed(self, state) -> None:
        if self._player is None:
            return
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_button.setText("Stop playback")
        else:
            self.play_button.setText("Play recording")

    def _on_player_error(self, *_args) -> None:
        if self._player is not None and self._player.errorString():
            self._show_notice(
                f"Audio playback failed: {self._player.errorString()}", error=True
            )

    # -- processing ----------------------------------------------------

    def _request_transcription(self) -> None:
        if self._meeting_id is None or self._processing:
            return
        transcript = (self._details or {}).get("transcript") or {}
        has_transcript = bool(str(transcript.get("text") or "").strip())
        if has_transcript:
            answer = QMessageBox.question(
                self,
                "Re-transcribe meeting",
                "Re-transcribing will replace the existing transcript and "
                "delete the saved analysis for this meeting.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.transcribe_requested.emit(self._meeting_id)

    def _request_analysis(self) -> None:
        if self._meeting_id is None or self._processing:
            return
        self.analysis_requested.emit(self._meeting_id)

    def _request_cancel(self) -> None:
        if self._processing:
            self.cancel_requested.emit()

    def set_processing(self, active: bool, message: str = "") -> None:
        self._processing = active
        self.back_button.setEnabled(not active)
        self.cancel_button.setVisible(active)
        if active:
            self.transcribe_button.setEnabled(False)
            self.analyse_button.setEnabled(False)
            self.tabs.setEnabled(False)
            self.play_button.setEnabled(False)
            if self._player is not None:
                self._player.stop()
            self.status_label.setText(message or "Processing meeting locally...")
            self._show_notice("")
        else:
            self.tabs.setEnabled(True)
            self._update_controls()

    def processing_finished(self) -> None:
        self._processing = False
        self.cancel_button.setVisible(False)
        self.tabs.setEnabled(True)
        self.back_button.setEnabled(True)
        self.status_label.setText("Meeting processing complete.")
        self.reload()

    def processing_failed(self, message: str) -> None:
        self._processing = False
        self.cancel_button.setVisible(False)
        self.tabs.setEnabled(True)
        self.back_button.setEnabled(True)
        self.status_label.setText("Processing failed.")
        self.reload()
        self._show_notice(message or "Meeting processing failed.", error=True)

    def processing_cancelled(self) -> None:
        """Return the screen to an idle state after a user cancellation."""

        self._processing = False
        self.cancel_button.setVisible(False)
        self.tabs.setEnabled(True)
        self.back_button.setEnabled(True)
        self.status_label.setText("Processing cancelled.")
        self.reload()

    def _show_notice(self, message: str, *, error: bool = False) -> None:
        self.notice_label.setText(message)
        self.notice_label.setObjectName("errorText" if error else "muted")
        self.notice_label.style().unpolish(self.notice_label)
        self.notice_label.style().polish(self.notice_label)
        self.notice_label.setVisible(bool(message))

    # -- navigation / cleanup -----------------------------------------

    def _go_back(self) -> None:
        if self._processing:
            QMessageBox.information(
                self,
                "Processing in progress",
                "Please wait for local processing to finish before leaving.",
            )
            return
        if self._player is not None:
            self._player.stop()
        self.back_requested.emit()

    def _render_empty_state(self, message: str) -> None:
        self.overview_browser.setHtml(
            self._wrap_html(f"<p class='muted'>{html.escape(message)}</p>")
        )
        self.transcript_browser.clear()
        self.actions_heading.setVisible(False)
        self.actions_table.setRowCount(0)
        self.actions_table.setVisible(False)
        self.no_actions_label.setVisible(True)
        self.no_actions_label.setText(message)

    def shutdown(self) -> None:
        if self._player is not None:
            self._player.stop()

    def set_user(self, user: dict[str, Any]) -> None:
        self.current_user = user
        if hasattr(self, 'user_label'):
            self.user_label.setText(
                f"Local profile: {user.get('display_name', 'Unknown')}"
            )
