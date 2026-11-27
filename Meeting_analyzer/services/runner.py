"""Background job runner: claims queued jobs and executes them.

Claims use ``FOR UPDATE SKIP LOCKED`` so only one worker ever owns a job, even
if two server processes are started by mistake.  For the MVP one worker thread
processes jobs sequentially; the SQL contract supports scaling later.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from services.pipeline import TranscriptionPipeline

logger = logging.getLogger(__name__)

STALE_RUNNING_SECONDS = 15 * 60
MAX_READONLY_BACKOFF_SECONDS = 60.0

_CLAIM_SQL = text(
    """
    UPDATE meeting_analyzer.processing_jobs
    SET status = 'running',
        started_at = COALESCE(started_at, now()),
        heartbeat_at = now(),
        updated_at = now(),
        attempt_count = attempt_count + 1,
        message = COALESCE(message, 'Running...')
    WHERE id = (
        SELECT id FROM meeting_analyzer.processing_jobs
        WHERE status = 'queued'
          AND (next_run_at IS NULL OR next_run_at <= now())
        ORDER BY next_run_at NULLS FIRST, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, meeting_id, job_type, attempt_count, max_attempts, cancel_requested_at
    """
)

_STALE_RECOVERY_SQL = text(
    """
    UPDATE meeting_analyzer.processing_jobs
    SET status = 'failed',
        error = :error,
        error_code = :error_code,
        error_detail = CAST(:error_detail AS jsonb),
        message = :error,
        finished_at = now(),
        updated_at = now(),
        heartbeat_at = now()
    WHERE id IN (
        SELECT id FROM meeting_analyzer.processing_jobs
        WHERE status IN ('running', 'cancelling')
          AND COALESCE(heartbeat_at, updated_at, started_at, created_at) < now() - (:stale_seconds * interval '1 second')
          AND (:exclude_id IS NULL OR id <> CAST(:exclude_id AS uuid))
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, meeting_id, job_type, status
    """
)


def _is_readonly_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).lower()
        sqlstate = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if sqlstate == "25006":
            return True
        if "read-only" in msg or "readonly" in msg or "read only" in msg:
            return True
        if "cannot execute" in msg and "read-only" in msg:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class JobRunner:
    """Poll for queued jobs and run them through the pipeline."""

    def __init__(
        self,
        engine: Engine,
        pipeline: TranscriptionPipeline,
        *,
        poll_seconds: float = 2.0,
        stale_running_seconds: int = STALE_RUNNING_SECONDS,
    ) -> None:
        self._engine = engine
        self._pipeline = pipeline
        self._poll_seconds = poll_seconds
        self._stale_running_seconds = stale_running_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._db_read_only = False
        self._degraded_reason: str | None = None
        self._last_error: str | None = None
        self._last_loop_at: datetime | None = None
        self._last_claim_at: datetime | None = None
        self._last_recovery_at: datetime | None = None
        self._current_job_id: str | None = None
        self._current_job_started_at: datetime | None = None
        self._readonly_backoff_seconds = self._poll_seconds

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="tma-job-runner", daemon=True
        )
        self._thread.start()
        logger.info("Job runner started.")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("Job runner stopped.")

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            now = datetime.now(timezone.utc)
            last_loop = self._last_loop_at
            stalled = False
            stall_seconds: float | None = None
            if last_loop is not None:
                stall_seconds = (now - last_loop).total_seconds()
                stalled = self._current_job_id is None and stall_seconds > max(30.0, self._poll_seconds * 6)
            return {
                "running": thread_alive and not self._stop_event.is_set(),
                "thread_alive": thread_alive,
                "db_read_only": self._db_read_only,
                "degraded_reason": self._degraded_reason,
                "last_error": self._last_error,
                "last_loop_at": last_loop.isoformat() if last_loop else None,
                "last_claim_at": self._last_claim_at.isoformat() if self._last_claim_at else None,
                "last_recovery_at": self._last_recovery_at.isoformat() if self._last_recovery_at else None,
                "current_job_id": self._current_job_id,
                "current_job_started_at": self._current_job_started_at.isoformat() if self._current_job_started_at else None,
                "stalled": stalled,
                "stall_seconds": stall_seconds,
                "stale_running_seconds": self._stale_running_seconds,
            }

    # -- internals ----------------------------------------------------------

    def _set_state(self, **kwargs: Any) -> None:
        with self._state_lock:
            for k, v in kwargs.items():
                setattr(self, f"_{k}" if not k.startswith("_") else k, v)

    def _mark_readonly(self, exc: BaseException) -> None:
        msg = f"Database is read-only/unwritable: {exc}"
        with self._state_lock:
            first = not self._db_read_only
            self._db_read_only = True
            self._degraded_reason = "database_read_only"
            self._last_error = str(exc)
        if first:
            logger.critical(
                "Job runner detected read-only database. Jobs will not be claimed or updated "
                "until the database is writable again. Error: %s",
                exc,
            )
        else:
            logger.warning("Job runner still blocked by read-only database: %s", exc)

    def _clear_readonly(self) -> None:
        with self._state_lock:
            was = self._db_read_only
            self._db_read_only = False
            if self._degraded_reason == "database_read_only":
                self._degraded_reason = None
            self._readonly_backoff_seconds = self._poll_seconds
        if was:
            logger.info("Job runner database writability restored.")

    def _run_forever(self) -> None:
        self._recover_stale_jobs_safe(reason="startup")
        while not self._stop_event.is_set():
            self._set_state(last_loop_at=datetime.now(timezone.utc))
            try:
                self._recover_stale_jobs_safe(reason="periodic")
                job = self._claim_one()
                self._clear_readonly()
            except Exception as exc:
                self._set_state(last_error=str(exc))
                if _is_readonly_error(exc):
                    self._mark_readonly(exc)
                    with self._state_lock:
                        delay = self._readonly_backoff_seconds
                        self._readonly_backoff_seconds = min(
                            MAX_READONLY_BACKOFF_SECONDS, max(self._poll_seconds, delay * 2)
                        )
                    self._stop_event.wait(delay)
                    continue
                logger.exception("Job claim/recovery failed; retrying shortly.")
                self._stop_event.wait(self._poll_seconds)
                continue

            if job is None:
                self._stop_event.wait(self._poll_seconds)
                continue

            job_id = str(job["id"])
            self._set_state(last_claim_at=datetime.now(timezone.utc), current_job_id=job_id, current_job_started_at=datetime.now(timezone.utc))
            try:
                self._pipeline.run_job(job_id)
                self._clear_readonly()
            except Exception as exc:
                self._set_state(last_error=str(exc))
                if _is_readonly_error(exc):
                    self._mark_readonly(exc)
                logger.exception("Job %s crashed the pipeline.", job_id)
                # Best-effort: try to mark failed if DB is writable. If this fails,
                # stale recovery will eventually handle it once DB is writable.
                try:
                    self._pipeline.fail_job_best_effort(
                        job_id,
                        error=f"Worker crashed while processing job: {exc}",
                        error_code="worker_crash",
                        retryable=False,
                    )
                except Exception:
                    logger.exception("Could not mark crashed job %s as failed.", job_id)
            finally:
                self._set_state(current_job_id=None, current_job_started_at=None, last_loop_at=datetime.now(timezone.utc))

    def _claim_one(self) -> dict[str, Any] | None:
        with self._engine.begin() as conn:
            row = conn.execute(_CLAIM_SQL).mappings().one_or_none()
        if row is None:
            return None
        logger.info(
            "Claimed job %s (%s, attempt %s/%s).",
            row["id"],
            row["job_type"],
            row.get("attempt_count"),
            row.get("max_attempts"),
        )
        return dict(row)

    def _recover_stale_jobs_safe(self, *, reason: str) -> None:
        try:
            recovered = self.recover_stale_jobs(reason=reason)
            self._clear_readonly()
            if recovered:
                logger.warning("Recovered %s stale running/cancelling job(s) (%s).", len(recovered), reason)
        except Exception as exc:
            self._set_state(last_error=str(exc))
            if _is_readonly_error(exc):
                self._mark_readonly(exc)
                return
            logger.exception("Stale job recovery failed (%s).", reason)

    def recover_stale_jobs(self, *, reason: str = "manual") -> list[dict[str, Any]]:
        import json

        detail = json.dumps({"reason": reason, "stale_seconds": self._stale_running_seconds})
        with self._state_lock:
            exclude_id = self._current_job_id
        with self._engine.begin() as conn:
            # Confirm queued jobs that were cancelled before a worker claimed them.
            conn.execute(
                text(
                    """
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = 'cancelled',
                        message = 'Cancelled at the user''s request.',
                        error = NULL,
                        error_code = 'cancelled',
                        error_detail = NULL,
                        finished_at = COALESCE(finished_at, now()),
                        updated_at = now(),
                        heartbeat_at = now()
                    WHERE status = 'cancelling' AND started_at IS NULL
                    """
                )
            )
            rows = conn.execute(
                _STALE_RECOVERY_SQL,
                {
                    "error": "Job was interrupted and marked failed after stale-running recovery. Please requeue manually if needed.",
                    "error_code": "stale_job_recovered",
                    "error_detail": detail,
                    "stale_seconds": self._stale_running_seconds,
                    "exclude_id": exclude_id,
                },
            ).mappings().all()
            recovered = [dict(r) for r in rows]
            # Apply meeting status policy for recovered transcription/analysis jobs.
            for row in recovered:
                if row.get("job_type") in ("transcription", "analysis"):
                    self._apply_meeting_status_after_failure(conn, str(row["meeting_id"]))
        self._set_state(last_recovery_at=datetime.now(timezone.utc))
        return recovered

    def _apply_meeting_status_after_failure(self, conn, meeting_id: str) -> None:
        # If prior valid transcript/summary exists, keep meeting completed/transcribed as appropriate.
        has_transcript = conn.execute(
            text("SELECT 1 FROM meeting_analyzer.transcripts WHERE meeting_id = :m AND is_primary = true LIMIT 1"),
            {"m": meeting_id},
        ).scalar_one_or_none()
        has_summary = conn.execute(
            text("SELECT 1 FROM meeting_analyzer.summaries WHERE meeting_id = :m LIMIT 1"),
            {"m": meeting_id},
        ).scalar_one_or_none()
        if has_summary and has_transcript:
            conn.execute(
                text("UPDATE meeting_analyzer.meetings SET status = 'completed' WHERE id = :m AND status <> 'completed'"),
                {"m": meeting_id},
            )
            return
        if has_transcript:
            conn.execute(
                text("UPDATE meeting_analyzer.meetings SET status = 'transcribed' WHERE id = :m AND status NOT IN ('completed', 'transcribed')"),
                {"m": meeting_id},
            )
            return
        conn.execute(
            text("UPDATE meeting_analyzer.meetings SET status = 'failed' WHERE id = :m AND status NOT IN ('completed')"),
            {"m": meeting_id},
        )
