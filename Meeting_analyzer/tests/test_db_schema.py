"""Relational schema tests: constraints, cascades, primary transcript
uniqueness and attachment rules.

These run against real PostgreSQL (see tests/pg_harness.py) because partial
unique indexes, triggers and Large Objects are PostgreSQL-specific.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db import lo


@pytest.fixture
def user_id(pg):
    return pg.execute(
        text(
            "INSERT INTO meeting_analyzer.users (id, display_name) VALUES (gen_random_uuid(), 'Test User') "
            "RETURNING id"
        )
    ).scalar_one()


@pytest.fixture
def meeting_id(pg, user_id):
    return pg.execute(
        text(
            "INSERT INTO meeting_analyzer.meetings (id, user_id, title) VALUES (gen_random_uuid(), "
            ":user_id, 'Test meeting') RETURNING id"
        ),
        {"user_id": user_id},
    ).scalar_one()


@pytest.fixture
def audio_file_id(pg, meeting_id):
    """A meeting with its original 48 kHz audio stored as a Large Object."""

    payload = b"fake 48k audio"
    oid, sha, size = lo.create_lo_from_bytes(pg, payload)
    return pg.execute(
        text(
            "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
            "size_bytes, mime_type, sample_rate, channels) VALUES (gen_random_uuid(), "
            ":meeting_id, 'original_audio_48k', :oid, :sha, :size, 'audio/wav', 48000, 2) "
            "RETURNING id"
        ),
        {"meeting_id": meeting_id, "oid": oid, "sha": sha, "size": size},
    ).scalar_one()


class TestMeetingLifecycle:
    def test_meeting_defaults(self, pg, user_id):
        row = (
            pg.execute(
                text(
                    "INSERT INTO meeting_analyzer.meetings (user_id, title) VALUES (:u, 'T') "
                    "RETURNING status, recorded_at"
                ),
                {"u": user_id},
            )
            .mappings()
            .one()
        )
        assert row["status"] == "recorded"
        assert row["recorded_at"] is not None

    def test_status_check_rejects_unknown(self, pg, user_id):
        with pytest.raises(Exception):
            pg.execute(
                text("INSERT INTO meeting_analyzer.meetings (user_id, title, status) VALUES (:u, 'T', 'nope')"),
                {"u": user_id},
            )
            pg.commit()


class TestFileConstraints:
    def test_one_original_audio_per_meeting(self, pg, meeting_id):
        first_oid, first_sha, first_size = lo.create_lo_from_bytes(pg, b"first track")
        pg.execute(
            text(
                "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                "size_bytes) VALUES (gen_random_uuid(), :m, 'original_audio_48k', "
                ":oid, :sha, :size)"
            ),
            {"m": meeting_id, "oid": first_oid, "sha": first_sha, "size": first_size},
        )
        pg.commit()
        oid, sha, size = lo.create_lo_from_bytes(pg, b"second track")
        with pytest.raises(Exception):
            pg.execute(
                text(
                    "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                    "size_bytes) VALUES (gen_random_uuid(), :m, 'original_audio_48k', "
                    ":oid, :sha, :size)"
                ),
                {"m": meeting_id, "oid": oid, "sha": sha, "size": size},
            )
            pg.commit()

    def test_kind_check_rejects_unknown(self, pg, meeting_id):
        oid, sha, size = lo.create_lo_from_bytes(pg, b"x")
        with pytest.raises(Exception):
            pg.execute(
                text(
                    "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                    "size_bytes) VALUES (gen_random_uuid(), :m, 'blob', :oid, :sha, :size)"
                ),
                {"m": meeting_id, "oid": oid, "sha": sha, "size": size},
            )
            pg.commit()

    def test_lo_oid_is_unique_across_rows(self, pg, meeting_id):
        oid, sha, size = lo.create_lo_from_bytes(pg, b"shared bytes")
        pg.execute(
            text(
                "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                "size_bytes, mime_type, original_filename) VALUES (gen_random_uuid(), "
                ":m, 'attachment', :oid, :sha, :size, 'text/plain', 'a.txt')"
            ),
            {"m": meeting_id, "oid": oid, "sha": sha, "size": size},
        )
        with pytest.raises(Exception):
            # A second meeting cannot claim the same LO.
            other = pg.execute(
                text(
                    "INSERT INTO meeting_analyzer.meetings (user_id, title) SELECT user_id, 'other' "
                    "FROM meeting_analyzer.meetings WHERE id = :m RETURNING id"
                ),
                {"m": meeting_id},
            ).scalar_one()
            pg.execute(
                text(
                    "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                    "size_bytes, mime_type, original_filename) VALUES (gen_random_uuid(), "
                    ":m, 'attachment', :oid, :sha, :size, 'text/plain', 'a.txt')"
                ),
                {"m": other, "oid": oid, "sha": sha, "size": size},
            )
            pg.commit()


class TestAttachmentConstraints:
    def _insert_attachment(self, pg, meeting_id, *, size, mime, filename):
        oid, sha, _ = lo.create_lo_from_bytes(pg, b"a" * min(size, 1024))
        pg.execute(
            text(
                "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                "size_bytes, mime_type, original_filename) VALUES (gen_random_uuid(), "
                ":m, 'attachment', :oid, :sha, :size, :mime, :name)"
            ),
            {"m": meeting_id, "oid": oid, "sha": sha, "size": size, "mime": mime,
             "name": filename},
        )
        pg.commit()

    def test_valid_attachment_accepted(self, pg, meeting_id):
        self._insert_attachment(
            pg, meeting_id, size=1024, mime="application/pdf", filename="notes.pdf"
        )

    def test_oversized_attachment_rejected(self, pg, meeting_id):
        with pytest.raises(Exception):
            self._insert_attachment(
                pg, meeting_id, size=50 * 1024 * 1024 + 1,
                mime="application/pdf", filename="big.pdf",
            )

    def test_disallowed_extension_rejected(self, pg, meeting_id):
        with pytest.raises(Exception):
            self._insert_attachment(
                pg, meeting_id, size=10, mime="application/x-msdownload",
                filename="evil.exe",
            )

    def test_disallowed_mime_rejected(self, pg, meeting_id):
        with pytest.raises(Exception):
            self._insert_attachment(
                pg, meeting_id, size=10, mime="application/zip", filename="a.pdf",
            )

    def test_extension_case_insensitive(self, pg, meeting_id):
        self._insert_attachment(
            pg, meeting_id, size=10, mime="image/png", filename="PHOTO.PNG"
        )

    def test_audio_rows_are_exempt_from_attachment_rules(self, pg, meeting_id):
        oid, sha, size = lo.create_lo_from_bytes(pg, b"audio")
        pg.execute(
            text(
                "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                "size_bytes, mime_type, sample_rate) VALUES (gen_random_uuid(), :m, "
                "'derived_audio_16k', :oid, :sha, :size, 'audio/wav', 16000)"
            ),
            {"m": meeting_id, "oid": oid, "sha": sha, "size": size},
        )
        pg.commit()


class TestDeletion:
    def test_hard_delete_removes_rows_and_large_objects(self, pg, meeting_id, audio_file_id):
        oid = pg.execute(
            text("SELECT lo_oid FROM meeting_analyzer.meeting_files WHERE id = :id"), {"id": audio_file_id}
        ).scalar_one()
        # Add a transcript so cascade covers multiple tables.
        pg.execute(
            text(
                "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, is_primary, "
                "text) VALUES (gen_random_uuid(), :m, 'whisper', 'small', true, 'text')"
            ),
            {"m": meeting_id},
        )
        pg.execute(
            text(
                "INSERT INTO meeting_analyzer.summaries (id, meeting_id, payload, model) VALUES "
                "(gen_random_uuid(), :m, '{\"overview\": \"x\"}'::jsonb, 'm')"
            ),
            {"m": meeting_id},
        )

        before = int(
            pg.execute(text("SELECT COUNT(*) FROM pg_largeobject_metadata")).scalar_one()
        )
        pg.execute(text("DELETE FROM meeting_analyzer.meetings WHERE id = :m"), {"m": meeting_id})
        pg.commit()

        assert pg.execute(text("SELECT COUNT(*) FROM meeting_analyzer.meeting_files")).scalar_one() == 0
        assert pg.execute(text("SELECT COUNT(*) FROM meeting_analyzer.transcripts")).scalar_one() == 0
        assert pg.execute(text("SELECT COUNT(*) FROM meeting_analyzer.summaries")).scalar_one() == 0
        after = int(
            pg.execute(text("SELECT COUNT(*) FROM pg_largeobject_metadata")).scalar_one()
        )
        # The trigger unlinked the LO in the same transaction.
        assert after == before - 1

    def test_deleted_lo_bytes_are_unreadable(self, pg, meeting_id, audio_file_id):
        oid = pg.execute(
            text("SELECT lo_oid FROM meeting_analyzer.meeting_files WHERE id = :id"), {"id": audio_file_id}
        ).scalar_one()
        pg.execute(text("DELETE FROM meeting_analyzer.meetings WHERE id = :m"), {"m": meeting_id})
        pg.commit()
        # lo_get on an unlinked OID returns nothing / errors; assert gone.
        count = pg.execute(
            text("SELECT COUNT(*) FROM pg_largeobject_metadata WHERE oid = :oid"),
            {"oid": oid},
        ).scalar_one()
        assert count == 0


class TestPrimaryTranscript:
    def test_only_one_primary_per_meeting(self, pg, meeting_id):
        def add(primary: bool) -> None:
            pg.execute(
                text(
                    "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, "
                    "is_primary, text) VALUES (gen_random_uuid(), :m, 'sarvam', "
                    "'saaras:v4', :p, 'text')"
                ),
                {"m": meeting_id, "p": primary},
            )

        add(True)
        with pytest.raises(Exception):
            add(True)
            pg.commit()

    def test_non_primary_versions_allowed(self, pg, meeting_id):
        for _ in range(3):
            pg.execute(
                text(
                    "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, "
                    "is_primary, text) VALUES (gen_random_uuid(), :m, 'whisper', "
                    "'small', false, 'text')"
                ),
                {"m": meeting_id},
            )
        pg.commit()

    def test_primary_flag_change_works(self, pg, meeting_id):
        first = pg.execute(
            text(
                "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, is_primary, "
                "text) VALUES (gen_random_uuid(), :m, 'sarvam', 'saaras:v4', true, 'a') "
                "RETURNING id"
            ),
            {"m": meeting_id},
        ).scalar_one()
        second = pg.execute(
            text(
                "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, is_primary, "
                "text) VALUES (gen_random_uuid(), :m, 'whisper', 'small', false, 'b') "
                "RETURNING id"
            ),
            {"m": meeting_id},
        ).scalar_one()
        # Demote and promote in one statement set (as the API will).
        pg.execute(text("UPDATE meeting_analyzer.transcripts SET is_primary = false WHERE id = :id"),
                   {"id": first})
        pg.execute(text("UPDATE meeting_analyzer.transcripts SET is_primary = true WHERE id = :id"),
                   {"id": second})
        pg.commit()
        primary = pg.execute(
            text("SELECT id FROM meeting_analyzer.transcripts WHERE is_primary AND meeting_id = :m"),
            {"m": meeting_id},
        ).scalar_one()
        assert primary == second


class TestUniqueIds:
    def test_ids_are_uuid4(self, pg, user_id, meeting_id):
        assert uuid.UUID(str(meeting_id)).version == 4
