"""Transcript versions and re-transcription enqueue."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import text

from api import errors
from api.deps import ConnDep
from api.schemas import JobOut, TranscriptOut

router = APIRouter(prefix="/meetings", tags=["transcripts"])


def _meeting_exists(conn, meeting_id) -> None:
    if conn.execute(
        text("SELECT 1 FROM meeting_analyzer.meetings WHERE id = :m"), {"m": meeting_id}
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
                "FROM meeting_analyzer.transcripts WHERE meeting_id = :m ORDER BY created_at DESC"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@router.post("/{meeting_id}/transcribe", response_model=JobOut, status_code=202)
def transcribe(
    meeting_id: uuid.UUID,
    conn: ConnDep,
) -> dict:
    """(Re-)queue transcription (Sarvam primary, Whisper fallback)."""

    _meeting_exists(conn, meeting_id)
    audio = conn.execute(
        text(
            """
            SELECT 1 FROM meeting_analyzer.meeting_files
            WHERE meeting_id = :m AND kind IN ('audio_opus', 'derived_audio_16k', 'original_audio_48k')
            LIMIT 1
            """
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if audio is None:
        raise errors.bad_request(
            "no_audio", "This meeting has no stored audio to transcribe."
        )
    job_fields = """
        id, meeting_id, job_type, status, progress, message, error, error_code, error_detail,
        provider, model, fallback_from, fallback_reason, attempt_count, max_attempts,
        next_run_at, heartbeat_at, cancel_requested_at, created_at, started_at, finished_at, updated_at
    """
    active = conn.execute(
        text(
            "SELECT 1 FROM meeting_analyzer.processing_jobs WHERE meeting_id = :m AND "
            "job_type = 'transcription' AND status IN ('queued', 'running', 'cancelling')"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if active is not None:
        raise errors.conflict("job_active", "A transcription job is already active.")
    row = (
        conn.execute(
            text(
                f"""
                INSERT INTO meeting_analyzer.processing_jobs (
                    id, meeting_id, job_type, status, message, attempt_count, max_attempts, heartbeat_at, updated_at
                ) VALUES (gen_random_uuid(), :m, 'transcription', 'queued', 'Queued for transcription', 0, 3, now(), now())
                ON CONFLICT (meeting_id, job_type) WHERE status IN ('queued', 'running', 'cancelling')
                DO NOTHING RETURNING {job_fields}
                """
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.conflict("job_active", "A transcription job is already active.")
    return dict(row)
