"""Server-side processing pipeline for transcription and analysis.

Job lifecycle (``processing_jobs``):

    queued -> running -> succeeded | failed | cancelled
                 \-> cancelling -> cancelled

* A transcription job streams the meeting's canonical audio out of its Large
  Object to a temporary file, decodes Opus to temporary WAV when required,
  runs the transcription provider (Sarvam primary with automatic Whisper
  fallback), and stores the result as the meeting's single primary transcript.
* An analysis job summarises the **primary Roman-script transcript** with the
  configured analysis provider (Groq) and stores the validated analysis plus
  individual action-item rows.

Cancellation is cooperative: the provider receives a ``cancel_cb`` that reads
the job's status from the database, and progress is throttled into the job
row so API clients can poll it.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from ai.summarization import MeetingSummarizer, SummarizationError
from ai.transcription import (
    NoSpeechError,
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionService,
)
from db import lo
from services.speaker_profiles import SpeakerProfileService, SpeakerProfilingError
from services.task_sync import TaskSyncError, sync_action_items_to_tasks
from services.transcript_metrics import compute_transcript_metrics

logger = logging.getLogger(__name__)

_PROGRESS_MIN_INTERVAL = 1.0  # seconds between job-row progress updates
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (30, 120, 300)
AUDIO_PREFERRED_KINDS = ("audio_opus", "derived_audio_16k", "original_audio_48k")


def _decode_audio_to_wav(source: Path, target: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PipelineError("ffmpeg is required to decode stored audio for processing.")
    cmd = [
        ffmpeg, "-y", "-v", "error", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise PipelineError("ffmpeg is required to decode stored audio for processing.") from exc
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size <= 44:
        detail = (proc.stderr or proc.stdout or "ffmpeg failed").strip().replace("\n", " ")[:300]
        raise PipelineError(f"Stored audio could not be decoded for processing: {detail}")
    return target


def _materialize_processing_audio(conn: Connection, file_row: dict[str, Any], temp_dir: Path, stem: str) -> Path:
    """Copy LO to temp and return a WAV path usable by local/remote providers."""
    kind = str(file_row.get("kind") or "")
    mime = str(file_row.get("mime_type") or "")
    codec = str(file_row.get("codec") or "")
    is_opus = kind == "audio_opus" or codec.lower() == "opus" or "ogg" in mime or "opus" in mime
    suffix = ".ogg" if is_opus else ".wav"
    source_path = temp_dir / f"{stem}{suffix}"
    with open(source_path, "wb") as target:
        lo.copy_lo_to_stream(conn, int(file_row["lo_oid"]), target)
    if is_opus:
        return _decode_audio_to_wav(source_path, temp_dir / f"{stem}-decoded.wav")
    # Legacy WAV: ensure 16k mono for downstream processors when possible.
    if suffix == ".wav":
        return source_path
    return _decode_audio_to_wav(source_path, temp_dir / f"{stem}-decoded.wav")


class PipelineError(RuntimeError):
    """Raised when a job cannot be completed."""


def _is_readonly_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).lower()
        sqlstate = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if sqlstate == "25006":
            return True
        if "read-only" in msg or "readonly" in msg or "read only transaction" in msg:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, (TranscriptionCancelled,)):
        return False
    if _is_readonly_error(exc):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OperationalError)):
        return True
    if isinstance(exc, DBAPIError):
        # connection/timeout/read-only/disconnect - treat as transient; statement errors may be permanent but rare.
        orig = getattr(exc, "orig", None)
        if _is_readonly_error(exc) or _is_readonly_error(orig) if orig else False:
            return True
        msg = str(exc).lower()
        transient_markers = ["timeout", "temporar", "connection", "unavailable", "deadlock", "serialization", "could not connect", "server closed"]
        if any(m in msg for m in transient_markers):
            return True
    msg = str(exc).lower()
    permanent_markers = [
        "missing", "not found", "no primary transcript", "unsupported", "invalid", "bad request",
        "validation", "schema", "empty audio", "too small", "corrupt", "no speech", "no diarized",
        "permission denied for file", "cannot open",
    ]
    transient_markers = [
        "timeout", "timed out", "temporar", "unavailable", "connection", "network", "502", "503", "504",
        "429", "rate limit", "service unavailable", "gateway", "reset by peer", "refused", "dns",
        "read-only", "readonly", "retry", "transient", "i/o", "io error",
    ]
    if isinstance(exc, (NoSpeechError,)):
        return False
    if isinstance(exc, SpeakerProfilingError):
        if any(m in msg for m in ["missing", "no primary transcript", "not wired up", "too little speech", "no diarized"]):
            return False
    if isinstance(exc, (TranscriptionError, SummarizationError, PipelineError)):
        if any(m in msg for m in permanent_markers):
            return False
        if any(m in msg for m in transient_markers):
            return True
        # Unknown provider/processing errors: retryable by default per user decision scope? User said
        # "transient/retryable failures..." so classify obvious transient; ambiguous provider failures
        # are safer to retry a limited number of times.
        return True
    if isinstance(exc, (FileNotFoundError, ValueError, KeyError, TypeError)):
        return False
    if any(m in msg for m in transient_markers):
        return True
    if any(m in msg for m in permanent_markers):
        return False
    # Unexpected runtime failures: treat as transient with limited retries.
    return True


def _error_code_for(exc: BaseException, *, retryable: bool) -> str:
    if isinstance(exc, TranscriptionCancelled):
        return "cancelled"
    if _is_readonly_error(exc):
        return "database_read_only"
    if isinstance(exc, NoSpeechError):
        return "no_speech"
    if isinstance(exc, TaskSyncError):
        return "task_sync_failed"
    if isinstance(exc, TranscriptionError):
        return "transcription_failed_retryable" if retryable else "transcription_failed"
    if isinstance(exc, SummarizationError):
        return "analysis_failed_retryable" if retryable else "analysis_failed"
    if isinstance(exc, SpeakerProfilingError):
        return "profiling_failed_retryable" if retryable else "profiling_failed"
    if isinstance(exc, PipelineError):
        return "pipeline_error_retryable" if retryable else "pipeline_error"
    return "unexpected_error_retryable" if retryable else "unexpected_error"


def _user_message_for(exc: BaseException, *, retryable: bool, attempts_exhausted: bool) -> str:
    if isinstance(exc, TranscriptionCancelled):
        return "Cancelled at the user's request."
    if _is_readonly_error(exc):
        if attempts_exhausted:
            return "The database is temporarily read-only/unavailable. Please try again after the database is writable."
        return "Temporary database write failure. Retrying..."
    if isinstance(exc, NoSpeechError):
        return "No speech was detected in the recording."
    base = str(exc).strip() or exc.__class__.__name__
    if attempts_exhausted and retryable:
        return f"{base} (after automatic retries)"
    return base


class TranscriptionPipeline:
    """Run transcription/analysis jobs with progress and cancellation."""

    def __init__(
        self,
        engine: Engine,
        transcription_service: TranscriptionService,
        summarizer: MeetingSummarizer | None,
        speaker_profiler: SpeakerProfileService | None = None,
    ) -> None:
        self._engine = engine
        self._transcription_service = transcription_service
        self._summarizer = summarizer
        self._speaker_profiler = speaker_profiler
        self._progress_log: dict[str, float] = {}

    # -- job execution ------------------------------------------------------

    def run_job(self, job_id: str) -> None:
        """Execute one claimed job to a terminal state."""

        with self._engine.begin() as conn:
            job = (
                conn.execute(
                    text(
                        """
                        SELECT meeting_id, job_type, status, attempt_count, max_attempts, cancel_requested_at
                        FROM meeting_analyzer.processing_jobs WHERE id = :id
                        """
                    ),
                    {"id": job_id},
                )
                .mappings()
                .one_or_none()
            )
        if job is None:
            raise PipelineError(f"Job {job_id} vanished.")

        if job.get("cancel_requested_at") is not None or job.get("status") == "cancelling":
            self._confirm_cancelled(job_id, "Cancelled at the user's request.")
            return

        try:
            self._heartbeat(job_id)
            if job["job_type"] == "transcription":
                self._run_transcription(job_id, str(job["meeting_id"]))
            elif job["job_type"] == "analysis":
                self._run_analysis(job_id, str(job["meeting_id"]))
            elif job["job_type"] == "profiling":
                self._run_profiling(job_id, str(job["meeting_id"]))
            else:
                raise PipelineError(f"Unknown job type {job['job_type']!r}.")
        except TranscriptionCancelled:
            self._confirm_cancelled(job_id, "Cancelled at the user's request.")
        except Exception as exc:  # expected + unexpected failures flow through retry/terminal handling
            self._handle_job_failure(job_id, job, exc)

    def fail_job_best_effort(
        self,
        job_id: str,
        *,
        error: str,
        error_code: str = "worker_crash",
        retryable: bool = False,
    ) -> None:
        try:
            with self._engine.begin() as conn:
                job = conn.execute(
                    text("SELECT meeting_id, job_type, attempt_count, max_attempts FROM meeting_analyzer.processing_jobs WHERE id=:id"),
                    {"id": job_id},
                ).mappings().one_or_none()
                if job is None:
                    return
                self._fail_or_retry_locked(
                    conn,
                    job_id=job_id,
                    job=dict(job),
                    exc=RuntimeError(error),
                    error_code=error_code,
                    retryable=retryable,
                    user_message=error,
                )
        except Exception:
            logger.exception("Best-effort failure update failed for job %s.", job_id)
            raise

    # -- transcription ------------------------------------------------------

    def _run_transcription(self, job_id: str, meeting_id: str) -> None:
        self._mark_job(job_id, "running", message="Transcribing...")
        self._raise_if_cancel_requested(job_id)

        with self._engine.begin() as conn:
            file_row = (
                conn.execute(
                    text(
                        """
                        SELECT lo_oid, kind, mime_type, codec
                        FROM meeting_analyzer.meeting_files
                        WHERE meeting_id = :m AND kind IN ('audio_opus', 'derived_audio_16k')
                        ORDER BY CASE kind WHEN 'audio_opus' THEN 0 ELSE 1 END
                        LIMIT 1
                        """
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )
            duration = conn.execute(
                text("SELECT duration_ms FROM meeting_analyzer.meetings WHERE id = :m"),
                {"m": meeting_id},
            ).scalar_one()
        if file_row is None:
            raise TranscriptionError(
                "The meeting audio track is missing; ingest the recording again."
            )

        with tempfile.TemporaryDirectory(prefix="tma-transcribe-") as temp_dir:
            with self._engine.connect() as conn:
                audio_path = _materialize_processing_audio(conn, dict(file_row), Path(temp_dir), "audio")

            def progress(percent: float, message: str) -> None:
                self._update_progress(job_id, percent, message)

            def cancelled() -> bool:
                return self._is_cancel_requested(job_id)

            result = self._transcription_service.transcribe(
                audio_path,
                progress_cb=progress,
                cancel_cb=cancelled,
            )

        self._raise_if_cancel_requested(job_id)
        fallback_from, fallback_reason = (None, None)
        provider = getattr(self._transcription_service, "_provider", None)
        if provider is not None and hasattr(provider, "fallback_info"):
            fallback_from, fallback_reason = provider.fallback_info()
        producer = getattr(provider, "last_provider", None) or provider
        producer_name = getattr(producer, "provider_name", "unknown")
        producer_model = getattr(producer, "model_name", "unknown")
        producer_mode = getattr(producer, "transcript_mode", None)

        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE meeting_analyzer.transcripts SET is_primary = false WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            conn.execute(
                text("DELETE FROM meeting_analyzer.action_items WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            conn.execute(
                text("DELETE FROM meeting_analyzer.summaries WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            transcript_id = conn.execute(
                text(
                    "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, mode, "
                    "language, is_primary, fallback_from, fallback_reason, text, "
                    "segments) VALUES (gen_random_uuid(), :m, :provider, :model, "
                    ":mode, :language, true, :fallback_from, :fallback_reason, :text, "
                    "CAST(:segments AS jsonb)) RETURNING id"
                ),
                {
                    "m": meeting_id,
                    "provider": producer_name,
                    "model": producer_model,
                    "mode": producer_mode,
                    "language": result.language,
                    "fallback_from": fallback_from,
                    "fallback_reason": fallback_reason,
                    "text": result.text,
                    "segments": _segments_json(result.segments),
                },
            ).scalar_one()
            try:
                metrics = compute_transcript_metrics(
                    segments=list(result.segments or []),
                    audio_duration_s=duration / 1000.0 if duration else None,
                    transcription_duration_s=self._job_latency_seconds(job_id, conn),
                    transcription_status="succeeded",
                    error=None,
                    fallback_from=fallback_from,
                    fallback_reason=fallback_reason,
                )
                conn.execute(
                    text(
                        "INSERT INTO meeting_analyzer.transcript_metrics (id, meeting_id, "
                        "transcript_id, metrics) VALUES (gen_random_uuid(), :m, "
                        ":t, CAST(:metrics AS jsonb)) "
                        "ON CONFLICT (transcript_id) DO UPDATE SET "
                        "metrics = EXCLUDED.metrics, computed_at = now()"
                    ),
                    {"m": meeting_id, "t": transcript_id, "metrics": _json(metrics)},
                )
            except Exception:
                logger.warning(
                    "Post-transcription metrics computation failed for meeting %s; continuing without metrics.",
                    meeting_id,
                    exc_info=True,
                )
            conn.execute(
                text(
                    "UPDATE meeting_analyzer.meetings SET status = 'transcribed', language = "
                    "COALESCE(:language, language) WHERE id = :m"
                ),
                {"m": meeting_id, "language": result.language},
            )
            conn.execute(
                text(
                    """
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = 'succeeded', progress = 1.0, message = 'Transcription complete',
                        finished_at = now(), updated_at = now(), heartbeat_at = now(),
                        error = NULL, error_code = NULL, error_detail = NULL,
                        fallback_from = :fallback_from, fallback_reason = :fallback_reason
                    WHERE id = :id
                    """
                ),
                {
                    "id": job_id,
                    "fallback_from": fallback_from,
                    "fallback_reason": fallback_reason,
                },
            )
        logger.info("Transcription job %s succeeded for meeting %s.", job_id, meeting_id)

        if self._summarizer is not None and self._summarizer.provider_configured:
            analysis_job_id = self._enqueue_analysis(meeting_id)
            try:
                self.run_job(analysis_job_id)
            except Exception:
                logger.exception("Chained analysis failed after transcription.")

        if self._speaker_profiler is not None and self._speaker_profiler.enabled:
            self._enqueue_profiling(meeting_id)

    def _job_latency_seconds(self, job_id: str, conn: Connection) -> float | None:
        value = conn.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM (now() - started_at)) "
                "FROM meeting_analyzer.processing_jobs WHERE id = :id AND started_at IS NOT NULL"
            ),
            {"id": job_id},
        ).scalar()
        return float(value) if value is not None else None

    def _enqueue_analysis(self, meeting_id: str) -> str:
        with self._engine.begin() as conn:
            active = conn.execute(text(
                "SELECT id FROM meeting_analyzer.processing_jobs WHERE meeting_id=:m "
                "AND job_type='analysis' AND status IN ('queued','running','cancelling') "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"m": meeting_id}).scalar_one_or_none()
            if active is not None:
                return str(active)
            return str(conn.execute(
                text(
                    "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status, message, attempt_count, max_attempts, heartbeat_at, updated_at) "
                    "VALUES (gen_random_uuid(), :m, 'analysis', 'queued', 'Queued for analysis', 0, :max_attempts, now(), now()) RETURNING id"
                ),
                {"m": meeting_id, "max_attempts": DEFAULT_MAX_ATTEMPTS},
            ).scalar_one())

    def _enqueue_profiling(self, meeting_id: str) -> str:
        with self._engine.begin() as conn:
            active = conn.execute(text(
                "SELECT id FROM meeting_analyzer.processing_jobs WHERE meeting_id=:m "
                "AND job_type='profiling' AND status IN ('queued','running','cancelling') "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"m": meeting_id}).scalar_one_or_none()
            if active is not None:
                return str(active)
            created = conn.execute(
                text(
                    "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status, "
                    "message, attempt_count, max_attempts, heartbeat_at, updated_at) "
                    "VALUES (gen_random_uuid(), :m, 'profiling', 'queued', 'Queued for voice profiling', 0, :max_attempts, now(), now()) "
                    "ON CONFLICT (meeting_id, job_type) WHERE status IN ('queued', 'running', 'cancelling') "
                    "DO NOTHING RETURNING id"
                ),
                {"m": meeting_id, "max_attempts": DEFAULT_MAX_ATTEMPTS},
            ).scalar_one_or_none()
            if created is not None:
                return str(created)
            return str(conn.execute(text(
                "SELECT id FROM meeting_analyzer.processing_jobs WHERE meeting_id=:m "
                "AND job_type='profiling' AND status IN ('queued','running','cancelling') "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"m": meeting_id}).scalar_one())

    # -- analysis -----------------------------------------------------------

    def _run_analysis(self, job_id: str, meeting_id: str) -> None:
        if self._summarizer is None:
            raise SummarizationError("No analysis backend is configured.")
        self._mark_job(job_id, "running", message="Analysing the meeting...")
        self._raise_if_cancel_requested(job_id)

        with self._engine.begin() as conn:
            primary = (
                conn.execute(
                    text(
                        "SELECT id, text, language FROM meeting_analyzer.transcripts WHERE meeting_id = :m "
                        "AND is_primary = true"
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )
            if primary is None:
                raise SummarizationError(
                    "This meeting has no primary transcript yet. Transcribe it first."
                )

        def progress(percent: float, message: str) -> None:
            self._update_progress(job_id, percent, message)

        analysis = self._summarizer.analyze(str(primary["text"]), progress_cb=progress)
        self._raise_if_cancel_requested(job_id)
        action_items = analysis.get("action_items") or []
        with self._engine.begin() as conn:
            conn.execute(text("UPDATE meeting_analyzer.meetings SET status = 'analyzing' WHERE id = :m"), {"m": meeting_id})
            conn.execute(text("DELETE FROM meeting_analyzer.summaries WHERE meeting_id = :m"), {"m": meeting_id})
            summary_id = conn.execute(
                text(
                    "INSERT INTO meeting_analyzer.summaries (id, meeting_id, transcript_id, overview, "
                    "payload, model) VALUES (gen_random_uuid(), :m, :transcript_id, "
                    ":overview, CAST(:payload AS jsonb), :model) RETURNING id"
                ),
                {
                    "m": meeting_id,
                    "transcript_id": primary["id"],
                    "overview": analysis.get("overview"),
                    "payload": _json(analysis),
                    "model": getattr(self._summarizer, "model_name", "unknown"),
                },
            ).scalar_one()
        try:
            with self._engine.begin() as conn:
                sync_result = sync_action_items_to_tasks(
                    conn, meeting_id=meeting_id, transcript_id=str(primary["id"]), action_items=action_items
                )
                conn.execute(text("DELETE FROM meeting_analyzer.action_items WHERE meeting_id = :m"), {"m": meeting_id})
                for position, item in enumerate(action_items):
                    conn.execute(text(
                        "INSERT INTO meeting_analyzer.action_items (id, meeting_id, summary_id, task, owner, deadline, evidence, status, position) "
                        "VALUES (gen_random_uuid(), :m, :s, :task, :owner, :deadline, :evidence, 'open', :position)"
                    ), {
                        "m": meeting_id, "s": summary_id, "task": item["task"], "owner": item.get("owner"),
                        "deadline": item.get("deadline"), "evidence": item.get("evidence"), "position": position,
                    })
                conn.execute(text("UPDATE meeting_analyzer.meetings SET status = 'completed' WHERE id = :m"), {"m": meeting_id})
                conn.execute(text(
                    """
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = 'succeeded', progress = 1.0, message = :msg, finished_at = now(),
                        updated_at = now(), heartbeat_at = now(), error = NULL, error_code = NULL, error_detail = NULL
                    WHERE id = :id
                    """
                ), {"id": job_id, "msg": f"Analysis complete; synced {sync_result.get('created',0)} tasks"})
        except TaskSyncError as exc:
            logger.exception("Task sync failed after analysis for meeting %s", meeting_id)
            self._handle_job_failure(job_id, {"meeting_id": meeting_id, "job_type": "analysis"}, exc)
            return
        logger.info("Analysis job %s succeeded for meeting %s.", job_id, meeting_id)

    # -- voice profiling ------------------------------------------------------

    def _run_profiling(self, job_id: str, meeting_id: str) -> None:
        if self._speaker_profiler is None:
            raise SpeakerProfilingError("Voice profiling is not wired up.")
        self._mark_job(job_id, "running", message="Profiling speakers...")
        self._raise_if_cancel_requested(job_id)

        with self._engine.begin() as conn:
            primary = (
                conn.execute(
                    text(
                        "SELECT id, segments FROM meeting_analyzer.transcripts WHERE meeting_id = :m "
                        "AND is_primary = true"
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )
            if primary is None:
                raise SpeakerProfilingError("This meeting has no primary transcript yet. Transcribe it first.")
            transcript_id = str(primary["id"])
            segments = list(primary["segments"] or [])
            labeled = [
                segment for segment in segments
                if isinstance(segment, dict) and isinstance(segment.get("speaker"), str) and segment.get("speaker", "").strip()
            ]
            file_row = (
                conn.execute(
                    text(
                        """
                        SELECT lo_oid, kind, mime_type, codec
                        FROM meeting_analyzer.meeting_files
                        WHERE meeting_id = :m AND kind IN ('audio_opus', 'derived_audio_16k')
                        ORDER BY CASE kind WHEN 'audio_opus' THEN 0 ELSE 1 END
                        LIMIT 1
                        """
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )

        if not labeled:
            self._finish_profiling(job_id, "No diarized speakers in this transcript; nothing to profile.")
            return
        if file_row is None:
            raise SpeakerProfilingError("The meeting audio track is missing; voice profiles cannot be computed.")

        def progress(percent: float, message: str) -> None:
            self._update_progress(job_id, 0.1 + 0.8 * percent, message)

        try:
            with tempfile.TemporaryDirectory(prefix="tma-profile-") as temp_dir:
                with self._engine.connect() as conn:
                    audio_path = _materialize_processing_audio(conn, dict(file_row), Path(temp_dir), "audio")
                self._raise_if_cancel_requested(job_id)
                stored = self._speaker_profiler.compute_and_store(
                    meeting_id=meeting_id,
                    transcript_id=transcript_id,
                    audio_path=audio_path,
                    segments=segments,
                    progress_cb=progress,
                )
        except SpeakerProfilingError as exc:
            try:
                self._speaker_profiler.mark_profiling_error(
                    meeting_id=meeting_id, transcript_id=transcript_id, error=str(exc)
                )
            except Exception:
                logger.exception("Could not persist profiling error diagnostics for meeting %s.", meeting_id)
            raise

        if not stored:
            self._finish_profiling(job_id, "Speakers present but too little speech per speaker to profile.")
            return
        self._finish_profiling(job_id, f"Voice profiles computed for {len(stored)} speaker(s).")
        logger.info("Profiling job %s succeeded for meeting %s (%d speakers).", job_id, meeting_id, len(stored))

    def _finish_profiling(self, job_id: str, message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = 'succeeded', progress = 1.0, message = :message, finished_at = now(),
                        updated_at = now(), heartbeat_at = now(), error = NULL, error_code = NULL, error_detail = NULL
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "message": message},
            )

    # -- job bookkeeping ----------------------------------------------------

    def _mark_job(self, job_id: str, status: str, *, message: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = :status, message = :message,
                        started_at = COALESCE(started_at, now()), updated_at = now(), heartbeat_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "status": status, "message": message},
            )

    def _heartbeat(self, job_id: str) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text("UPDATE meeting_analyzer.processing_jobs SET heartbeat_at = now(), updated_at = now() WHERE id = :id"),
                    {"id": job_id},
                )
        except Exception:
            logger.exception("Heartbeat update failed for job %s.", job_id)

    def _update_progress(self, job_id: str, percent: float, message: str) -> None:
        now = time.monotonic()
        last = self._progress_log.get(job_id)
        if last is not None and now - last < _PROGRESS_MIN_INTERVAL:
            return
        self._progress_log[job_id] = now
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE meeting_analyzer.processing_jobs
                        SET progress = :p, message = :m, updated_at = now(), heartbeat_at = now()
                        WHERE id = :id AND status IN ('queued', 'running', 'cancelling')
                        """
                    ),
                    {"id": job_id, "p": float(percent), "m": message},
                )
        except Exception:
            logger.exception("Could not record progress for job %s.", job_id)

    def _is_cancel_requested(self, job_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, cancel_requested_at FROM meeting_analyzer.processing_jobs WHERE id = :id"),
                {"id": job_id},
            ).mappings().one_or_none()
        if row is None:
            return False
        return row["status"] in ("cancelling", "cancelled") or row["cancel_requested_at"] is not None

    def _raise_if_cancel_requested(self, job_id: str) -> None:
        if self._is_cancel_requested(job_id):
            raise TranscriptionCancelled("Cancelled at the user's request.")

    def _confirm_cancelled(self, job_id: str, message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = 'cancelled', message = :message, error = :message, error_code = 'cancelled',
                        error_detail = NULL, finished_at = COALESCE(finished_at, now()), updated_at = now(), heartbeat_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "message": message},
            )

    def _handle_job_failure(self, job_id: str, job: dict[str, Any], exc: Exception) -> None:
        retryable = _is_transient_error(exc)
        error_code = _error_code_for(exc, retryable=retryable)
        # Pull latest attempt info from DB (runner increments attempt_count on claim).
        with self._engine.begin() as conn:
            current = conn.execute(
                text("SELECT meeting_id, job_type, attempt_count, max_attempts, cancel_requested_at, status FROM meeting_analyzer.processing_jobs WHERE id=:id"),
                {"id": job_id},
            ).mappings().one_or_none()
            if current is None:
                return
            current = dict(current)
            if current.get("cancel_requested_at") is not None or current.get("status") == "cancelling":
                self._confirm_cancelled(job_id, "Cancelled at the user's request.")
                return
            # Preserve profiling diagnostics for unexpected failures.
            if str(current.get("job_type")) == "profiling" and self._speaker_profiler is not None and not isinstance(exc, (SpeakerProfilingError, TranscriptionCancelled)):
                try:
                    primary = conn.execute(
                        text("SELECT id FROM meeting_analyzer.transcripts WHERE meeting_id=:m AND is_primary=true"),
                        {"m": str(current.get("meeting_id"))},
                    ).mappings().one_or_none()
                    if primary is not None:
                        self._speaker_profiler.mark_profiling_error(
                            meeting_id=str(current.get("meeting_id")),
                            transcript_id=str(primary["id"]),
                            error=f"Unexpected error: {type(exc).__name__}: {exc}",
                        )
                except Exception:
                    logger.exception("Could not mark profiling error diagnostics.")
            attempts = int(current.get("attempt_count") or 0)
            max_attempts = int(current.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
            attempts_exhausted = attempts >= max_attempts
            user_message = _user_message_for(exc, retryable=retryable, attempts_exhausted=attempts_exhausted)
            will_retry = retryable and not attempts_exhausted
            error_detail = {
                "type": exc.__class__.__name__,
                "retryable": retryable,
                "attempt": attempts,
                "max_attempts": max_attempts,
                "will_retry": will_retry,
            }
            self._fail_or_retry_locked(
                conn,
                job_id=job_id,
                job=current,
                exc=exc,
                error_code=error_code,
                retryable=will_retry,
                user_message=user_message,
                error_detail=error_detail,
            )

    def _fail_or_retry_locked(
        self,
        conn: Connection,
        *,
        job_id: str,
        job: dict[str, Any],
        exc: Exception,
        error_code: str,
        retryable: bool,
        user_message: str,
        error_detail: dict[str, Any] | None = None,
    ) -> None:
        meeting_id = str(job.get("meeting_id")) if job.get("meeting_id") else None
        job_type = job.get("job_type")
        if error_detail is None:
            attempts = int(job.get("attempt_count") or 0)
            max_attempts = int(job.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
            error_detail = {
                "type": exc.__class__.__name__,
                "retryable": retryable,
                "attempt": attempts,
                "max_attempts": max_attempts,
                "will_retry": retryable,
            }
        if retryable:
            attempt = int(error_detail.get("attempt") or 1)
            backoff_idx = min(max(attempt - 1, 0), len(RETRY_BACKOFF_SECONDS) - 1)
            delay = RETRY_BACKOFF_SECONDS[backoff_idx]
            conn.execute(
                text(
                    """
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = 'queued', message = :message, error = :error, error_code = :error_code,
                        error_detail = CAST(:error_detail AS jsonb), next_run_at = now() + (:delay * interval '1 second'),
                        finished_at = NULL, updated_at = now(), heartbeat_at = now(), progress = GREATEST(progress, 0)
                    WHERE id = :id
                    """
                ),
                {
                    "id": job_id,
                    "message": f"Transient failure; retrying in {delay}s (attempt {attempt}/{error_detail.get('max_attempts', DEFAULT_MAX_ATTEMPTS)}).",
                    "error": user_message,
                    "error_code": error_code,
                    "error_detail": json.dumps(error_detail),
                    "delay": delay,
                },
            )
            logger.warning("Job %s failed transiently (%s); queued for retry in %ss.", job_id, error_code, delay)
            return

        conn.execute(
            text(
                """
                UPDATE meeting_analyzer.processing_jobs
                SET status = 'failed', error = :error, error_code = :error_code,
                    error_detail = CAST(:error_detail AS jsonb), message = :error,
                    finished_at = now(), updated_at = now(), heartbeat_at = now(), next_run_at = NULL
                WHERE id = :id
                """
            ),
            {
                "id": job_id,
                "error": user_message,
                "error_code": error_code,
                "error_detail": json.dumps(error_detail),
            },
        )
        if meeting_id and job_type in ("transcription", "analysis"):
            self._apply_meeting_status_after_failure(conn, meeting_id)
        logger.info("Job %s marked failed (%s).", job_id, error_code)

    def _apply_meeting_status_after_failure(self, conn: Connection, meeting_id: str) -> None:
        has_transcript = conn.execute(
            text("SELECT 1 FROM meeting_analyzer.transcripts WHERE meeting_id = :m AND is_primary = true LIMIT 1"),
            {"m": meeting_id},
        ).scalar_one_or_none()
        has_summary = conn.execute(
            text("SELECT 1 FROM meeting_analyzer.summaries WHERE meeting_id = :m LIMIT 1"),
            {"m": meeting_id},
        ).scalar_one_or_none()
        if has_summary and has_transcript:
            conn.execute(text("UPDATE meeting_analyzer.meetings SET status = 'completed' WHERE id = :m"), {"m": meeting_id})
            return
        if has_transcript:
            conn.execute(text("UPDATE meeting_analyzer.meetings SET status = 'transcribed' WHERE id = :m AND status <> 'completed'"), {"m": meeting_id})
            return
        conn.execute(text("UPDATE meeting_analyzer.meetings SET status = 'failed' WHERE id = :m AND status NOT IN ('completed')"), {"m": meeting_id})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _segments_json(segments: list[dict[str, Any]]) -> str:
    return _json(segments)
