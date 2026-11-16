"""Recording ingest: store the original WAV and its 16 kHz mono derivative.

The original 48 kHz WAV (as captured) is stored in a Large Object untouched;
a 16 kHz mono WAV is derived for transcription (the shape Sarvam/Whisper
expect) and stored in a second Large Object.  Both are SHA-256 hashed.
"""

from __future__ import annotations

import audioop  # stdlib resampler (Python 3.11; removed only in 3.13+)
import logging
import wave
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from db import lo

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16_000


class IngestError(RuntimeError):
    """Raised when a recording cannot be ingested."""


def derive_16k_mono(source: Path, target: Path) -> dict[str, Any]:
    """Convert a 16-bit PCM WAV to 16 kHz mono; return audio metadata.

    Uses the standard-library ``wave``/``audioop`` resampler so no extra
    dependency is needed on the server.
    """

    with wave.open(str(source), "rb") as reader:
        channels = reader.getnchannels()
        sample_rate = reader.getframerate()
        sample_width = reader.getsampwidth()
        frames = reader.getnframes()
        raw = reader.readframes(frames)

    if sample_width != 2:
        raise IngestError("Only 16-bit PCM WAV recordings are supported.")
    if channels not in (1, 2):
        raise IngestError(
            f"Unexpected channel count {channels}; expected mono or stereo."
        )

    if channels == 2:
        raw = audioop.tomono(raw, 2, 0.5, 0.5)
    if sample_rate != TARGET_SAMPLE_RATE:
        raw, _ = audioop.ratecv(raw, 2, 1, sample_rate, TARGET_SAMPLE_RATE, None)

    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(TARGET_SAMPLE_RATE)
        writer.writeframes(raw)

    return {
        "sample_rate": TARGET_SAMPLE_RATE,
        "channels": 1,
        "duration_ms": round(frames * 1000 / sample_rate) if sample_rate else 0,
    }


def ingest_recording(
    conn: Connection,
    *,
    user_id: str,
    title: str,
    original_path: Path,
    duration_ms: int,
    sample_rate: int,
    channels: int,
    mime_type: str = "audio/wav",
    original_filename: str | None = None,
) -> str:
    """Create the meeting row and store both audio tracks as Large Objects.

    Everything happens in the caller's transaction: the meeting row, both
    ``meeting_files`` rows and both Large Objects commit (or roll back)
    together, so a crash cannot leave a meeting without audio or an orphan LO.
    """

    original_path = Path(original_path)
    if not original_path.is_file():
        raise IngestError("The recording file is missing.")
    if original_path.stat().st_size <= 44:
        raise IngestError("The recording file is empty.")
    display_name = original_filename or original_path.name

    meeting_id = conn.execute(
        text(
            "INSERT INTO meetings (user_id, title, status, duration_ms) VALUES "
            "(:user_id, :title, 'recorded', :duration_ms) RETURNING id"
        ),
        {"user_id": user_id, "title": title, "duration_ms": duration_ms},
    ).scalar_one()

    original_oid, original_sha, original_size = lo.create_lo_from_file(
        conn, original_path
    )
    conn.execute(
        text(
            "INSERT INTO meeting_files (id, meeting_id, kind, lo_oid, sha256, "
            "size_bytes, mime_type, duration_ms, sample_rate, channels, "
            "original_filename) VALUES (gen_random_uuid(), :meeting_id, "
            "'original_audio_48k', :oid, :sha, :size, :mime, :duration_ms, "
            ":sample_rate, :channels, :filename)"
        ),
        {
            "meeting_id": meeting_id,
            "oid": original_oid,
            "sha": original_sha,
            "size": original_size,
            "mime": mime_type,
            "duration_ms": duration_ms,
            "sample_rate": sample_rate,
            "channels": channels,
            "filename": display_name,
        },
    )

    derived_path = original_path.with_name(original_path.stem + "-16k-mono.wav")
    derived_meta = derive_16k_mono(original_path, derived_path)
    derived_oid, derived_sha, derived_size = lo.create_lo_from_file(conn, derived_path)
    conn.execute(
        text(
            "INSERT INTO meeting_files (id, meeting_id, kind, lo_oid, sha256, "
            "size_bytes, mime_type, duration_ms, sample_rate, channels, "
            "original_filename) VALUES (gen_random_uuid(), :meeting_id, "
            "'derived_audio_16k', :oid, :sha, :size, 'audio/wav', :duration_ms, "
            "16000, 1, :filename)"
        ),
        {
            "meeting_id": meeting_id,
            "oid": derived_oid,
            "sha": derived_sha,
            "size": derived_size,
            "duration_ms": derived_meta["duration_ms"],
            "filename": derived_path.name,
        },
    )
    # The derivative is already persisted; remove the temporary local file.
    derived_path.unlink(missing_ok=True)

    conn.execute(
        text(
            "INSERT INTO processing_jobs (id, meeting_id, job_type, status, message) "
            "VALUES (gen_random_uuid(), :meeting_id, 'transcription', 'queued', "
            "'Queued for transcription')"
        ),
        {"meeting_id": meeting_id},
    )
    conn.commit()
    logger.info("Ingested meeting %s (%d Hz, %d ch).", meeting_id, sample_rate, channels)
    return str(meeting_id)
