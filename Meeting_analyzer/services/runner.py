"""Background job runner: claims queued jobs and executes them.

Claims use ``FOR UPDATE SKIP LOCKED`` so only one worker ever owns a job, even
if two server processes are started by mistake.  For the MVP one worker thread
processes jobs sequentially; the SQL contract supports scaling later.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.pipeline import TranscriptionPipeline

logger = logging.getLogger(__name__)

_CLAIM_SQL = text(
    "UPDATE meeting_analyzer.processing_jobs SET status = 'running', started_at = now(), "
    "updated_at = now() "
    "WHERE id = ("
    "  SELECT id FROM meeting_analyzer.processing_jobs"
    "  WHERE status = 'queued'"
    "  ORDER BY created_at"
    "  FOR UPDATE SKIP LOCKED LIMIT 1"
    ") RETURNING id, meeting_id, job_type"
)


class JobRunner:
    """Poll for queued jobs and run them through the pipeline."""

    def __init__(
        self,
        engine: Engine,
        pipeline: TranscriptionPipeline,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self._engine = engine
        self._pipeline = pipeline
        self._poll_seconds = poll_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
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

    # -- internals ----------------------------------------------------------

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._claim_one()
            except Exception:
                logger.exception("Job claim failed; retrying shortly.")
                self._stop_event.wait(self._poll_seconds)
                continue
            if job_id is None:
                self._stop_event.wait(self._poll_seconds)
                continue
            try:
                self._pipeline.run_job(job_id)
            except Exception:
                logger.exception("Job %s crashed the pipeline.", job_id)

    def _claim_one(self) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(_CLAIM_SQL).mappings().one_or_none()
        if row is None:
            return None
        logger.info("Claimed job %s (%s).", row["id"], row["job_type"])
        return str(row["id"])
