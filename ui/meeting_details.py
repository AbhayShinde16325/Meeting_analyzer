"""Meeting detail view: playback, transcript, summary and attachments.

All data comes from the server API.  Audio playback downloads the stored
recording (Large Object) into a local cache once, then plays it from disk so
seeking works without re-streaming.  Transcription/analysis are queued on the
server and polled with a timer; cancellation is a server-side job cancel.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
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

from client.api_client import (
    ApiClientError,
    ApiConnectionError,
    TeamMeetingAIClient,
)
from config import Settings
from ui.qt_utils import worker_running
from ui.speaker_review import SpeakerReviewDialog

try:  # PySide6 multimedia is included in the standard wheel.
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    _MULTIMEDIA_AVAILABLE = True
except Exception:  # pragma: no cover - platform packaging fallback
    QAudioOutput = None  # type: ignore[assignment]
    QMediaPlayer = None  # type: ignore[assignment]
    _MULTIMEDIA_AVAILABLE = False


_STATUS_LABELS = {
    "recording": "Recording in progress",
    "recorded": "Recorded - transcription queued",
    "transcribed": "Transcribed - analysis pending",
    "analyzing": "Analyzing",
    "completed": "Complete",
    "failed": "Failed",
}

_CLOUD_WARNING = (
    "Transcription runs on Sarvam's cloud service: the meeting audio is "
    "uploaded to Sarvam when transcription is queued. Analysis (this summary) "
    "is generated with an external language model from the transcript. Do not "
    "record confidential meetings unless your organisation permits this."
)


def _format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "Unknown duration"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def _format_datetime(value: str | datetime | None) -> str:
    if value is None:
        return "Unknown date"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%A, %d %B %Y at %H:%M")


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


class _DetailsWorker(QThread):
    """Fetch everything this screen renders, off the UI thread."""

    completed = Signal(object, object)  # details dict, error or None

    def __init__(self, client: TeamMeetingAIClient, meeting_id: str) -> None:
        super().__init__()
        self.client = client
        self.meeting_id = meeting_id

    def run(self) -> None:
        try:
            details = {
                "meeting": self.client.get_meeting(self.meeting_id),
                "files": self.client.list_files(self.meeting_id),
                "transcripts": self.client.list_transcripts(self.meeting_id),
                "summary": self.client.get_summary(self.meeting_id),
                "action_items": self.client.list_action_items(self.meeting_id),
                "jobs": self.client.list_jobs(self.meeting_id),
                "speakers": self.client.list_meeting_speakers(self.meeting_id),
                "metrics": self.client.get_meeting_metrics(self.meeting_id),
            }
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, f"Unexpected error: {type(exc).__name__}: {exc}")
        else:
            self.completed.emit(details, None)


class _DownloadWorker(QThread):
    """Download a stored file (audio/attachment) into a local cache."""

    completed = Signal(object, object, object)  # file row, path, error or None

    def __init__(
        self,
        client: TeamMeetingAIClient,
        file_row: dict[str, Any],
        cache_dir: Path,
    ) -> None:
        super().__init__()
        self.client = client
        self.file_row = file_row
        self.cache_dir = cache_dir

    def run(self) -> None:
        sha = str(self.file_row.get("sha256", ""))
        filename = self.file_row.get("original_filename") or f"file-{self.file_row['id']}"
        target = self.cache_dir / f"{sha[:16]}-{filename}"
        try:
            self.client.download_file(str(self.file_row["id"]), target)
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit(self.file_row, None, str(exc))
        except Exception as exc:
            self.completed.emit(
                self.file_row, None, f"Unexpected error: {type(exc).__name__}: {exc}"
            )
        else:
            self.completed.emit(self.file_row, target, None)


class _JobActionWorker(QThread):
    """One-shot job mutation: queue transcribe/analyse or cancel a job."""

    completed = Signal(object, object)  # result, error or None

    def __init__(self, client: TeamMeetingAIClient, action: str, **kwargs: Any) -> None:
        super().__init__()
        self.client = client
        self.action = action
        self.kwargs = kwargs

    def run(self) -> None:
        try:
            if self.action == "transcribe":
                result = self.client.transcribe(self.kwargs["meeting_id"])
            elif self.action == "analyse":
                result = self.client.analyse(self.kwargs["meeting_id"])
            elif self.action == "cancel":
                result = self.client.cancel_job(
                    self.kwargs["meeting_id"], self.kwargs["job_id"]
                )
            else:  # pragma: no cover - guarded by caller
                raise ValueError(f"Unknown action {self.action!r}")
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, f"Unexpected error: {type(exc).__name__}: {exc}")
        else:
            self.completed.emit(result, None)


class _JobPollWorker(QThread):
    """Poll the meeting's job list while the processing timer is active."""

    completed = Signal(list, object)

    def __init__(self, client: TeamMeetingAIClient, meeting_id: str) -> None:
        super().__init__()
        self.client = client
        self.meeting_id = meeting_id

    def run(self) -> None:
        try:
            self.completed.emit(self.client.list_jobs(self.meeting_id), None)
        except Exception as exc:  # polling errors are non-fatal
            self.completed.emit([], f"{type(exc).__name__}: {exc}")


class MeetingDetailsScreen(QWidget):
    back_requested = Signal()
    meeting_updated = Signal()  # dashboard should refresh its list

    def __init__(
        self,
        client: TeamMeetingAIClient,
        settings: Settings,
        parent: QWidget | None = None,
        *,
        can_process: bool = True,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.settings = settings
        self.current_user: dict[str, Any] | None = None
        self._can_process = can_process  # host-only actions
        self._meeting_id: str | None = None
        self._details: dict[str, Any] | None = None
        self._processing = False
        # The job this screen just queued.  The cached details snapshot is
        # stale for a moment after queueing, so cancelling has to use this id
        # rather than the previous job list.
        self._active_job_id: str | None = None
        self._player = None
        self._audio_output = None

        self._details_worker: _DetailsWorker | None = None
        self._download_worker: _DownloadWorker | None = None
        self._action_worker: _JobActionWorker | None = None
        self._poll_worker: _JobPollWorker | None = None
        self._audio_file_id: str | None = None

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(3000)
        self._poll_timer.timeout.connect(self._poll_jobs)

        self._build_ui()
        self._setup_player()

    # -- UI construction -------------------------------------------------

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

        self.cloud_warning = QLabel(_CLOUD_WARNING)
        self.cloud_warning.setObjectName("consentText")
        self.cloud_warning.setWordWrap(True)
        layout.addWidget(self.cloud_warning)

        actions = QHBoxLayout()
        self.play_button = QPushButton("Play recording")
        self.play_button.setToolTip(
            "Download the recording from the server (once) and play it."
        )
        self.play_button.clicked.connect(self._toggle_playback)
        self.play_button.setEnabled(False)

        self.transcribe_button = QPushButton("Transcribe recording")
        self.transcribe_button.setObjectName("primaryButton")
        self.transcribe_button.setToolTip(
            "Queue transcription on the server (Sarvam cloud, Whisper fallback)."
        )
        self.transcribe_button.clicked.connect(self._request_transcription)
        self.transcribe_button.setEnabled(False)

        self.analyse_button = QPushButton("Analyse transcript")
        self.analyse_button.setToolTip(
            "Queue meeting analysis on the server from the primary transcript."
        )
        self.analyse_button.clicked.connect(self._request_analysis)
        self.analyse_button.setEnabled(False)

        self.cancel_button = QPushButton("Cancel job")
        self.cancel_button.clicked.connect(self._request_cancel)
        self.cancel_button.setVisible(False)

        self.export_button = QPushButton("Export ZIP")
        self.export_button.setToolTip(
            "Download the meeting (metadata, transcript, summary, attachments, "
            "audio) as a ZIP file."
        )
        self.export_button.clicked.connect(self._export_meeting)
        self.export_button.setEnabled(False)

        self.review_speakers_button = QPushButton("Review speakers")
        self.review_speakers_button.setToolTip(
            "Review diarized speakers and confirm voice-profile matches. "
            "Confirmed names appear in the transcript."
        )
        self.review_speakers_button.clicked.connect(self._review_speakers)
        self.review_speakers_button.setEnabled(False)

        actions.addWidget(self.play_button)
        actions.addWidget(self.transcribe_button)
        actions.addWidget(self.analyse_button)
        actions.addWidget(self.review_speakers_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.export_button)
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

        # Metrics tab: reference-free quality metrics computed by the server
        # after each transcription.
        self.metrics_browser = QTextBrowser()
        self.metrics_browser.setOpenExternalLinks(False)
        self.metrics_browser.setObjectName("detailBrowser")
        self.tabs.addTab(self.metrics_browser, "Metrics")

        # Attachments tab: documents stored with the meeting.
        attachments_widget = QWidget()
        attachments_layout = QVBoxLayout(attachments_widget)
        attachments_layout.setContentsMargins(0, 8, 0, 0)
        attachments_layout.setSpacing(6)

        self.attachments_table = QTableWidget(0, 4)
        self.attachments_table.setHorizontalHeaderLabels(
            ["File", "Type", "Size", "Uploaded"]
        )
        self.attachments_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.attachments_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.attachments_table.verticalHeader().setVisible(False)
        attach_header = self.attachments_table.horizontalHeader()
        attach_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        attach_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        attach_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        attach_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        attachments_layout.addWidget(self.attachments_table, 1)

        attach_buttons = QHBoxLayout()
        self.no_attachments_label = QLabel("No attachments on this meeting.")
        self.no_attachments_label.setObjectName("muted")
        attachments_layout.addWidget(self.no_attachments_label)
        self.open_attachment_button = QPushButton("Download && open")
        self.open_attachment_button.clicked.connect(self._open_selected_attachment)
        self.open_attachment_button.setEnabled(False)
        attach_buttons.addStretch(1)
        attach_buttons.addWidget(self.open_attachment_button)
        attachments_layout.addLayout(attach_buttons)
        self.attachments_table.itemSelectionChanged.connect(self._update_attachment_actions)
        self.attachments_table.itemDoubleClicked.connect(
            lambda _item: self._open_selected_attachment()
        )

        self.tabs.addTab(attachments_widget, "Attachments")

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._render_empty_state("Select a meeting to view its details.")

    def _setup_player(self) -> None:
        if not _MULTIMEDIA_AVAILABLE:
            return
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_player_error)

    # -- loading ---------------------------------------------------------

    def show_meeting(self, meeting_id: str) -> None:
        self._meeting_id = meeting_id
        self._processing = False
        self.cancel_button.setVisible(False)
        self.status_label.setText("")
        self._poll_timer.stop()
        self.reload()

    def reload(self) -> None:
        if self._meeting_id is None:
            return
        if worker_running(self._details_worker):
            return
        self._details_worker = _DetailsWorker(self.client, self._meeting_id)
        self._details_worker.completed.connect(self._on_details_loaded)
        self._details_worker.finished.connect(self._details_worker.deleteLater)
        self._details_worker.start()

    def _primary_transcript(self, details: dict[str, Any]) -> dict[str, Any] | None:
        transcripts = details.get("transcripts") or []
        for transcript in transcripts:
            if transcript.get("is_primary"):
                return transcript
        return transcripts[0] if transcripts else None

    def _original_audio(self, details: dict[str, Any]) -> dict[str, Any] | None:
        for row in details.get("files") or []:
            if row.get("kind") == "original_audio_48k":
                return row
        return None

    def _attachments(self, details: dict[str, Any]) -> list[dict[str, Any]]:
        return [f for f in details.get("files") or [] if f.get("kind") == "attachment"]

    def _on_details_loaded(self, details: object, error: object) -> None:
        self._details_worker = None
        if error:
            self._show_notice(f"Could not load the meeting: {error}", error=True)
            return
        assert isinstance(details, dict)
        if not details.get("meeting"):
            self._show_notice("That meeting could not be found.", error=True)
            self._render_empty_state("Meeting not found.")
            return

        self._details = details
        meeting = details["meeting"]
        self.title_label.setText(str(meeting.get("title", "Untitled meeting")))
        audio = self._original_audio(details)
        duration_seconds = None
        if meeting.get("duration_ms"):
            duration_seconds = meeting["duration_ms"] / 1000.0
        meta_parts = [
            _format_datetime(meeting.get("recorded_at")),
            _format_duration(duration_seconds),
            _STATUS_LABELS.get(
                str(meeting.get("status", "")),
                str(meeting.get("status", "")).replace("_", " ").title(),
            ),
        ]
        if audio:
            meta_parts.append(_format_size(audio.get("size_bytes")))
        self.meta_label.setText("  |  ".join(meta_parts))

        self._render_overview(details)
        self._render_transcript(details)
        self._render_metrics(details)
        self._render_actions(details)
        self._render_attachments(details)
        self._update_controls()

        jobs = details.get("jobs") or []
        active = [j for j in jobs if j.get("status") in ("queued", "running")]
        if active:
            job = active[0]
            self._processing = True
            self.cancel_button.setVisible(True)
            self.status_label.setText(
                f"{job.get('job_type', 'job').title()} {job.get('status', '')} "
                f"({int(float(job.get('progress', 0)) * 100)}%) - "
                f"{job.get('message') or 'working...'}"
            )
            self._poll_timer.start()
        elif meeting.get("status") == "failed":
            failed_jobs = [j for j in jobs if j.get("status") == "failed"]
            message = (
                failed_jobs[0].get("error") if failed_jobs else None
            ) or "Processing failed."
            self._show_notice(str(message), error=True)

    # -- rendering (unchanged presentation) ---------------------------------

    def _render_overview(self, details: dict[str, Any]) -> None:
        summary = details.get("summary")
        transcript = self._primary_transcript(details)
        if not summary:
            if transcript:
                if self._can_process:
                    body = (
                        "<p>This meeting has a transcript but has not been "
                        "analysed yet. Click <b>Analyse transcript</b> above "
                        "to create the summary.</p>"
                    )
                else:
                    body = (
                        "<p>This meeting has a transcript. Analysis can only "
                        "be queued from the meeting host computer.</p>"
                    )
            else:
                body = (
                    "<p>This meeting has not been processed yet. Use the "
                    "<b>Transcribe recording</b> button above to queue "
                    "transcription on the server.</p>"
                )
            self.overview_browser.setHtml(self._wrap_html(body))
            return

        parts: list[str] = []
        overview = html.escape(str(summary.get("overview", ""))).replace("\n", "<br>")
        parts.append(f"<h2>Overview</h2><p>{overview}</p>")

        payload = summary.get("payload") or {}
        parts.append(self._render_html_list("Key points", payload.get("key_points")))
        parts.append(self._render_html_list("Decisions", payload.get("decisions")))
        parts.append(
            self._render_html_list("Open questions", payload.get("open_questions"))
        )
        parts.append(
            self._render_html_list(
                "Risks and blockers", payload.get("risks_or_blockers")
            )
        )
        parts.append(
            self._render_html_list("Suggested follow-ups", payload.get("follow_ups"))
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

    def _confirmed_speaker_names(self) -> dict[str, str]:
        """Local diarization label -> confirmed profile name (user-reviewed)."""

        mapping: dict[str, str] = {}
        for speaker in (self._details or {}).get("speakers") or []:
            if (
                speaker.get("status") == "confirmed"
                and speaker.get("profile_name")
                and speaker.get("local_label")
            ):
                mapping[str(speaker["local_label"])] = str(speaker["profile_name"])
        return mapping

    def _render_transcript(self, details: dict[str, Any]) -> None:
        transcript = self._primary_transcript(details)
        if not transcript:
            self.transcript_browser.setHtml(
                self._wrap_html(
                    "<p class='muted'>No transcript is available yet. Queue "
                    "transcription from the server to create one.</p>"
                )
            )
            return

        confirmed = self._confirmed_speaker_names()
        segments = transcript.get("segments") or []
        parts = ["<h2>Transcript</h2>"]
        language = transcript.get("language")
        if language:
            parts.append(
                f"<p class='muted'>Detected language: {html.escape(str(language))}"
                f"  |  provider: {html.escape(str(transcript.get('provider', '')))}"
                f"</p>"
            )
        if transcript.get("fallback_from"):
            parts.append(
                "<p class='muted'>Produced by the local Whisper fallback "
                f"after {html.escape(str(transcript.get('fallback_from')))} "
                "was unavailable.</p>"
            )
        if segments:
            for segment in segments:
                try:
                    start = _timestamp(float(segment.get("start", 0)))
                except (TypeError, ValueError):
                    start = "00:00"
                text = html.escape(str(segment.get("text", "")))
                speaker = segment.get("speaker")
                if isinstance(speaker, str) and speaker.strip():
                    # Only user-confirmed profiles replace the raw label;
                    # suggestions never claim an identity.
                    label = confirmed.get(speaker.strip(), speaker.strip())
                    label = html.escape(label)
                    parts.append(f"<p><b>[{start}] {label}:</b> {text}</p>")
                else:
                    parts.append(f"<p><b>[{start}]</b> {text}</p>")
        else:
            text = html.escape(str(transcript.get("text", ""))).replace("\n", "<br>")
            parts.append(f"<p>{text}</p>")
        self.transcript_browser.setHtml(self._wrap_html("".join(parts)))

    def _render_metrics(self, details: dict[str, Any]) -> None:
        """Render the eight post-transcription metrics, measured from the
        current transcription run."""

        row = details.get("metrics")
        if not row or not row.get("metrics"):
            self.metrics_browser.setHtml(
                self._wrap_html(
                    "<h2>Metrics</h2>"
                    "<p class='muted'>No quality metrics are available for "
                    "this meeting yet. They are computed automatically after "
                    "a transcription completes; re-transcribe the meeting to "
                    "generate them.</p>"
                )
            )
            return

        metrics = row["metrics"]

        def seconds(value: Any) -> str:
            if value is None:
                return "-"
            return f"{float(value):.1f}s"

        def percent(value: Any) -> str:
            if value is None:
                return "-"
            return f"{float(value):.1f}%"

        status = str(metrics.get("transcription_status") or "unknown").title()
        status_html = html.escape(status)
        error = metrics.get("error")
        if error:
            status_html += (
                f"<br><span style='color:#b42318'>"
                f"{html.escape(str(error))}</span>"
            )
        fallback_from = metrics.get("fallback_from")
        if fallback_from:
            status_html += html.escape(
                f" (via {fallback_from} fallback)"
            )

        rows: list[tuple[str, str]] = [
            ("Audio duration", seconds(metrics.get("audio_duration_s"))),
            (
                "Transcription duration",
                seconds(metrics.get("transcription_duration_s")),
            ),
            ("Real-time factor (RTF)", str(metrics.get("rtf") or "-")),
            (
                "Speakers detected",
                str(metrics.get("num_speakers") or "-"),
            ),
            ("Speaker-labelled speech", percent(metrics.get("labelled_speech_pct"))),
            ("Unknown-speaker speech", percent(metrics.get("unknown_speech_pct"))),
            ("Segments needing review", str(metrics.get("review_segments", "-"))),
            ("Transcription status", status_html),
        ]

        body = [
            "<h2>Transcript metrics</h2>",
            "<p class='muted'>Measured from the current transcription run. "
            "Speaker percentages are computed from speech duration, not "
            "segment counts.</p>",
            "<table>",
        ]
        for label, value in rows:
            body.append(
                f"<tr><td><b>{html.escape(label)}:</b></td>"
                f"<td>{value}</td></tr>"
            )
        body.append("</table>")
        computed = row.get("computed_at")
        if computed:
            body.append(
                f"<p class='muted'>Computed {html.escape(_format_datetime(computed))}.</p>"
            )
        self.metrics_browser.setHtml(self._wrap_html("".join(body)))

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

    def _render_attachments(self, details: dict[str, Any]) -> None:
        attachments = self._attachments(details)
        self.attachments_table.setRowCount(len(attachments))
        for row, attachment in enumerate(attachments):
            created = str(attachment.get("created_at", ""))[:16].replace("T", " ")
            values = (
                str(attachment.get("original_filename", "")),
                str(attachment.get("mime_type", "")),
                _format_size(attachment.get("size_bytes")),
                created,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                cell.setData(Qt.ItemDataRole.UserRole, str(attachment["id"]))
                self.attachments_table.setItem(row, column, cell)
        has_items = bool(attachments)
        self.attachments_table.setVisible(has_items)
        self.no_attachments_label.setVisible(not has_items)
        self._update_attachment_actions()

    # -- controls ----------------------------------------------------------

    def _update_controls(self) -> None:
        details = self._details
        if details is None:
            self.play_button.setEnabled(False)
            self.transcribe_button.setEnabled(False)
            self.analyse_button.setEnabled(False)
            self.review_speakers_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self.open_attachment_button.setEnabled(False)
            return

        idle = not self._processing
        audio = self._original_audio(details)
        self._audio_file_id = str(audio["id"]) if audio else None
        transcript = self._primary_transcript(details) or {}
        has_transcript = bool(str(transcript.get("text") or "").strip())
        has_summary = details.get("summary") is not None
        has_speakers = bool(details.get("speakers"))

        self.play_button.setEnabled(
            audio is not None and idle and self._player is not None
        )
        self.transcribe_button.setEnabled(
            audio is not None and idle and self._can_process
        )
        self.transcribe_button.setText(
            "Re-transcribe" if has_transcript else "Transcribe recording"
        )
        self.transcribe_button.setToolTip(
            (
                "Transcribe the recording again. The new transcript becomes "
                "the primary; re-run analysis afterwards."
                if has_transcript
                else ""
            )
        )
        self.analyse_button.setEnabled(
            has_transcript and idle and self._can_process
        )
        self.analyse_button.setText(
            "Re-run analysis" if has_summary else "Analyse transcript"
        )
        self.analyse_button.setToolTip(
            "" if self._can_process else "Only the meeting host can queue jobs."
        )
        self.review_speakers_button.setEnabled(
            has_transcript and idle and (self._can_process or has_speakers)
        )
        self.export_button.setEnabled(idle)

    def _update_attachment_actions(self) -> None:
        has_selection = self._selected_attachment() is not None
        self.open_attachment_button.setEnabled(has_selection and not self._processing)

    def _selected_attachment(self) -> dict[str, Any] | None:
        row = self.attachments_table.currentRow()
        if row < 0:
            return None
        item = self.attachments_table.item(row, 0)
        if item is None:
            return None
        file_id = item.data(Qt.ItemDataRole.UserRole)
        for attachment in self._attachments(self._details or {}):
            if str(attachment["id"]) == str(file_id):
                return attachment
        return None

    def _open_selected_attachment(self) -> None:
        attachment = self._selected_attachment()
        if attachment is None or self._download_worker is not None:
            return
        self._start_download(attachment, open_after=True)

    # -- playback ----------------------------------------------------------

    def _toggle_playback(self) -> None:
        if self._player is None or self._details is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.stop()
            return
        audio = self._original_audio(self._details)
        if audio is None or self._audio_file_id is None:
            return
        cached = self._cached_audio_path(audio)
        if cached is not None and cached.is_file():
            self._play_file(cached)
            return
        self._start_download(audio, open_after=False)

    def _cached_audio_path(self, audio: dict[str, Any]) -> Path | None:
        sha = str(audio.get("sha256", ""))
        filename = audio.get("original_filename") or "audio.wav"
        if not sha:
            return None
        return self._cache_dir() / f"{sha[:16]}-{filename}"

    def _cache_dir(self) -> Path:
        return self.settings.data_dir / "audio-cache"

    def _start_download(self, file_row: dict[str, Any], *, open_after: bool) -> None:
        self._download_worker = _DownloadWorker(
            self.client, file_row, self._cache_dir()
        )
        self._download_worker.completed.connect(
            lambda row, path, error, open_after=open_after:
            self._on_download_done(row, path, error, open_after)
        )
        self._download_worker.finished.connect(self._download_worker.deleteLater)
        self.play_button.setEnabled(False)
        self.open_attachment_button.setEnabled(False)
        self.status_label.setText(
            f"Downloading {file_row.get('original_filename') or 'file'}..."
        )
        self._download_worker.start()

    def _on_download_done(
        self, file_row: object, path: object, error: object, open_after: bool
    ) -> None:
        self._download_worker = None
        self._update_controls()
        self._update_attachment_actions()
        if error:
            self.status_label.setText("")
            self._show_notice(f"Download failed: {error}", error=True)
            return
        assert path is not None
        self.status_label.setText("")
        if open_after:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self._play_file(Path(str(path)))

    def _play_file(self, path: Path) -> None:
        if self._player is None:
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

    # -- processing (server jobs) -------------------------------------------

    def _request_transcription(self) -> None:
        if self._meeting_id is None or self._processing:
            return
        transcript = self._primary_transcript(self._details or {}) or {}
        has_transcript = bool(str(transcript.get("text") or "").strip())
        if has_transcript:
            answer = QMessageBox.question(
                self,
                "Re-transcribe meeting",
                "Transcribe the recording again on the server?\n\nThe new "
                "transcript becomes the primary version and analysis will need "
                "to be re-run.\n\nThe audio is sent to Sarvam (cloud).",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._run_action("transcribe", meeting_id=self._meeting_id)

    def _request_analysis(self) -> None:
        if self._meeting_id is None or self._processing:
            return
        self._run_action("analyse", meeting_id=self._meeting_id)

    def _review_speakers(self) -> None:
        if self._meeting_id is None or self._processing:
            return
        dialog = SpeakerReviewDialog(
            self.client,
            self._meeting_id,
            can_process=self._can_process,
            parent=self,
        )
        dialog.exec()
        dialog.shutdown()
        dialog.deleteLater()
        self.reload()

    def _request_cancel(self) -> None:
        if not self._processing or self._details is None:
            return
        job_id = self._active_job_id
        if not job_id:
            jobs = self._details.get("jobs") or []
            active = [j for j in jobs if j.get("status") in ("queued", "running")]
            if not active:
                return
            job_id = str(active[0]["id"])
        self._run_action(
            "cancel", meeting_id=self._meeting_id, job_id=job_id
        )

    def _run_action(self, action: str, **kwargs: Any) -> None:
        if worker_running(self._action_worker):
            return
        self._action_worker = _JobActionWorker(self.client, action, **kwargs)
        self._action_worker.completed.connect(
            lambda result, error, action=action: self._on_action_done(
                action, error, result
            )
        )
        self._action_worker.finished.connect(self._action_worker.deleteLater)
        self._set_busy(True, "Queuing on the server...")
        self._action_worker.start()

    def _on_action_done(self, action: str, error: object, result: object = None) -> None:
        self._action_worker = None
        if error:
            self._set_busy(False)
            self._show_notice(str(error), error=True)
            return
        if action == "cancel":
            self._active_job_id = None
            self._set_busy(False)
            self.status_label.setText("Cancellation requested.")
            self._poll_timer.stop()
            self.reload()
            return
        if isinstance(result, dict) and result.get("id"):
            self._active_job_id = str(result["id"])
        self._processing = True
        self._poll_timer.start()

    def _poll_jobs(self) -> None:
        if self._meeting_id is None:
            return
        if worker_running(self._poll_worker):
            return
        self._poll_worker = _JobPollWorker(self.client, self._meeting_id)
        self._poll_worker.completed.connect(self._on_jobs_polled)
        self._poll_worker.finished.connect(self._poll_worker.deleteLater)
        self._poll_worker.start()

    def _on_jobs_polled(self, jobs: list, error: object) -> None:
        self._poll_worker = None
        if error:
            return  # transient polling errors are ignored; next tick retries
        active = [j for j in jobs if j.get("status") in ("queued", "running")]
        if self._details is not None:
            self._details["jobs"] = jobs
        if active:
            job = active[0]
            self.status_label.setText(
                f"{job.get('job_type', 'job').title()} {job.get('status', '')} "
                f"({int(float(job.get('progress', 0)) * 100)}%) - "
                f"{job.get('message') or 'working...'}"
            )
            return
        self._poll_timer.stop()
        self._active_job_id = None
        self._set_busy(False)
        self.status_label.setText("Processing complete.")
        self.meeting_updated.emit()
        self.reload()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._processing = busy
        self.back_button.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        if not busy:
            self._poll_timer.stop()
        if busy:
            self.transcribe_button.setEnabled(False)
            self.analyse_button.setEnabled(False)
            self.play_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self.open_attachment_button.setEnabled(False)
            if self._player is not None:
                self._player.stop()
            self.status_label.setText(message or "Working...")
            self._show_notice("")
        else:
            self._update_controls()
            self._update_attachment_actions()

    def _export_meeting(self) -> None:
        if self._meeting_id is None:
            return
        target, _ = self._ask_save_path()
        if target is None:
            return
        self._set_busy(True, "Exporting meeting ZIP...")
        worker = _ExportWorker(self.client, self._meeting_id, target)
        worker.completed.connect(self._on_export_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._export_worker = worker

    def _ask_save_path(self) -> tuple[Path | None, Any]:
        from PySide6.QtWidgets import QFileDialog

        default_name = f"meeting-{self._meeting_id}.zip"
        chosen, _filter = QFileDialog.getSaveFileName(
            self, "Export meeting", default_name, "ZIP archive (*.zip)"
        )
        return (Path(chosen) if chosen else None), None

    def _on_export_done(self, target: object, error: object) -> None:
        self._set_busy(False)
        if error:
            self._show_notice(f"Export failed: {error}", error=True)
            return
        self.status_label.setText(f"Export saved: {target}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(str(target)).parent)))

    def _show_notice(self, message: str, *, error: bool = False) -> None:
        self.notice_label.setText(message)
        self.notice_label.setObjectName("errorText" if error else "muted")
        self.notice_label.style().unpolish(self.notice_label)
        self.notice_label.style().polish(self.notice_label)
        self.notice_label.setVisible(bool(message))

    # -- navigation / cleanup ----------------------------------------------

    def _go_back(self) -> None:
        if self._processing:
            QMessageBox.information(
                self,
                "Processing in progress",
                "A server job is still running. Cancel it or wait for it to "
                "finish before leaving.",
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

    def clear_meeting(self) -> None:
        if self._player is not None:
            self._player.stop()
        self._meeting_id = None
        self._details = None
        self._processing = False
        self.cancel_button.setVisible(False)
        self.status_label.setText("")
        self._poll_timer.stop()
        self._render_empty_state("Select a meeting to view its details.")
        self._update_controls()

    def shutdown(self) -> None:
        self._poll_timer.stop()
        if self._player is not None:
            self._player.stop()
        for worker in (
            self._details_worker,
            self._download_worker,
            self._action_worker,
            self._poll_worker,
            getattr(self, "_export_worker", None),
        ):
            if worker_running(worker):
                worker.wait(5000)


class _ExportWorker(QThread):
    """Download the meeting export ZIP off the UI thread."""

    completed = Signal(object, object)  # target path, error or None

    def __init__(
        self, client: TeamMeetingAIClient, meeting_id: str, target: Path
    ) -> None:
        super().__init__()
        self.client = client
        self.meeting_id = meeting_id
        self.target = target

    def run(self) -> None:
        try:
            self.client.export_meeting(self.meeting_id, self.target)
        except (ApiClientError, ApiConnectionError) as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, f"Unexpected error: {type(exc).__name__}: {exc}")
        else:
            self.completed.emit(self.target, None)
