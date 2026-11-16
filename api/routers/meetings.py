"""Meetings: list, search, create (recording ingest), details, deletion.

Deletion is permanent (hard delete).  Two-step confirmation lives in the UI;
the API endpoint itself deletes relationally and the ``meeting_files`` trigger
unlinks every Large Object in the same transaction.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import text

from api import errors
from api.deps import ConnDep, ProfileDep, require_host
from api.schemas import MeetingOut
from services.ingest import IngestError, ingest_recording

router = APIRouter(prefix="/meetings", tags=["meetings"])

_STATUS_VALUES = {
    "recording", "recorded", "transcribed", "analyzing", "completed", "failed"
}


@router.get("", response_model=list[MeetingOut])
def list_meetings(
    conn: ConnDep,
    query: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[str | None, Query()] = None,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    """List meetings, newest first, with an optional text search.

    The text search looks into titles, the primary transcript and the summary
    overview, matching the dashboard's search box.
    """

    if status is not None and status not in _STATUS_VALUES:
        raise errors.bad_request("invalid_status", "Unknown meeting status filter.")
    sql = """
        SELECT m.id, m.user_id, m.title, m.status, m.recorded_at, m.duration_ms,
               m.language, m.notes
        FROM meetings m
        WHERE (CAST(:user_id AS uuid) IS NULL OR m.user_id = CAST(:user_id AS uuid))
          AND (CAST(:status AS text) IS NULL OR m.status = CAST(:status AS text))
          AND (
            CAST(:query AS text) IS NULL
            OR m.title ILIKE '%' || :query || '%'
            OR EXISTS (SELECT 1 FROM transcripts t
                       WHERE t.meeting_id = m.id AND t.is_primary
                         AND t.text ILIKE '%' || :query || '%')
            OR EXISTS (SELECT 1 FROM summaries s
                       WHERE s.meeting_id = m.id
                         AND s.overview ILIKE '%' || :query || '%')
          )
        ORDER BY m.recorded_at DESC
        LIMIT :limit OFFSET :offset
    """
    rows = (
        conn.execute(
            text(sql),
            {"query": query, "status": status, "user_id": user_id,
             "limit": limit, "offset": offset},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@router.post("", response_model=MeetingOut, status_code=201)
def create_meeting(
    profile: ProfileDep,
    _: Annotated[None, Depends(require_host)],
    conn: ConnDep,
    title: Annotated[str, Form(min_length=1, max_length=300)],
    file: Annotated[UploadFile, File()],
    duration_ms: Annotated[int, Form(ge=0)] = 0,
    sample_rate: Annotated[int, Form(ge=8000, le=192_000)] = 48_000,
    channels: Annotated[int, Form(ge=1, le=2)] = 2,
    user_id: Annotated[uuid.UUID | None, Form()] = None,
    notes: Annotated[str | None, Form(max_length=4000)] = None,
) -> dict:
    """Host-only: ingest a finished recording.

    Stores the original WAV and its 16 kHz mono derivative as Large Objects
    and queues a transcription job.  Note: audio is sent to Sarvam (cloud)
    during transcription; participant consent is disclosed in the UI.
    """

    effective_user = user_id or profile.get("id")
    if effective_user is None:
        raise errors.bad_request(
            "user_required", "Select or create a profile before recording."
        )
    if conn.execute(
        text("SELECT 1 FROM users WHERE id = :id"), {"id": effective_user}
    ).scalar_one_or_none() is None:
        raise errors.not_found("profile_not_found", "Unknown profile.")

    # Spool the upload to a temp file so ingest can stream it into a Large
    # Object without holding multi-GB recordings in memory.
    with tempfile.TemporaryDirectory(prefix="tma-upload-") as temp_dir:
        target = tempfile.NamedTemporaryFile(
            dir=temp_dir, suffix=".wav", delete=False
        )
        try:
            uploaded_bytes = 0
            while chunk := file.file.read(4 * 1024 * 1024):
                uploaded_bytes += len(chunk)
                target.write(chunk)
            target.close()
            if uploaded_bytes <= 44:
                raise errors.bad_request(
                    "empty_recording", "The uploaded recording is empty."
                )
            try:
                meeting_id = ingest_recording(
                    conn,
                    user_id=str(effective_user),
                    title=title,
                    original_path=Path(target.name),
                    duration_ms=duration_ms,
                    sample_rate=sample_rate,
                    channels=channels,
                    original_filename=file.filename or "recording.wav",
                )
            except IngestError as exc:
                raise errors.bad_request("ingest_failed", str(exc)) from exc
            if notes is not None:
                conn.execute(
                    text("UPDATE meetings SET notes = :n WHERE id = :m"),
                    {"n": notes, "m": uuid.UUID(meeting_id)},
                )
        finally:
            import os

            os.unlink(target.name)

    row = (
        conn.execute(
            text(
                "SELECT id, user_id, title, status, recorded_at, duration_ms, "
                "language, notes FROM meetings WHERE id = :id"
            ),
            {"id": meeting_id},
        )
        .mappings()
        .one()
    )
    return dict(row)


@router.get("/{meeting_id}", response_model=MeetingOut)
def get_meeting(meeting_id: uuid.UUID, conn: ConnDep) -> dict:
    row = (
        conn.execute(
            text(
                "SELECT id, user_id, title, status, recorded_at, duration_ms, "
                "language, notes FROM meetings WHERE id = :id"
            ),
            {"id": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")
    return dict(row)


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(
    _: Annotated[None, Depends(require_host)],
    meeting_id: uuid.UUID,
    conn: ConnDep,
    profile: ProfileDep,
) -> None:
    """Host-only, permanent deletion (no soft delete, no retention period).

    Deleting the meeting cascades to transcripts, summaries, action items,
    attachments and job history; the ``meeting_files`` trigger unlinks each
    Large Object in the same transaction.  Backups taken before this deletion
    keep the removed data until they expire.
    """

    result = conn.execute(
        text("DELETE FROM meetings WHERE id = :id"), {"id": meeting_id}
    )
    if result.rowcount == 0:
        raise errors.not_found("meeting_not_found", "Meeting not found.")
