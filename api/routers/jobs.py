"""Job progress and cancellation for API polling."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter
from sqlalchemy import text

from api import errors
from api.deps import ConnDep
from api.schemas import JobOut

router = APIRouter(tags=["jobs"])


@router.get("/meetings/{meeting_id}/jobs", response_model=list[JobOut])
def list_jobs(meeting_id: uuid.UUID, conn: ConnDep) -> list[dict]:
    rows = (
        conn.execute(
            text(
                "SELECT id, meeting_id, job_type, status, progress, message, error, "
                "provider, model, fallback_from, fallback_reason, created_at, "
                "finished_at FROM processing_jobs WHERE meeting_id = :m "
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
    """Host-only: request cooperative cancellation of an active job.

    The pipeline checks the status between work steps and raises, so the job
    lands in the ``cancelled`` state without side effects on the meeting.
    """

    row = (
        conn.execute(
            text(
                "UPDATE processing_jobs SET status = 'cancelled', "
                "error = :error, message = 'Cancelling...', finished_at = now() "
                "WHERE id = :id AND meeting_id = :m AND status IN ('queued', 'running') "
                "RETURNING id, meeting_id, job_type, status, progress, message, error, "
                "provider, model, fallback_from, fallback_reason, created_at, finished_at"
            ),
            {
                "id": job_id,
                "m": meeting_id,
                # Bound, not inlined: the apostrophe in "user's" used to break
                # the SQL literal and turned every cancel into a 503.
                "error": "Cancelled at the user's request.",
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.not_found("job_not_cancellable", "Job is not active.")
    return dict(row)
