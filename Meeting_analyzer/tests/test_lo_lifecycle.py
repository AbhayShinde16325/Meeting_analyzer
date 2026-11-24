"""Large Object lifecycle tests: create, read, verify, unlink, orphans."""

from __future__ import annotations

import hashlib

from sqlalchemy import text

from db import lo


def _store(conn, payload: bytes) -> tuple[int, str]:
    oid, sha, size = lo.create_lo_from_bytes(conn, payload)
    assert size == len(payload)
    return oid, sha


class TestCreateAndRead:
    def test_roundtrip_small_payload(self, pg):
        payload = b"hello large object"
        oid, sha = _store(pg, payload)
        assert lo.read_lo(pg, oid) == payload
        assert lo.lo_size(pg, oid) == len(payload)
        assert sha == hashlib.sha256(payload).hexdigest()

    def test_roundtrip_multi_chunk_payload(self, pg):
        # Force several 4 MiB chunks to exercise offset handling.
        payload = bytes(range(256)) * (lo.CHUNK_SIZE // 32) * 3
        assert len(payload) > 2 * lo.CHUNK_SIZE
        oid, sha = _store(pg, payload)
        assert lo.read_lo(pg, oid) == payload

    def test_read_range(self, pg):
        payload = b"A" * 1000 + b"B" * 500
        oid, _sha = _store(pg, payload)
        assert lo.read_lo_range(pg, oid, 0, 1000) == b"A" * 1000
        assert lo.read_lo_range(pg, oid, 1000, 500) == b"B" * 500
        assert lo.read_lo_range(pg, oid, 990, 20) == b"A" * 10 + b"B" * 10
        assert lo.read_lo_range(pg, oid, 0, 0) == b""


class TestVerification:
    def test_verify_matches_correct_hash(self, pg):
        payload = b"verify me"
        oid, sha = _store(pg, payload)
        assert lo.verify_lo(pg, oid, sha) is True

    def test_verify_rejects_wrong_hash(self, pg):
        oid, _sha = _store(pg, b"actual content")
        assert lo.verify_lo(pg, oid, "0" * 64) is False

    def test_file_ingest_matches_byte_ingest(self, pg, tmp_path):
        payload = b"file content" * 1000
        path = tmp_path / "audio.wav"
        path.write_bytes(payload)
        oid, sha, size = lo.create_lo_from_file(pg, path)
        assert size == len(payload)
        assert sha == hashlib.sha256(payload).hexdigest()
        assert lo.read_lo(pg, oid) == payload


class TestDeletionAndOrphans:
    def test_unlink_frees_the_object(self, pg):
        oid, _sha = _store(pg, b"to be deleted")
        assert lo.lo_size(pg, oid) == 13
        lo.unlink_lo(pg, oid)
        # The LO disappears from the metadata catalog.
        count = pg.execute(
            text("SELECT COUNT(*) FROM pg_largeobject_metadata WHERE oid = :oid"),
            {"oid": oid},
        ).scalar_one()
        assert count == 0

    def test_orphan_sweep_removes_unreferenced_los(self, pg):
        # A meeting file that references its LO...
        kept_oid, _sha = _store(pg, b"referenced")
        user = pg.execute(
            text(
                "INSERT INTO meeting_analyzer.users (id, display_name) VALUES (gen_random_uuid(), 'U') "
                "RETURNING id"
            )
        ).mappings().one()
        meeting = pg.execute(
            text(
                "INSERT INTO meeting_analyzer.meetings (id, user_id, title) VALUES (gen_random_uuid(), "
                ":user_id, 'M') RETURNING id"
            ),
            {"user_id": user["id"]},
        ).mappings().one()
        pg.execute(
            text(
                "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                "size_bytes, mime_type, original_filename) VALUES (gen_random_uuid(), "
                ":meeting_id, 'attachment', :oid, :sha, 10, 'text/plain', 'a.txt')"
            ),
            {"meeting_id": meeting["id"], "oid": kept_oid, "sha": _sha},
        )

        orphan_oid, _sha2 = _store(pg, b"orphaned content")

        removed = lo.sweep_orphan_los(pg)
        assert removed == 1
        # The referenced LO survived; the orphan is gone.
        assert lo.lo_size(pg, kept_oid) == 10
        assert lo.lo_size(pg, orphan_oid) == 0
