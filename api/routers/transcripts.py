"""Transcript versions and re-transcription enqueue."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text

from api import errors
from api.deps import ConnDep, require_host
from api.schemas import JobOut, TranscriptOut

router = APIRouter(prefix="/meetings", tags=["transcripts"])


def _meeting_exists(conn, meeting_id) -> None:
    if conn.execute(
        text("SELECT 1 FROM meetings WHERE id = :m"), {"m": meeting_id}
    ).scalar_one_or_none() is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")


@router.get("/{meeting_id}/transcripts", response_model=list[TranscriptOut])
def list_transcripts(meeting_id: uuid.UUID, conn: ConnDep) -> list[dict]:
    _meeting_exists(conn, meeting_id)
    rows = (
        conn.execute(
            text(
                "SELECT id, meeting_id, provider, model, mode, language, is_primary, "
                "fallback_from, fallback_reason, text, segments, created_at "
                "FROM transcripts WHERE meeting_id = :m ORDER BY created_at DESC"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@router.post("/{meeting_id}/transcribe", response_model=JobOut, status_code=202)
def transcribe(
    _: Annotated[None, Depends(require_host)],
    meeting_id: uuid.UUID,
    conn: ConnDep,
) -> dict:
    """Host-only: (re-)queue transcription (Sarvam primary, Whisper fallback)."""

    _meeting_exists(conn, meeting_id)
    original = conn.execute(
        text(
            "SELECT 1 FROM meeting_files WHERE meeting_id = :m AND "
            "kind = 'original_audio_48k'"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if original is None:
        raise errors.bad_request(
            "no_audio", "This meeting has no stored audio to transcribe."
        )
    active = conn.execute(
        text(
            "SELECT 1 FROM processing_jobs WHERE meeting_id = :m AND "
            "job_type = 'transcription' AND status IN ('queued', 'running')"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if active is not None:
        raise errors.ApiError(409, "job_active", "A transcription job is already queued.")
    row = (
        conn.execute(
            text(
                "INSERT INTO processing_jobs (id, meeting_id, job_type, status, message) "
                "VALUES (gen_random_uuid(), :m, 'transcription', 'queued', "
                "'Queued for transcription') RETURNING id, meeting_id, job_type, "
                "status, progress, message, error, provider, model, fallback_from, "
                "fallback_reason, created_at, finished_at"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one()
    )
    return dict(row)
