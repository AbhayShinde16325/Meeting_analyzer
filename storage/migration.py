"""One-time migration from the legacy SQLite database to the file library.

Older builds stored meetings in ``<data_dir>\\teammeetingai.db`` and audio in
``<data_dir>\\recordings\\``.  This module reads that legacy data with the
standard-library ``sqlite3`` module (no SQLAlchemy required), converts every
meeting into a library folder (audio files + ``meeting.json`` + markdown
documents), and then renames the database file aside as a backup.

The migration is best-effort per meeting: a meeting that cannot be converted
is logged and skipped.  The database file is only renamed once every meeting
was converted, so an interrupted migration simply runs again on next start.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import Settings
from storage.library import MeetingLibrary, utc_now_iso

logger = logging.getLogger(__name__)

_LEGACY_DB_NAME = "teammeetingai.db"
AUDIO_TARGET = "audio.wav"
MIC_TARGET = "mic.wav"
SYSTEM_TARGET = "system.wav"


@dataclass
class MigrationResult:
    migrated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    database_renamed: bool = False


def migrate_legacy_sqlite(settings: Settings, library: MeetingLibrary) -> MigrationResult:
    """Convert a legacy ``teammeetingai.db`` into the meeting library."""

    result = MigrationResult()
    db_path = settings.data_dir / _LEGACY_DB_NAME
    if not db_path.is_file():
        return result

    logger.info("Legacy database found; migrating meetings to the library.")
    try:
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.error("Could not open the legacy database: %s", exc)
        result.errors.append(f"Could not open the legacy database: {exc}")
        return result

    try:
        _migrate_all(connection, settings, library, result)
    finally:
        connection.close()

    if not result.errors:
        result.database_renamed = _rename_database_aside(db_path)
    else:
        logger.warning(
            "Legacy database kept in place because %s meeting(s) failed to "
            "migrate; migration will retry on next start.",
            len(result.errors),
        )
    logger.info(
        "Legacy migration finished: %s migrated, %s skipped.",
        result.migrated,
        result.skipped,
    )
    return result


def _migrate_all(
    connection: sqlite3.Connection,
    settings: Settings,
    library: MeetingLibrary,
    result: MigrationResult,
) -> None:
    recordings_root = settings.recordings_dir

    # Profiles move into the library's profiles.json so the login screen
    # keeps showing the same local profiles after the migration.
    _import_users(connection, library)

    users = {
        int(row["id"]): str(row["display_name"])
        for row in connection.execute("SELECT id, display_name FROM users")
    }

    for row in connection.execute("SELECT * FROM meetings"):
        meeting_id = str(row["id"])
        if meeting_id in library._index:  # noqa: SLF001 - same-package access
            result.skipped += 1
            continue
        try:
            _migrate_meeting(connection, row, users, recordings_root, library)
            result.migrated += 1
        except Exception as exc:  # noqa: BLE001 - per-meeting isolation
            logger.warning("Could not migrate meeting %s: %s", meeting_id, exc)
            result.errors.append(f"{meeting_id}: {exc}")


def _import_users(connection: sqlite3.Connection, library: MeetingLibrary) -> None:
    """Copy legacy users into the library's ``profiles.json`` (ids preserved)."""

    profiles = library._load_profiles()  # noqa: SLF001 - same-package access
    existing_ids = {
        int(profile["id"])
        for profile in profiles["profiles"]
        if isinstance(profile, dict) and "id" in profile
    }
    for row in connection.execute(
        "SELECT id, display_name, created_at FROM users ORDER BY id"
    ):
        user_id = int(row["id"])
        if user_id in existing_ids:
            continue
        profiles["profiles"].append(
            {
                "id": user_id,
                "display_name": str(row["display_name"]),
                "created_at": _iso(row["created_at"]),
            }
        )
        next_id = int(profiles["next_id"])
        profiles["next_id"] = max(next_id, user_id + 1)
    library._save_profiles(profiles)  # noqa: SLF001 - same-package access


def _migrate_meeting(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    users: dict[int, str],
    recordings_root: Path,
    library: MeetingLibrary,
) -> None:
    meeting_id = str(row["id"])

    folder_name = library._unique_folder_name(
        str(row["started_at"]), str(row["title"]), meeting_id
    )
    folder = library.library_dir / folder_name
    folder.mkdir(parents=True, exist_ok=False)

    # -- audio files ----------------------------------------------------
    audio_name = mic_name = system_name = None
    recording_row = connection.execute(
        "SELECT * FROM recordings WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    relative_path = str(recording_row["relative_path"]) if recording_row else None
    duration = float(recording_row["duration_seconds"]) if recording_row else 0.0

    if relative_path:
        source = recordings_root / relative_path
        if source.is_file():
            audio_name = AUDIO_TARGET if source.name == AUDIO_TARGET else source.name
            shutil.copy2(source, folder / audio_name)
        else:
            logger.warning(
                "Legacy recording file %s is missing for meeting %s.",
                relative_path,
                meeting_id,
            )
    # Source tracks are not stored in the legacy database; find them by the
    # meeting id prefix next to the mixed file.
    for legacy_name, key in ((f"{meeting_id}.mic.wav", "mic"), (f"{meeting_id}.system.wav", "system")):
        source = recordings_root / legacy_name
        if source.is_file():
            shutil.copy2(source, folder / f"{key}.wav")
            if key == "mic":
                mic_name = MIC_TARGET
            else:
                system_name = SYSTEM_TARGET

    recording = None
    if audio_name or recording_row:
        recording = {
            "audio": audio_name,
            "mic": mic_name,
            "system": system_name,
            "format": str(recording_row["format"]) if recording_row else "wav",
            "duration_seconds": duration,
            "size_bytes": int(recording_row["size_bytes"]) if recording_row else 0,
            "sample_rate": int(recording_row["sample_rate"]) if recording_row else 48_000,
            "channels": int(recording_row["channels"]) if recording_row else 2,
            "created_at": _iso(recording_row["created_at"] if recording_row else None),
        }

    # -- transcript -----------------------------------------------------
    transcript_row = connection.execute(
        "SELECT * FROM transcripts WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    transcript = None
    if transcript_row is not None:
        try:
            segments = json.loads(transcript_row["segments_json"] or "[]")
        except (TypeError, ValueError):
            segments = []
        if not isinstance(segments, list):
            segments = []
        transcript = {
            "text": str(transcript_row["text"]),
            "language": transcript_row["language"],
            "segments": segments,
            "created_at": _iso(transcript_row["created_at"]),
        }

    # -- summary + action items -----------------------------------------
    summary_row = connection.execute(
        "SELECT * FROM summaries WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    summary = None
    if summary_row is not None:
        summary = {
            "overview": str(summary_row["overview"] or ""),
            "key_points": _json_list(summary_row["key_points_json"]),
            "decisions": _json_list(summary_row["decisions_json"]),
            "open_questions": _json_list(summary_row["open_questions_json"]),
            "risks_or_blockers": _json_list(summary_row["risks_or_blockers_json"]),
            "follow_ups": _json_list(summary_row["follow_ups_json"]),
            "model": summary_row["model"],
            "created_at": _iso(summary_row["created_at"]),
        }

    action_items: list[dict] = []
    item_rows = connection.execute(
        "SELECT * FROM action_items WHERE meeting_id = ? ORDER BY id", (meeting_id,)
    ).fetchall()
    for item_row in item_rows:
        action_items.append(
            {
                "id": f"{meeting_id}#{item_row['id']}",
                "task": str(item_row["task"]),
                "owner": item_row["owner"],
                "deadline": item_row["deadline"],
                "evidence": item_row["evidence"],
                "status": str(item_row["status"] or "open"),
                "created_at": _iso(item_row["created_at"]),
            }
        )

    record = {
        "id": meeting_id,
        "profile_id": int(row["user_id"]),
        "profile": users.get(int(row["user_id"]), "Unknown"),
        "title": str(row["title"]),
        "status": str(row["status"]),
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
        "error_message": row["error_message"],
        "recording": recording,
        "transcript": transcript,
        "summary": summary,
        "action_items": action_items,
        "created_at": _iso(row["created_at"]),
        "updated_at": utc_now_iso(),
        "meeting_dir": str(folder),
    }
    library._save_record(record)
    if transcript is not None:
        library._write_transcript_md(record)
    library._write_analysis_md(record)


def _json_list(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _iso(value: object) -> str | None:
    """Normalise a legacy SQLite datetime (``YYYY-MM-DD HH:MM:SS[.ffffff]``)."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1) if " " in text else text)
    except ValueError:
        return text
    return parsed.isoformat(timespec="seconds")


def _rename_database_aside(db_path: Path) -> bool:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{_LEGACY_DB_NAME}.migrated-{stamp}")
    try:
        db_path.rename(target)
    except OSError as exc:
        logger.error("Could not archive the legacy database: %s", exc)
        return False
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(_LEGACY_DB_NAME + suffix)
        if side.is_file():
            try:
                side.rename(side.with_name(side.name + f".migrated-{stamp}"))
            except OSError:
                pass
    logger.info("Legacy database archived as %s.", target.name)
    return True
