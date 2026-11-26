"""Server-side processing pipeline for transcription and analysis.

Job lifecycle (``processing_jobs``):

    queued -> running -> succeeded | failed | cancelled

* A transcription job streams the meeting's derived 16 kHz WAV out of its
  Large Object to a temporary file, runs the transcription provider (Sarvam
  primary with automatic Whisper fallback), and stores the result as the
  meeting's single primary transcript.
* An analysis job summarises the **primary Roman-script transcript** with the
  configured analysis provider (Groq) and stores the validated analysis plus
  individual action-item rows.

Cancellation is cooperative: the provider receives a ``cancel_cb`` that reads
the job's status from the database, and progress is throttled into the job
row so API clients can poll it.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ai.summarization import MeetingSummarizer, SummarizationError
from ai.transcription import (
    CancelCallback,
    NoSpeechError,
    ProgressCallback,
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


class PipelineError(RuntimeError):
    """Raised when a job cannot be completed."""


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
        # Per-instance progress throttle state: job id -> last monotonic update.
        self._progress_log: dict[str, float] = {}

    # -- job execution ------------------------------------------------------

    def run_job(self, job_id: str) -> None:
        """Execute one claimed job to a terminal state."""

        with self._engine.begin() as conn:
            job = (
                conn.execute(
                    text(
                        "SELECT meeting_id, job_type FROM meeting_analyzer.processing_jobs WHERE id = :id"
                    ),
                    {"id": job_id},
                )
                .mappings()
                .one_or_none()
            )
        if job is None:
            raise PipelineError(f"Job {job_id} vanished.")
        try:
            if job["job_type"] == "transcription":
                self._run_transcription(job_id, str(job["meeting_id"]))
            elif job["job_type"] == "analysis":
                self._run_analysis(job_id, str(job["meeting_id"]))
            elif job["job_type"] == "profiling":
                self._run_profiling(job_id, str(job["meeting_id"]))
            else:
                raise PipelineError(f"Unknown job type {job['job_type']!r}.")
        except TranscriptionCancelled:
            self._fail_job(job_id, "cancelled", "Cancelled at the user's request.")
        except (
            TranscriptionError,
            NoSpeechError,
            SummarizationError,
            SpeakerProfilingError,
        ) as exc:
            self._fail_job(job_id, "failed", str(exc))
        except Exception as exc:  # unexpected - keep the server alive
            logger.exception("Job %s failed unexpectedly.", job_id)
            if str(job.get("job_type")) == "profiling" and self._speaker_profiler is not None:
                try:
                    with self._engine.connect() as conn:
                        primary = (
                            conn.execute(
                                text(
                                    "SELECT id FROM meeting_analyzer.transcripts WHERE meeting_id=:m "
                                    "AND is_primary=true"
                                ),
                                {"m": str(job["meeting_id"])},
                            )
                            .mappings()
                            .one_or_none()
                        )
                    if primary is not None:
                        self._speaker_profiler.mark_profiling_error(
                            meeting_id=str(job["meeting_id"]),
                            transcript_id=str(primary["id"]),
                            error=f"Unexpected error: {type(exc).__name__}: {exc}",
                        )
                except Exception:
                    logger.exception("Could not mark profiling error diagnostics.")
            self._fail_job(
                job_id, "failed", f"Unexpected error: {type(exc).__name__}: {exc}"
            )

    # -- transcription ------------------------------------------------------

    def _run_transcription(self, job_id: str, meeting_id: str) -> None:
        self._mark_job(job_id, "running", message="Transcribing...")

        with self._engine.begin() as conn:
            file_row = (
                conn.execute(
                    text(
                        "SELECT lo_oid FROM meeting_analyzer.meeting_files WHERE meeting_id = :m "
                        "AND kind = 'derived_audio_16k'"
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
                "The 16 kHz audio track is missing; ingest the recording again."
            )

        with tempfile.TemporaryDirectory(prefix="tma-transcribe-") as temp_dir:
            audio_path = Path(temp_dir) / "audio-16k-mono.wav"
            with self._engine.connect() as conn:
                with open(audio_path, "wb") as target:
                    lo.copy_lo_to_stream(conn, int(file_row["lo_oid"]), target)

            def progress(percent: float, message: str) -> None:
                self._update_progress(job_id, percent, message)

            def cancelled() -> bool:
                return self._is_job_cancelled(job_id)

            result = self._transcription_service.transcribe(
                audio_path,
                progress_cb=progress,
                cancel_cb=cancelled,
            )

        fallback_from, fallback_reason = (None, None)
        provider = getattr(self._transcription_service, "_provider", None)
        if provider is not None and hasattr(provider, "fallback_info"):
            fallback_from, fallback_reason = provider.fallback_info()
        producer = getattr(provider, "last_provider", None) or provider
        producer_name = getattr(producer, "provider_name", "unknown")
        producer_model = getattr(producer, "model_name", "unknown")
        producer_mode = getattr(producer, "transcript_mode", None)

        with self._engine.begin() as conn:
            # Exactly one primary transcript: demote any previous one first.
            conn.execute(
                text("UPDATE meeting_analyzer.transcripts SET is_primary = false WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            # The previous analysis was derived from the transcript that is
            # being replaced.  Drop it (and its items) so saved notes can never
            # disagree with the new primary transcript; analysis re-runs and
            # recreates both.
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
            # Post-transcription metrics: computed from the stored transcript
            # and the job's own timing, persisted per run.  Re-transcription
            # replaces the previous metrics (one row per transcript).  A
            # metrics failure must never fail transcription.
            try:
                metrics = compute_transcript_metrics(
                    segments=list(result.segments or []),
                    audio_duration_s=duration / 1000.0 if duration else None,
                    transcription_duration_s=self._job_latency_seconds(
                        job_id, conn
                    ),
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
                    {
                        "m": meeting_id,
                        "t": transcript_id,
                        "metrics": _json(metrics),
                    },
                )
            except Exception:
                logger.warning(
                    "Post-transcription metrics computation failed for "
                    "meeting %s; continuing without metrics.",
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
                    "UPDATE meeting_analyzer.processing_jobs SET status = 'succeeded', progress = 1.0, "
                    "message = 'Transcription complete', finished_at = now(), "
                    "fallback_from = :fallback_from, fallback_reason = :fallback_reason "
                    "WHERE id = :id"
                ),
                {
                    "id": job_id,
                    "fallback_from": fallback_from,
                    "fallback_reason": fallback_reason,
                },
            )
        logger.info(
            "Transcription job %s succeeded for meeting %s.", job_id, meeting_id
        )

        # Chain analysis automatically when an analysis backend is configured.
        if self._summarizer is not None and self._summarizer.provider_configured:
            analysis_job_id = self._enqueue_analysis(meeting_id)
            try:
                self.run_job(analysis_job_id)
            except Exception:  # analysis failure must not lose the transcript
                logger.exception("Chained analysis failed after transcription.")

        # Queue profiling separately so the worker can run it after this
        # transcription job has completed. A profiling failure never fails the
        # transcript or its meeting status.
        if self._speaker_profiler is not None and self._speaker_profiler.enabled:
            self._enqueue_profiling(meeting_id)

    def _job_latency_seconds(self, job_id: str, conn: Connection) -> float | None:
        """Seconds since the job started running (measured, not estimated)."""

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
            return conn.execute(
                text(
                    "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status, "
                    "message) VALUES (gen_random_uuid(), :m, 'analysis', 'queued', "
                    "'Queued for analysis') RETURNING id"
                ),
                {"m": meeting_id},
            ).scalar_one()

    def _enqueue_profiling(self, meeting_id: str) -> str:
        with self._engine.begin() as conn:
            active = conn.execute(text(
                "SELECT id FROM meeting_analyzer.processing_jobs WHERE meeting_id=:m "
                "AND job_type='profiling' AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"m": meeting_id}).scalar_one_or_none()
            if active is not None:
                return str(active)
            created = conn.execute(
                text(
                    "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status, "
                    "message) VALUES (gen_random_uuid(), :m, 'profiling', 'queued', "
                    "'Queued for voice profiling') "
                    "ON CONFLICT (meeting_id, job_type) WHERE status IN ('queued', 'running') "
                    "DO NOTHING RETURNING id"
                ),
                {"m": meeting_id},
            ).scalar_one_or_none()
            if created is not None:
                return str(created)
            return str(conn.execute(text(
                "SELECT id FROM meeting_analyzer.processing_jobs WHERE meeting_id=:m "
                "AND job_type='profiling' AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"m": meeting_id}).scalar_one())

    # -- analysis -----------------------------------------------------------

    def _run_analysis(self, job_id: str, meeting_id: str) -> None:
        if self._summarizer is None:
            raise SummarizationError("No analysis backend is configured.")

        self._mark_job(job_id, "running", message="Analysing the meeting...")

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

        analysis = self._summarizer.analyze(
            str(primary["text"]), progress_cb=progress
        )
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
                    "UPDATE meeting_analyzer.processing_jobs SET status = 'succeeded', progress = 1.0, message = :msg, finished_at = now() WHERE id = :id"
                ), {"id": job_id, "msg": f"Analysis complete; synced {sync_result.get('created',0)} tasks"})
        except TaskSyncError as exc:
            logger.exception("Task sync failed after analysis for meeting %s", meeting_id)
            self._fail_job(job_id, "failed", f"Analysis saved, but task sync failed: {exc}")
            return
        logger.info("Analysis job %s succeeded for meeting %s.", job_id, meeting_id)

    # -- voice profiling ------------------------------------------------------

    def _run_profiling(self, job_id: str, meeting_id: str) -> None:
        """Compute per-speaker voice embeddings for the primary transcript."""

        if self._speaker_profiler is None:
            raise SpeakerProfilingError("Voice profiling is not wired up.")

        self._mark_job(job_id, "running", message="Profiling speakers...")

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
                raise SpeakerProfilingError(
                    "This meeting has no primary transcript yet. Transcribe it first."
                )
            transcript_id = str(primary["id"])
            segments = list(primary["segments"] or [])
            labeled = [
                segment
                for segment in segments
                if isinstance(segment, dict)
                and isinstance(segment.get("speaker"), str)
                and segment.get("speaker", "").strip()
            ]
            file_row = (
                conn.execute(
                    text(
                        "SELECT lo_oid FROM meeting_analyzer.meeting_files WHERE meeting_id = :m "
                        "AND kind = 'derived_audio_16k'"
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )

        if not labeled:
            self._finish_profiling(
                job_id,
                "No diarized speakers in this transcript; nothing to profile.",
            )
            logger.info(
                "Profiling job %s finished without speakers (meeting %s).",
                job_id,
                meeting_id,
            )
            return
        if file_row is None:
            raise SpeakerProfilingError(
                "The 16 kHz audio track is missing; voice profiles cannot be computed."
            )

        def progress(percent: float, message: str) -> None:
            self._update_progress(job_id, 0.1 + 0.8 * percent, message)

        try:
            with tempfile.TemporaryDirectory(prefix="tma-profile-") as temp_dir:
                audio_path = Path(temp_dir) / "audio-16k-mono.wav"
                with self._engine.connect() as conn:
                    with open(audio_path, "wb") as target:
                        lo.copy_lo_to_stream(conn, int(file_row["lo_oid"]), target)
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
                logger.exception(
                    "Could not persist profiling error diagnostics for meeting %s.",
                    meeting_id,
                )
            raise

        if not stored:
            self._finish_profiling(
                job_id,
                "Speakers present but too little speech per speaker to profile.",
            )
            return
        self._finish_profiling(
            job_id, f"Voice profiles computed for {len(stored)} speaker(s)."
        )
        logger.info(
            "Profiling job %s succeeded for meeting %s (%d speakers).",
            job_id,
            meeting_id,
            len(stored),
        )

    def _finish_profiling(self, job_id: str, message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE meeting_analyzer.processing_jobs SET status = 'succeeded', progress = 1.0, "
                    "message = :message, finished_at = now() WHERE id = :id"
                ),
                {"id": job_id, "message": message},
            )

    # -- job bookkeeping ----------------------------------------------------

    def _mark_job(self, job_id: str, status: str, *, message: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE meeting_analyzer.processing_jobs SET status = :status, message = :message, "
                    "started_at = COALESCE(started_at, now()), updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": job_id, "status": status, "message": message},
            )

    def _update_progress(self, job_id: str, percent: float, message: str) -> None:
        # Throttled in-memory so a chatty provider cannot hammer the DB.
        now = time.monotonic()
        last = self._progress_log.get(job_id)
        if last is not None and now - last < _PROGRESS_MIN_INTERVAL:
            return
        self._progress_log[job_id] = now
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE meeting_analyzer.processing_jobs SET progress = :p, message = :m, "
                        "updated_at = now() WHERE id = :id"
                    ),
                    {"id": job_id, "p": float(percent), "m": message},
                )
        except Exception:  # progress updates must never kill a job
            logger.exception("Could not record progress for job %s.", job_id)

    def _is_job_cancelled(self, job_id: str) -> bool:
        with self._engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM meeting_analyzer.processing_jobs WHERE id = :id"),
                {"id": job_id},
            ).scalar_one_or_none()
        return status == "cancelled"

    def _fail_job(self, job_id: str, status: str, error: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE meeting_analyzer.processing_jobs SET status = :status, error = :error, "
                    "message = :error, finished_at = now() WHERE id = :id"
                ),
                {"id": job_id, "status": status, "error": error},
            )
            meeting_id = conn.execute(
                text("SELECT meeting_id FROM meeting_analyzer.processing_jobs WHERE id = :id"),
                {"id": job_id},
            ).scalar_one_or_none()
            if status == "failed" and meeting_id:
                job_type = conn.execute(
                    text("SELECT job_type FROM meeting_analyzer.processing_jobs WHERE id = :id"),
                    {"id": job_id},
                ).scalar_one_or_none()
                if job_type in ("transcription", "analysis"):
                    conn.execute(
                        text(
                            "UPDATE meeting_analyzer.meetings SET status = 'failed' WHERE id = :m AND "
                            "status NOT IN ('completed')"
                        ),
                        {"m": meeting_id},
                    )


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _segments_json(segments: list[dict[str, Any]]) -> str:
    return _json(segments)
