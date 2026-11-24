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

from fastapi import APIRouter, File, Form, Query, UploadFile
from sqlalchemy import text

from api import errors
from api.deps import ConnDep, ProfileDep
from api.schemas import MeetingOut
from services.ingest import IngestError, ingest_recording, ingest_multi_source_recording, normalize_uploaded_audio

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
        FROM meeting_analyzer.meetings m
        WHERE (CAST(:user_id AS uuid) IS NULL OR m.user_id = CAST(:user_id AS uuid))
          AND (CAST(:status AS text) IS NULL OR m.status = CAST(:status AS text))
          AND (
            CAST(:query AS text) IS NULL
            OR m.title ILIKE '%' || :query || '%'
            OR EXISTS (SELECT 1 FROM meeting_analyzer.transcripts t
                       WHERE t.meeting_id = m.id AND t.is_primary
                         AND t.text ILIKE '%' || :query || '%')
            OR EXISTS (SELECT 1 FROM meeting_analyzer.summaries s
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
    conn: ConnDep,
    title: Annotated[str, Form(min_length=1, max_length=300)],
    file: Annotated[UploadFile, File()],
    duration_ms: Annotated[int, Form(ge=0)] = 0,
    sample_rate: Annotated[int, Form(ge=8000, le=192_000)] = 48_000,
    channels: Annotated[int, Form(ge=1, le=2)] = 2,
    user_id: Annotated[uuid.UUID | None, Form()] = None,
    notes: Annotated[str | None, Form(max_length=4000)] = None,
) -> dict:
    """Ingest a finished recording.

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
        text("SELECT 1 FROM meeting_analyzer.users WHERE id = :id"), {"id": effective_user}
    ).scalar_one_or_none() is None:
        raise errors.not_found("profile_not_found", "Unknown profile.")

    # Spool upload to disk to avoid holding large audio in memory.
    raw_name = file.filename or "recording"
    suffix = Path(raw_name).suffix.lower()
    if not suffix:
        ct = (file.content_type or "").lower()
        suffix = {
            "audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg",
            "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a", "audio/wav": ".wav", "audio/x-wav": ".wav",
            "audio/flac": ".flac",
        }.get(ct.split(";")[0].strip(), ".upload")
    with tempfile.TemporaryDirectory(prefix="tma-upload-") as temp_dir:
        target = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=suffix, delete=False)
        try:
            uploaded_bytes = 0
            while chunk := file.file.read(4 * 1024 * 1024):
                uploaded_bytes += len(chunk)
                target.write(chunk)
            target.close()
            if uploaded_bytes <= 44:
                raise errors.bad_request("empty_recording", "The uploaded recording is empty.")
            try:
                meeting_id = ingest_recording(
                    conn,
                    user_id=str(effective_user),
                    title=title,
                    original_path=Path(target.name),
                    duration_ms=duration_ms,
                    sample_rate=sample_rate,
                    channels=channels,
                    mime_type=file.content_type or "application/octet-stream",
                    original_filename=raw_name,
                )
            except IngestError as exc:
                raise errors.bad_request("ingest_failed", str(exc)) from exc
            if notes is not None:
                conn.execute(text("UPDATE meeting_analyzer.meetings SET notes = :n WHERE id = :m"), {"n": notes, "m": uuid.UUID(meeting_id)})
        finally:
            try:
                Path(target.name).unlink(missing_ok=True)
            except OSError:
                pass

    row = (
        conn.execute(
            text(
                "SELECT id, user_id, title, status, recorded_at, duration_ms, "
                "language, notes FROM meeting_analyzer.meetings WHERE id = :id"
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
                "language, notes FROM meeting_analyzer.meetings WHERE id = :id"
            ),
            {"id": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")
    return dict(row)


@router.post("/multi-source", response_model=MeetingOut, status_code=201)
def create_multi_source_meeting(
    profile: ProfileDep,
    conn: ConnDep,
    title: Annotated[str, Form(min_length=1, max_length=300)],
    user_id: Annotated[uuid.UUID | None, Form()] = None,
    notes: Annotated[str | None, Form(max_length=4000)] = None,
    duration_ms: Annotated[int, Form(ge=0)] = 0,
    source_manifest: Annotated[str | None, Form()] = None,
    microphone: Annotated[UploadFile | None, File()] = None,
    system: Annotated[UploadFile | None, File()] = None,
    browser: Annotated[UploadFile | None, File()] = None,
    mixed: Annotated[UploadFile | None, File()] = None,
) -> dict:
    """Ingest synchronized multi-source recording."""
    effective_user = user_id or profile.get("id")
    if effective_user is None:
        raise errors.bad_request("user_required", "Select or create a profile before recording.")
    if conn.execute(text("SELECT 1 FROM meeting_analyzer.users WHERE id = :id"), {"id": effective_user}).scalar_one_or_none() is None:
        raise errors.not_found("profile_not_found", "Unknown profile.")

    uploads = [("microphone", microphone), ("system", system), ("browser", browser), ("mixed", mixed)]
    uploads = [(n, f) for n, f in uploads if f is not None]
    if not uploads:
        raise errors.bad_request("missing_audio", "Provide at least one audio source.")

    import json
    manifest = {}
    if source_manifest:
        try:
            manifest = json.loads(source_manifest)
        except json.JSONDecodeError:
            raise errors.bad_request("invalid_manifest", "source_manifest must be valid JSON.")

    with tempfile.TemporaryDirectory(prefix="tma-multi-") as temp_dir:
        sources: dict[str, Path] = {}
        meta: dict[str, dict] = {}
        try:
            for name, uf in uploads:
                raw_name = uf.filename or f"{name}.webm"
                suffix = Path(raw_name).suffix.lower() or ".upload"
                target = tempfile.NamedTemporaryFile(dir=temp_dir, suffix=suffix, delete=False)
                size = 0
                while chunk := uf.file.read(4 * 1024 * 1024):
                    size += len(chunk)
                    target.write(chunk)
                target.close()
                if size <= 44:
                    raise errors.bad_request("empty_recording", f"Source '{name}' is empty.")
                sources[name] = Path(target.name)
                meta[name] = {"original_filename": raw_name, "mime_type": uf.content_type or "application/octet-stream", **(manifest.get(name, {}) if isinstance(manifest.get(name), dict) else {})}
            try:
                meeting_id = ingest_multi_source_recording(
                    conn, user_id=str(effective_user), title=title, sources=sources, source_meta=meta, duration_ms=duration_ms, notes=notes
                )
            except IngestError as exc:
                raise errors.bad_request("ingest_failed", str(exc)) from exc
        finally:
            for p in sources.values():
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
    row = conn.execute(text(
        "SELECT id, user_id, title, status, recorded_at, duration_ms, language, notes FROM meeting_analyzer.meetings WHERE id = :id"
    ), {"id": meeting_id}).mappings().one()
    return dict(row)


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(
    meeting_id: uuid.UUID,
    conn: ConnDep,
    profile: ProfileDep,
) -> None:
    """Permanent deletion (no soft delete, no retention period)."""

    result = conn.execute(
        text("DELETE FROM meeting_analyzer.meetings WHERE id = :id"), {"id": meeting_id}
    )
    if result.rowcount == 0:
        raise errors.not_found("meeting_not_found", "Meeting not found.")
