"""Large Object (LO) storage helpers.

psycopg 3 does not ship an ``lobject()`` wrapper, so every operation goes
through the server-side SQL functions ``lo_create``, ``lo_put``, ``lo_get`` and
``lo_unlink``.  All of these must run inside a transaction; callers pass an
open SQLAlchemy :class:`~sqlalchemy.Connection`.

Every binary stored in the database goes through this module so that SHA-256
hashing and chunked streaming are consistent across ingest, playback and
export.

Deletion normally happens through the ``meeting_files`` BEFORE DELETE trigger
(which unlinks the LO in the same transaction); :func:`unlink_lo` exists for
orphan cleanup.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import BinaryIO, Iterator

from sqlalchemy import Connection, text

logger = logging.getLogger(__name__)

# 4 MiB read/write chunks: small enough to keep memory flat for multi-GB
# recordings, large enough to avoid per-block round-trips.
CHUNK_SIZE = 4 * 1024 * 1024


class LargeObjectError(RuntimeError):
    """Raised when a Large Object operation cannot be completed."""


def create_lo(conn: Connection) -> int:
    """Create an empty Large Object and return its OID."""

    row = conn.execute(text("SELECT lo_create(0) AS oid")).mappings().one()
    return int(row["oid"])


def write_lo(conn: Connection, oid: int, offset: int, data: bytes) -> None:
    """Write ``data`` into the LO at ``offset`` using ``lo_put``."""

    conn.execute(
        text("SELECT lo_put(:oid, :offset, :data)"),
        {"oid": oid, "offset": offset, "data": data},
    )


def create_lo_from_bytes(conn: Connection, data: bytes) -> tuple[int, str, int]:
    """Store ``data`` in a new LO.  Returns ``(oid, sha256_hex, size)``."""

    oid = create_lo(conn)
    digest = hashlib.sha256()
    offset = 0
    for start in range(0, len(data), CHUNK_SIZE):
        chunk = data[start : start + CHUNK_SIZE]
        digest.update(chunk)
        write_lo(conn, oid, offset, chunk)
        offset += len(chunk)
    return oid, digest.hexdigest(), len(data)


def create_lo_from_file(conn: Connection, path: Path) -> tuple[int, str, int]:
    """Stream a file into a new LO.  Returns ``(oid, sha256_hex, size)``.

    The file is read in chunks so even multi-gigabyte recordings never sit
    fully in memory.
    """

    oid = create_lo(conn)
    digest = hashlib.sha256()
    offset = 0
    size = 0
    with open(path, "rb") as source:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            write_lo(conn, oid, offset, chunk)
            offset += len(chunk)
            size += len(chunk)
    return oid, digest.hexdigest(), size


def read_lo(conn: Connection, oid: int) -> bytes:
    """Read an entire LO into memory (intended for small objects/tests)."""

    return b"".join(iter_lo_chunks(conn, oid))


def iter_lo_chunks(
    conn: Connection, oid: int, *, offset: int = 0, length: int | None = None
) -> Iterator[bytes]:
    """Yield LO content as chunks, optionally a bounded range.

    Used by streaming endpoints; supports HTTP Range requests by passing an
    ``offset`` and ``length``.
    """

    remaining = length
    position = offset
    while remaining is None or remaining > 0:
        fetch = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
        row = (
            conn.execute(
                text("SELECT lo_get(:oid, :offset, :len) AS data"),
                {"oid": oid, "offset": position, "len": fetch},
            )
            .mappings()
            .one()
        )
        chunk = row["data"]
        if not chunk:
            return
        position += len(chunk)
        if remaining is not None:
            remaining -= len(chunk)
        yield bytes(chunk)


def read_lo_range(conn: Connection, oid: int, offset: int, length: int) -> bytes:
    """Read exactly one bounded slice of an LO (for HTTP Range responses)."""

    if length <= 0:
        return b""
    row = (
        conn.execute(
            text("SELECT lo_get(:oid, :offset, :len) AS data"),
            {"oid": oid, "offset": offset, "len": length},
        )
        .mappings()
        .one()
    )
    return bytes(row["data"] or b"")


def verify_lo(conn: Connection, oid: int, expected_sha256: str) -> bool:
    """Stream an LO and compare its SHA-256 with the stored metadata."""

    digest = hashlib.sha256()
    for chunk in iter_lo_chunks(conn, oid):
        digest.update(chunk)
    return digest.hexdigest() == expected_sha256


def lo_size(conn: Connection, oid: int) -> int:
    """Return the size of an LO in bytes.

    ``pg_largeobject`` holds the content in chunks; summing chunk lengths gives
    the exact size.  Metadata rows normally carry the size, so this is used for
    verification and orphan accounting only.
    """

    row = (
        conn.execute(
            text(
                "SELECT COALESCE(SUM(LENGTH(data)), 0) AS size "
                "FROM pg_largeobject WHERE loid = :oid"
            ),
            {"oid": oid},
        )
        .mappings()
        .one()
    )
    return int(row["size"])


def unlink_lo(conn: Connection, oid: int) -> None:
    """Unlink (free) one Large Object."""

    conn.execute(text("SELECT lo_unlink(:oid)"), {"oid": oid})


def sweep_orphan_los(conn: Connection) -> int:
    """Unlink LOs that no longer have a ``meeting_files`` row.

    Large Objects are a known consistency hazard: anything that unlinks rows
    outside the guarded transaction leaks storage.  This sweep is safe to run
    periodically (documented in ``docs/OPERATIONS.md``) and returns how many
    orphans were removed.
    """

    orphans = (
        conn.execute(
            text(
                "SELECT l.oid FROM pg_largeobject_metadata l "
                "WHERE NOT EXISTS (SELECT 1 FROM meeting_analyzer.meeting_files f WHERE f.lo_oid = l.oid)"
            )
        )
        .scalars()
        .all()
    )
    for oid in orphans:
        unlink_lo(conn, int(oid))
    if orphans:
        logger.warning("Unlinked %d orphan Large Object(s).", len(orphans))
    return len(orphans)


def copy_lo_to_stream(conn: Connection, oid: int, target: BinaryIO) -> int:
    """Stream an LO into an open binary file handle; returns bytes written."""

    written = 0
    for chunk in iter_lo_chunks(conn, oid):
        target.write(chunk)
        written += len(chunk)
    return written
