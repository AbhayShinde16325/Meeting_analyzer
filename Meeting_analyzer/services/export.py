"""Meeting export: a self-contained ZIP with metadata, transcript, summary,
action items, attachments and (optionally) audio.

Large Objects are streamed into the archive through a seekable file wrapper so
multi-GB audio never loads into memory.
"""

from __future__ import annotations

import io
import json
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Connection

from api import errors


class _LargeObjectFile(io.RawIOBase):
    """A seekable, read-only file-like view of a Large Object."""

    def __init__(self, conn: Connection, oid: int, size: int) -> None:
        super().__init__()
        self._conn = conn
        self._oid = oid
        self._size = size
        self._position = 0
        self._buffer = b""
        self._buffer_offset = -1

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def _fill(self, offset: int, length: int) -> bytes:
        from db import lo

        if offset < 0 or offset >= self._size:
            return b""
        return lo.read_lo_range(
            self._conn, self._oid, offset, min(length, self._size - offset)
        )

    def read(self, size: int = -1) -> bytes:
        remaining = self._size - self._position
        if remaining <= 0:
            return b""
        want = remaining if size < 0 else min(size, remaining)
        chunk = self._fill(self._position, min(want, 4 * 1024 * 1024))
        self._position += len(chunk)
        return chunk

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._position = offset
        elif whence == io.SEEK_CUR:
            self._position += offset
        elif whence == io.SEEK_END:
            self._position = self._size + offset
        return self._position

    def tell(self) -> int:
        return self._position


def _meeting_rows(conn: Connection, meeting_id: uuid.UUID) -> dict:
    row = (
        conn.execute(
            text("SELECT * FROM meeting_analyzer.meetings WHERE id = :m"), {"m": meeting_id}
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")
    return dict(row)


def build_export(
    conn: Connection, meeting_id: uuid.UUID, *, include_audio: bool = True
) -> Iterator[bytes]:
    """Yield the export ZIP as chunks (FastAPI streams it to the client)."""

    meeting = _meeting_rows(conn, meeting_id)
    files = (
        conn.execute(
            text(
                "SELECT kind, lo_oid, sha256, size_bytes, mime_type, "
                "original_filename, sample_rate, channels FROM meeting_analyzer.meeting_files "
                "WHERE meeting_id = :m ORDER BY created_at"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .all()
    )
    transcripts = (
        conn.execute(
            text(
                "SELECT provider, model, mode, is_primary, fallback_from, text, "
                "segments, created_at FROM meeting_analyzer.transcripts WHERE meeting_id = :m "
                "ORDER BY is_primary DESC, created_at"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .all()
    )
    summary = (
        conn.execute(
            text(
                "SELECT overview, payload, model, created_at FROM meeting_analyzer.summaries "
                "WHERE meeting_id = :m"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    action_items = (
        conn.execute(
            text(
                "SELECT task, owner, deadline, evidence, status FROM meeting_analyzer.action_items "
                "WHERE meeting_id = :m ORDER BY position"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .all()
    )

    metadata = {
        "meeting": {k: v for k, v in meeting.items()},
        "transcripts": [dict(r) for r in transcripts],
        "summary": dict(summary) if summary else None,
        "action_items": [dict(r) for r in action_items],
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "meeting.json",
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        )
        for index, transcript in enumerate(transcripts):
            label = "primary" if transcript["is_primary"] else f"version-{index}"
            archive.writestr(
                f"transcripts/{label}.txt",
                str(transcript["text"]),
            )
        if summary is not None:
            archive.writestr(
                "summary.json",
                json.dumps(dict(summary), ensure_ascii=False, indent=2, default=str),
            )
        if include_audio:
            original = next((f for f in files if f["kind"] == "audio_opus"), None)
            if original is None:
                original = next((f for f in files if f["kind"] == "original_audio_48k"), None)
            if original is not None:
                lo_file = _LargeObjectFile(
                    conn, int(original["lo_oid"]), int(original["size_bytes"])
                )
                name = original["original_filename"]
                if not name:
                    name = "audio.ogg" if original["kind"] == "audio_opus" else "audio.wav"
                with archive.open(f"audio/{name}", "w") as destination:
                    shutil.copyfileobj(lo_file, destination, length=1024 * 1024)
        for attachment in (f for f in files if f["kind"] == "attachment"):
            lo_file = _LargeObjectFile(
                conn, int(attachment["lo_oid"]), int(attachment["size_bytes"])
            )
            with archive.open(
                f"attachments/{attachment['original_filename']}", "w"
            ) as destination:
                shutil.copyfileobj(lo_file, destination, length=1024 * 1024)
    yield out.getvalue()
