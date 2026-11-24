"""Post-transcription quality metrics for the primary transcript."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from api import errors
from api.deps import ConnDep
from api.schemas import TranscriptMetricsOut

router = APIRouter(tags=["metrics"])


@router.get("/meetings/{meeting_id}/metrics", response_model=TranscriptMetricsOut | None)
def get_meeting_metrics(meeting_id: uuid.UUID, conn: ConnDep) -> dict[str, Any] | None:
    """Metrics for the meeting's primary transcript, or null when absent."""

    if conn.execute(
        text("SELECT 1 FROM meetings WHERE id = :m"), {"m": meeting_id}
    ).scalar_one_or_none() is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")

    row = (
        conn.execute(
            text(
                "SELECT tm.transcript_id, tm.metrics, tm.computed_at "
                "FROM transcript_metrics tm "
                "JOIN transcripts t ON t.id = tm.transcript_id "
                "WHERE tm.meeting_id = :m AND t.is_primary = true "
                "ORDER BY tm.computed_at DESC LIMIT 1"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return dict(row)
