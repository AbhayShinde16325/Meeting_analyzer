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
    ) -> None:
        self._engine = engine
        self._transcription_service = transcription_service
        self._summarizer = summarizer
        # Per-instance progress throttle state: job id -> last monotonic update.
        self._progress_log: dict[str, float] = {}

    # -- job execution ------------------------------------------------------

    def run_job(self, job_id: str) -> None:
        """Execute one claimed job to a terminal state."""

        with self._engine.begin() as conn:
            job = (
                conn.execute(
                    text(
                        "SELECT meeting_id, job_type FROM processing_jobs WHERE id = :id"
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
            else:
                raise PipelineError(f"Unknown job type {job['job_type']!r}.")
        except TranscriptionCancelled:
            self._fail_job(job_id, "cancelled", "Cancelled at the user's request.")
        except (TranscriptionError, NoSpeechError, SummarizationError) as exc:
            self._fail_job(job_id, "failed", str(exc))
        except Exception as exc:  # unexpected - keep the server alive
            logger.exception("Job %s failed unexpectedly.", job_id)
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
                        "SELECT lo_oid FROM meeting_files WHERE meeting_id = :m "
                        "AND kind = 'derived_audio_16k'"
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )
            duration = conn.execute(
                text("SELECT duration_ms FROM meetings WHERE id = :m"),
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
                text("UPDATE transcripts SET is_primary = false WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            # The previous analysis was derived from the transcript that is
            # being replaced.  Drop it (and its items) so saved notes can never
            # disagree with the new primary transcript; analysis re-runs and
            # recreates both.
            conn.execute(
                text("DELETE FROM action_items WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            conn.execute(
                text("DELETE FROM summaries WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            transcript_id = conn.execute(
                text(
                    "INSERT INTO transcripts (id, meeting_id, provider, model, mode, "
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
            conn.execute(
                text(
                    "UPDATE meetings SET status = 'transcribed', language = "
                    "COALESCE(:language, language) WHERE id = :m"
                ),
                {"m": meeting_id, "language": result.language},
            )
            conn.execute(
                text(
                    "UPDATE processing_jobs SET status = 'succeeded', progress = 1.0, "
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

    def _enqueue_analysis(self, meeting_id: str) -> str:
        with self._engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO processing_jobs (id, meeting_id, job_type, status, "
                    "message) VALUES (gen_random_uuid(), :m, 'analysis', 'queued', "
                    "'Queued for analysis') RETURNING id"
                ),
                {"m": meeting_id},
            ).scalar_one()

    # -- analysis -----------------------------------------------------------

    def _run_analysis(self, job_id: str, meeting_id: str) -> None:
        if self._summarizer is None:
            raise SummarizationError("No analysis backend is configured.")

        self._mark_job(job_id, "running", message="Analysing the meeting...")

        with self._engine.begin() as conn:
            primary = (
                conn.execute(
                    text(
                        "SELECT id, text, language FROM transcripts WHERE meeting_id = :m "
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
            # Re-analysis replaces the previous result: the schema keeps one
            # summary per meeting, and derived notes must always describe the
            # current primary transcript.  Item rows are removed explicitly
            # (their FK is ON DELETE SET NULL, which would orphan them).
            conn.execute(
                text("DELETE FROM action_items WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            conn.execute(
                text("DELETE FROM summaries WHERE meeting_id = :m"),
                {"m": meeting_id},
            )
            summary_id = conn.execute(
                text(
                    "INSERT INTO summaries (id, meeting_id, transcript_id, overview, "
                    "payload, model) VALUES (gen_random_uuid(), :m, :transcript_id, "
                    ":overview, CAST(:payload AS jsonb), :model) RETURNING id"
                ),
                {
                    "m": meeting_id,
                    "transcript_id": primary["id"],
                    "overview": analysis.get("overview"),
                    "payload": _json(analysis),
                    "model": self._summarizer.provider_model_name or "unknown",
                },
            ).scalar_one()
            for position, item in enumerate(action_items):
                conn.execute(
                    text(
                        "INSERT INTO action_items (id, meeting_id, summary_id, task, "
                        "owner, deadline, evidence, position) VALUES "
                        "(gen_random_uuid(), :m, :summary_id, :task, :owner, "
                        ":deadline, :evidence, :position)"
                    ),
                    {
                        "m": meeting_id,
                        "summary_id": summary_id,
                        "task": item.get("task") or "",
                        "owner": item.get("owner"),
                        "deadline": item.get("deadline"),
                        "evidence": item.get("evidence"),
                        "position": position,
                    },
                )
            conn.execute(
                text(
                    "UPDATE meetings SET status = 'completed' WHERE id = :m"
                ),
                {"m": meeting_id},
            )
            conn.execute(
                text(
                    "UPDATE processing_jobs SET status = 'succeeded', progress = 1.0, "
                    "message = 'Analysis complete', finished_at = now() WHERE id = :id"
                ),
                {"id": job_id},
            )
        logger.info("Analysis job %s succeeded for meeting %s.", job_id, meeting_id)

    # -- job bookkeeping ----------------------------------------------------

    def _mark_job(self, job_id: str, status: str, *, message: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE processing_jobs SET status = :status, message = :message, "
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
                        "UPDATE processing_jobs SET progress = :p, message = :m, "
                        "updated_at = now() WHERE id = :id"
                    ),
                    {"id": job_id, "p": float(percent), "m": message},
                )
        except Exception:  # progress updates must never kill a job
            logger.exception("Could not record progress for job %s.", job_id)

    def _is_job_cancelled(self, job_id: str) -> bool:
        with self._engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM processing_jobs WHERE id = :id"),
                {"id": job_id},
            ).scalar_one_or_none()
        return status == "cancelled"

    def _fail_job(self, job_id: str, status: str, error: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE processing_jobs SET status = :status, error = :error, "
                    "message = :error, finished_at = now() WHERE id = :id"
                ),
                {"id": job_id, "status": status, "error": error},
            )
            meeting_id = conn.execute(
                text("SELECT meeting_id FROM processing_jobs WHERE id = :id"),
                {"id": job_id},
            ).scalar_one_or_none()
            if status == "failed" and meeting_id:
                conn.execute(
                    text(
                        "UPDATE meetings SET status = 'failed' WHERE id = :m AND "
                        "status NOT IN ('completed')"
                    ),
                    {"m": meeting_id},
                )


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _segments_json(segments: list[dict[str, Any]]) -> str:
    return _json(segments)
