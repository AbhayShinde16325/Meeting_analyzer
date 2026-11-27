"""Job progress, cancellation, and manual requeue for API polling."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import text

from api import errors
from api.deps import ConnDep
from api.schemas import JobOut

router = APIRouter(tags=["jobs"])

_JOB_FIELDS = """
    id, meeting_id, job_type, status, progress, message, error, error_code, error_detail,
    provider, model, fallback_from, fallback_reason, attempt_count, max_attempts,
    next_run_at, heartbeat_at, cancel_requested_at, created_at, started_at, finished_at, updated_at
"""


@router.get("/meetings/{meeting_id}/jobs", response_model=list[JobOut])
def list_jobs(meeting_id: uuid.UUID, conn: ConnDep) -> list[dict]:
    rows = (
        conn.execute(
            text(
                f"SELECT {_JOB_FIELDS} FROM meeting_analyzer.processing_jobs WHERE meeting_id = :m "
                "ORDER BY created_at DESC LIMIT 20"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@router.post("/meetings/{meeting_id}/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(
    meeting_id: uuid.UUID,
    job_id: uuid.UUID,
    conn: ConnDep,
) -> dict:
    """Request cooperative cancellation of an active job.

    Queued jobs are cancelled immediately because no worker is processing them.
    Running jobs move to ``cancelling``; the worker confirms terminal
    ``cancelled`` once it has reached a safe checkpoint.
    """

    existing = (
        conn.execute(
            text(
                f"SELECT {_JOB_FIELDS} FROM meeting_analyzer.processing_jobs WHERE id = :id AND meeting_id = :m"
            ),
            {"id": job_id, "m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    if existing is None:
        raise errors.not_found("job_not_found", "Job not found.")
    if existing["status"] in ("cancelling", "cancelled"):
        return dict(existing)
    if existing["status"] not in ("queued", "running"):
        raise errors.conflict("job_not_cancellable", "Job is not active and cannot be cancelled.")

    if existing["status"] == "queued":
        row = (
            conn.execute(
                text(
                    f"""
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = 'cancelled',
                        cancel_requested_at = COALESCE(cancel_requested_at, now()),
                        message = 'Cancelled at the user''s request.',
                        error = 'Cancelled at the user''s request.',
                        error_code = 'cancelled',
                        error_detail = NULL,
                        finished_at = COALESCE(finished_at, now()),
                        updated_at = now(),
                        heartbeat_at = now()
                    WHERE id = :id AND meeting_id = :m AND status = 'queued'
                    RETURNING {_JOB_FIELDS}
                    """
                ),
                {"id": job_id, "m": meeting_id},
            )
            .mappings()
            .one_or_none()
        )
    else:
        row = (
            conn.execute(
                text(
                    f"""
                    UPDATE meeting_analyzer.processing_jobs
                    SET status = 'cancelling',
                        cancel_requested_at = COALESCE(cancel_requested_at, now()),
                        message = 'Cancellation requested...',
                        error = NULL,
                        error_code = NULL,
                        error_detail = NULL,
                        updated_at = now(),
                        heartbeat_at = now()
                    WHERE id = :id AND meeting_id = :m AND status = 'running'
                    RETURNING {_JOB_FIELDS}
                    """
                ),
                {"id": job_id, "m": meeting_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise errors.conflict("job_not_cancellable", "Job state changed; please refresh.")
    return dict(row)


@router.post("/meetings/{meeting_id}/jobs/{job_id}/requeue", response_model=JobOut)
def requeue_job(
    meeting_id: uuid.UUID,
    job_id: uuid.UUID,
    conn: ConnDep,
) -> dict:
    """Manually requeue a terminal failed/cancelled job.

    This is an explicit operator/user action. It does not delete transcripts,
    summaries, audio, or other meeting data. The job is moved back to queued and
    will be claimed by the worker when eligible.
    """

    existing = (
        conn.execute(
            text(
                f"SELECT {_JOB_FIELDS} FROM meeting_analyzer.processing_jobs WHERE id = :id AND meeting_id = :m"
            ),
            {"id": job_id, "m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    if existing is None:
        raise errors.not_found("job_not_found", "Job not found.")
    if existing["status"] not in ("failed", "cancelled"):
        raise errors.conflict("job_not_requeueable", "Only failed or cancelled jobs can be requeued.")

    active = conn.execute(
        text(
            """
            SELECT id FROM meeting_analyzer.processing_jobs
            WHERE meeting_id = :m AND job_type = :t AND status IN ('queued', 'running', 'cancelling')
              AND id <> :id
            LIMIT 1
            """
        ),
        {"m": meeting_id, "t": existing["job_type"], "id": job_id},
    ).scalar_one_or_none()
    if active is not None:
        raise errors.conflict("job_already_active", "An active job of this type already exists for the meeting.")

    row = (
        conn.execute(
            text(
                f"""
                UPDATE meeting_analyzer.processing_jobs
                SET status = 'queued',
                    progress = 0,
                    message = 'Queued for manual retry',
                    error = NULL,
                    error_code = NULL,
                    error_detail = NULL,
                    next_run_at = NULL,
                    cancel_requested_at = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    heartbeat_at = now(),
                    updated_at = now(),
                    attempt_count = 0
                WHERE id = :id AND meeting_id = :m AND status IN ('failed', 'cancelled')
                RETURNING {_JOB_FIELDS}
                """
            ),
            {"id": job_id, "m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.conflict("job_not_requeueable", "Job could not be requeued.")
    return dict(row)
