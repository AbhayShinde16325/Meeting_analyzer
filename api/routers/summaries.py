"""Summaries and action items.

Summaries are always produced from the meeting's primary Roman-script
transcript; the stored row records which transcript it was built from.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import text

from api import errors
from api.deps import ConnDep
from api.schemas import ActionItemOut, ActionItemPatch, JobOut, SummaryOut

router = APIRouter(prefix="/meetings", tags=["summaries"])
action_router = APIRouter(prefix="/action-items", tags=["summaries"])


@router.get("/{meeting_id}/summary", response_model=SummaryOut | None)
def get_summary(meeting_id: uuid.UUID, conn: ConnDep) -> dict | None:
    row = (
        conn.execute(
            text(
                "SELECT id, meeting_id, transcript_id, overview, payload, model, "
                "created_at FROM summaries WHERE meeting_id = :m"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


@router.post("/{meeting_id}/analyse", response_model=JobOut, status_code=202)
def analyse(
    meeting_id: uuid.UUID,
    conn: ConnDep,
) -> dict:
    """(Re-)queue analysis of the primary transcript."""

    if conn.execute(
        text("SELECT 1 FROM meetings WHERE id = :m"), {"m": meeting_id}
    ).scalar_one_or_none() is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")
    primary = conn.execute(
        text(
            "SELECT 1 FROM transcripts WHERE meeting_id = :m AND is_primary = true"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if primary is None:
        raise errors.bad_request(
            "no_transcript", "Transcribe the meeting before requesting analysis."
        )
    active = conn.execute(
        text(
            "SELECT 1 FROM processing_jobs WHERE meeting_id = :m AND "
            "job_type = 'analysis' AND status IN ('queued', 'running')"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if active is not None:
        raise errors.ApiError(409, "job_active", "An analysis job is already queued.")
    row = (
        conn.execute(
            text(
                "INSERT INTO processing_jobs (id, meeting_id, job_type, status, "
                "message) VALUES (gen_random_uuid(), :m, 'analysis', 'queued', "
                "'Queued for analysis') RETURNING id, meeting_id, job_type, status, "
                "progress, message, error, provider, model, fallback_from, "
                "fallback_reason, created_at, finished_at"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one()
    )
    return dict(row)


@router.get("/{meeting_id}/action-items", response_model=list[ActionItemOut])
def list_action_items(meeting_id: uuid.UUID, conn: ConnDep) -> list[dict]:
    rows = (
        conn.execute(
            text(
                "SELECT id, meeting_id, task, owner, deadline, evidence, status, "
                "position FROM action_items WHERE meeting_id = :m "
                "ORDER BY position, created_at"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@action_router.patch("/{item_id}", response_model=ActionItemOut)
def patch_action_item(
    item_id: uuid.UUID,
    payload: ActionItemPatch,
    conn: ConnDep,
) -> dict:
    """Host-only: edit or re-status an action item (e.g. mark done)."""

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise errors.bad_request("empty_patch", "Nothing to update.")
    assignments = ", ".join(f"{key} = :{key}" for key in fields)
    row = (
        conn.execute(
            text(
                f"UPDATE action_items SET {assignments} WHERE id = :id RETURNING "
                "id, meeting_id, task, owner, deadline, evidence, status, position"
            ),
            {**fields, "id": item_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.not_found("item_not_found", "Action item not found.")
    return dict(row)
