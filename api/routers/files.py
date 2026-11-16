"""Meeting files: audio streaming/download and attachment management.

All binary content is streamed out of Large Objects.  ``GET /files/{id}``
supports HTTP Range requests so the desktop player can seek.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from api import errors
from api.deps import ConnDep, ProfileDep, require_host
from api.schemas import MeetingFileOut
from db import lo
from db.models import (
    ATTACHMENT_EXTENSIONS,
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MIME_TYPES,
)

router = APIRouter(tags=["files"])

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

_EXTENSION_BY_MIME = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def _load_file(conn, file_id) -> dict:
    row = (
        conn.execute(
            text(
                "SELECT id, meeting_id, kind, lo_oid, sha256, size_bytes, mime_type, "
                "original_filename, duration_ms, sample_rate, channels, created_at "
                "FROM meeting_files WHERE id = :id"
            ),
            {"id": file_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.not_found("file_not_found", "File not found.")
    return dict(row)


def _validate_attachment(filename: str, mime_type: str, size: int) -> None:
    """Backend validation for attachments: extension, MIME type and size."""

    lowered = (filename or "").lower()
    if not any(lowered.endswith(ext) for ext in ATTACHMENT_EXTENSIONS):
        raise errors.bad_request(
            "attachment_extension",
            "Attachments may be PDF, DOCX, XLSX, PPTX, TXT, PNG or JPG.",
        )
    if mime_type not in ATTACHMENT_MIME_TYPES:
        raise errors.bad_request(
            "attachment_mime", "This file type is not allowed as an attachment."
        )
    if size > ATTACHMENT_MAX_BYTES:
        raise errors.bad_request(
            "attachment_too_large", "Attachments are limited to 50 MB."
        )


@router.get("/meetings/{meeting_id}/files", response_model=list[MeetingFileOut])
def list_meeting_files(meeting_id: uuid.UUID, conn: ConnDep) -> list[dict]:
    rows = (
        conn.execute(
            text(
                "SELECT id, meeting_id, kind, sha256, size_bytes, mime_type, "
                "original_filename, duration_ms, sample_rate, channels, created_at "
                "FROM meeting_files WHERE meeting_id = :m ORDER BY created_at"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@router.post(
    "/meetings/{meeting_id}/files",
    response_model=MeetingFileOut,
    status_code=201,
)
def upload_attachment(
    _: Annotated[None, Depends(require_host)],
    meeting_id: uuid.UUID,
    conn: ConnDep,
    file: Annotated[UploadFile, File()],
) -> dict:
    """Host-only: attach a document or image (50 MB max, allowlisted types)."""

    if conn.execute(
        text("SELECT 1 FROM meetings WHERE id = :m"), {"m": meeting_id}
    ).scalar_one_or_none() is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")

    data = file.file.read(ATTACHMENT_MAX_BYTES + 1)
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise errors.bad_request(
            "attachment_too_large", "Attachments are limited to 50 MB."
        )
    filename = file.filename or "attachment"
    mime_type = file.content_type or "application/octet-stream"
    _validate_attachment(filename, mime_type, len(data))

    oid, sha256, size = lo.create_lo_from_bytes(conn, data)
    row = (
        conn.execute(
            text(
                "INSERT INTO meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                "size_bytes, mime_type, original_filename) VALUES "
                "(gen_random_uuid(), :m, 'attachment', :oid, :sha, :size, :mime, :name) "
                "RETURNING id, meeting_id, kind, sha256, size_bytes, mime_type, "
                "original_filename, duration_ms, sample_rate, channels, created_at"
            ),
            {"m": meeting_id, "oid": oid, "sha": sha256, "size": size,
             "mime": mime_type, "name": filename},
        )
        .mappings()
        .one()
    )
    return dict(row)


def _parse_range(request: Request, size: int) -> tuple[int, int] | None:
    header = request.headers.get("range")
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match:
        raise errors.ApiError(416, "invalid_range", "Unsupported Range header.")
    start_raw, end_raw = match.groups()
    start = int(start_raw) if start_raw else None
    end = int(end_raw) if end_raw else None
    if start is None:
        if end is None or end == 0:
            raise errors.ApiError(416, "invalid_range", "Unsupported Range header.")
        start = max(0, size - end)
        end = size - 1
    elif end is None:
        end = size - 1
    if start >= size or end < start:
        raise errors.ApiError(
            416, "invalid_range", "Requested range is outside the file."
        )
    return start, min(end, size - 1)


def _stream_lo(
    conn, oid: int, start: int, end: int, chunk_size: int = 1024 * 1024
) -> Iterator[bytes]:
    """Yield a byte range of a Large Object.

    The connection stays open for the lifetime of the response (FastAPI keeps
    generator-scoped dependencies alive), so it is released after the last
    chunk by the dependency teardown.
    """

    position = start
    remaining = end - start + 1
    while remaining > 0:
        chunk = lo.read_lo_range(
            conn, oid, position, min(chunk_size, remaining)
        )
        if not chunk:
            break
        yield chunk
        position += len(chunk)
        remaining -= len(chunk)


@router.get("/files/{file_id}")
def stream_file(
    file_id: uuid.UUID, request: Request, conn: ConnDep
) -> StreamingResponse:
    """Stream a file from storage; supports Range for audio seeking."""

    row = _load_file(conn, file_id)
    size = int(row["size_bytes"])
    oid = int(row["lo_oid"])
    content_type = row["mime_type"] or "application/octet-stream"
    range_ = _parse_range(request, size)
    if range_ is None:
        return StreamingResponse(
            _stream_lo(conn, oid, 0, size - 1),
            media_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(size),
                "X-File-SHA256": row["sha256"],
            },
        )
    start, end = range_
    return StreamingResponse(
        _stream_lo(conn, oid, start, end),
        media_type=content_type,
        status_code=206,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "X-File-SHA256": row["sha256"],
        },
    )


@router.get("/files/{file_id}/download")
def download_file(file_id: uuid.UUID, conn: ConnDep) -> StreamingResponse:
    row = _load_file(conn, file_id)
    filename = row["original_filename"] or f"file-{file_id}"
    size = int(row["size_bytes"])
    oid = int(row["lo_oid"])
    return StreamingResponse(
        _stream_lo(conn, oid, 0, size - 1),
        media_type=row["mime_type"] or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
            "X-File-SHA256": row["sha256"],
        },
    )


@router.delete("/files/{file_id}", status_code=204)
def delete_file(
    _: Annotated[None, Depends(require_host)],
    file_id: uuid.UUID,
    conn: ConnDep,
) -> None:
    """Host-only: delete one attachment (its LO is unlinked by the trigger)."""

    result = conn.execute(
        text(
            "DELETE FROM meeting_files WHERE id = :id AND kind = 'attachment'"
        ),
        {"id": file_id},
    )
    if result.rowcount == 0:
        raise errors.not_found("file_not_found", "Attachment not found.")
