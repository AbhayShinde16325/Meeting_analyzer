"""Migration from the legacy SQLite storage into the file library."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from storage import library as ops
from storage.library import MeetingLibrary
from storage.migration import migrate_legacy_sqlite
from support import write_pcm_wav

_LEGACY_SCHEMA = """
CREATE TABLE users (
    id INTEGER NOT NULL PRIMARY KEY,
    display_name VARCHAR(120) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL
);
CREATE TABLE meetings (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title VARCHAR(300) NOT NULL,
    status VARCHAR(32) NOT NULL,
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    error_message TEXT
);
CREATE TABLE recordings (
    id INTEGER NOT NULL PRIMARY KEY,
    meeting_id VARCHAR(36) NOT NULL UNIQUE,
    relative_path VARCHAR(500) NOT NULL,
    format VARCHAR(16) NOT NULL,
    duration_seconds FLOAT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE transcripts (
    id INTEGER NOT NULL PRIMARY KEY,
    meeting_id VARCHAR(36) NOT NULL UNIQUE,
    text TEXT NOT NULL,
    segments_json TEXT NOT NULL,
    language VARCHAR(16),
    created_at DATETIME NOT NULL
);
CREATE TABLE summaries (
    id INTEGER NOT NULL PRIMARY KEY,
    meeting_id VARCHAR(36) NOT NULL UNIQUE,
    overview TEXT NOT NULL,
    key_points_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    open_questions_json TEXT NOT NULL,
    risks_or_blockers_json TEXT NOT NULL,
    follow_ups_json TEXT NOT NULL,
    model VARCHAR(120),
    created_at DATETIME NOT NULL
);
CREATE TABLE action_items (
    id INTEGER NOT NULL PRIMARY KEY,
    meeting_id VARCHAR(36) NOT NULL,
    task TEXT NOT NULL,
    owner VARCHAR(120),
    deadline VARCHAR(120),
    evidence TEXT,
    status VARCHAR(24) NOT NULL,
    created_at DATETIME NOT NULL
);
"""

_SEGMENTS = json.dumps(
    [{"start": 0.0, "end": 1.0, "text": "Legacy transcript text."}]
)


def _make_legacy_db(settings, wav_factory) -> str:
    """Create a legacy teammeetingai.db + recordings dir; return meeting id."""

    data_dir = settings.data_dir
    meeting_id = "11111111-2222-3333-4444-555555555555"

    recordings_dir = data_dir / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    audio_path = recordings_dir / f"{meeting_id}.wav"
    write_pcm_wav(audio_path, seconds=0.05)

    connection = sqlite3.connect(str(data_dir / "teammeetingai.db"))
    try:
        connection.executescript(_LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO users (id, display_name, created_at)"
            " VALUES (1, 'Legacy User', '2026-01-01 10:00:00')"
        )
        connection.execute(
            "INSERT INTO meetings (id, user_id, title, status, started_at,"
            " ended_at, created_at, updated_at, error_message)"
            " VALUES (?, 1, 'Legacy meeting', 'completed',"
            " '2026-01-01 10:00:00.000000', '2026-01-01 10:30:00.000000',"
            " '2026-01-01 10:00:00.000000', '2026-01-01 10:30:00.000000', NULL)",
            (meeting_id,),
        )
        connection.execute(
            "INSERT INTO recordings (meeting_id, relative_path, format,"
            " duration_seconds, size_bytes, sample_rate, channels, created_at)"
            " VALUES (?, ?, 'wav', 0.05, ?, 48000, 2, '2026-01-01 10:30:00')",
            (meeting_id, audio_path.name, audio_path.stat().st_size),
        )
        connection.execute(
            "INSERT INTO transcripts (meeting_id, text, segments_json,"
            " language, created_at) VALUES (?, 'Legacy transcript text.', ?,"
            " 'en', '2026-01-01 10:31:00')",
            (meeting_id, _SEGMENTS),
        )
        connection.execute(
            "INSERT INTO summaries (meeting_id, overview, key_points_json,"
            " decisions_json, open_questions_json, risks_or_blockers_json,"
            " follow_ups_json, model, created_at)"
            " VALUES (?, 'Legacy overview', '[]', '[]', '[]', '[]', '[]',"
            " 'legacy-model', '2026-01-01 10:32:00')",
            (meeting_id,),
        )
        connection.execute(
            "INSERT INTO action_items (meeting_id, task, owner, deadline,"
            " evidence, status, created_at)"
            " VALUES (?, 'Legacy task', 'Owner', 'Friday', 'Evidence text',"
            " 'open', '2026-01-01 10:32:00')",
            (meeting_id,),
        )
        connection.commit()
    finally:
        connection.close()
    return meeting_id


def test_migration_converts_legacy_meeting(settings, wav_factory):
    meeting_id = _make_legacy_db(settings, wav_factory)
    library = MeetingLibrary(settings)

    result = migrate_legacy_sqlite(settings, library)

    assert result.migrated == 1
    assert result.skipped == 0
    assert result.errors == []
    assert result.database_renamed

    # The database file is archived, not deleted.
    assert not (settings.data_dir / "teammeetingai.db").exists()
    archives = list(settings.data_dir.glob("teammeetingai.db.migrated-*"))
    assert len(archives) == 1

    # The legacy user became a library profile.
    users = ops.list_users(library)
    assert len(users) == 1
    assert users[0]["id"] == 1
    assert users[0]["display_name"] == "Legacy User"

    details = ops.get_meeting_details(library, meeting_id)
    assert details["title"] == "Legacy meeting"
    assert details["status"] == "completed"
    assert details["user_id"] == 1

    meeting_dir = Path(details["meeting_dir"])
    audio_name = details["recording"]["audio"]
    assert audio_name.endswith(".wav")
    assert (meeting_dir / audio_name).is_file()
    assert details["recording"]["duration_seconds"] == 0.05

    assert details["transcript"]["text"] == "Legacy transcript text."
    assert details["summary"]["overview"] == "Legacy overview"
    assert details["summary"]["model"] == "legacy-model"
    assert details["action_items"][0]["task"] == "Legacy task"
    assert details["action_items"][0]["evidence"] == "Evidence text"

    # The markdown documents were generated during migration.
    assert "Legacy transcript text." in (
        meeting_dir / "transcript.md"
    ).read_text(encoding="utf-8")
    assert "Legacy overview" in (meeting_dir / "analysis.md").read_text(
        encoding="utf-8"
    )


def test_migration_without_legacy_db_is_a_noop(settings):
    library = MeetingLibrary(settings)

    result = migrate_legacy_sqlite(settings, library)

    assert result.migrated == 0
    assert result.errors == []
    assert result.database_renamed is False


def test_migration_is_idempotent(settings, wav_factory):
    _make_legacy_db(settings, wav_factory)
    library = MeetingLibrary(settings)

    first = migrate_legacy_sqlite(settings, library)
    assert first.migrated == 1

    # The database was renamed aside, so a second run has nothing to do.
    second = migrate_legacy_sqlite(settings, library)
    assert second.migrated == 0
    assert second.database_renamed is False
    assert len(ops.list_meetings(library)) == 1


def test_migration_failure_keeps_database_for_retry(
    settings, wav_factory, monkeypatch
):
    _make_legacy_db(settings, wav_factory)
    library = MeetingLibrary(settings)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("storage.migration._migrate_meeting", _boom)
    result = migrate_legacy_sqlite(settings, library)

    assert result.migrated == 0
    assert result.errors
    # The database stays in place so the next start can retry.
    assert (settings.data_dir / "teammeetingai.db").exists()
