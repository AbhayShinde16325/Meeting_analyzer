"""File-library CRUD, search, markdown regeneration, and deletion tests.

These mirror the former database tests: every meeting lives in its own folder
(``meeting.json`` + audio + ``transcript.md`` + ``analysis.md``) and there is
no SQLite database anywhere.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from storage import library as ops
from support import valid_analysis


def test_library_dir_is_created(db, settings):
    assert Path(settings.library_dir) == db.library_dir
    assert db.library_dir.is_dir()


def test_local_profile_reuse_is_case_insensitive(db):
    first = ops.get_or_create_user(db, "Abhay")
    second = ops.get_or_create_user(db, "  abhay  ")
    assert first["id"] == second["id"]
    assert second["display_name"] == "Abhay"
    assert len(ops.list_users(db)) == 1


def test_profiles_are_stored_in_profiles_json(db):
    user = ops.get_or_create_user(db, "Persisted User")
    data = json.loads(
        (db.library_dir / "profiles.json").read_text(encoding="utf-8")
    )
    assert any(
        profile["id"] == user["id"] and profile["display_name"] == "Persisted User"
        for profile in data["profiles"]
    )


def test_empty_profile_name_is_rejected(db):
    with pytest.raises(ValueError):
        ops.get_or_create_user(db, "   ")


def test_meeting_lifecycle_and_details(db, user, wav_factory):
    meeting = ops.create_meeting(db, user["id"], "  Planning   sync  ")
    assert meeting["title"] == "Planning sync"
    assert meeting["status"] == "recording"

    meeting_dir = Path(meeting["meeting_dir"])
    assert meeting_dir.is_dir()
    assert meeting_dir.parent == db.library_dir

    audio_path = meeting_dir / "audio.wav"
    wav_factory(audio_path, seconds=1.0)
    ops.finish_recording(
        db,
        meeting["id"],
        relative_path="audio.wav",
        format="wav",
        duration_seconds=1.0,
        size_bytes=audio_path.stat().st_size,
        sample_rate=48000,
        channels=2,
        mic_relative_path="mic.wav",
        system_relative_path="system.wav",
    )

    details = ops.get_meeting_details(db, meeting["id"])
    assert details is not None
    assert details["status"] == "recorded"
    assert details["recording"]["audio"] == "audio.wav"
    assert details["recording"]["mic"] == "mic.wav"
    assert details["recording"]["system"] == "system.wav"
    assert (meeting_dir / "meeting.json").is_file()

    ops.save_transcript(
        db,
        meeting["id"],
        text="We agreed to ship.",
        segments=[{"start": 0.0, "end": 1.0, "text": "We agreed to ship."}],
        language="en",
    )
    assert ops.get_meeting(db, meeting["id"])["status"] == "transcribed"
    transcript_md = meeting_dir / "transcript.md"
    assert transcript_md.is_file()
    assert "We agreed to ship." in transcript_md.read_text(encoding="utf-8")

    ops.save_analysis(db, meeting["id"], valid_analysis(), model="fake")
    details = ops.get_meeting_details(db, meeting["id"])
    assert details["status"] == "completed"
    assert details["summary"]["overview"]
    assert details["action_items"][0]["task"] == "Ship the release"
    assert details["action_items"][0]["id"] == f"{meeting['id']}#1"
    analysis_md = meeting_dir / "analysis.md"
    assert "Ship the release" in analysis_md.read_text(encoding="utf-8")


def test_meeting_json_is_source_of_truth_and_md_regenerates(db, user):
    meeting = ops.create_meeting(db, user["id"], "Original title")
    ops.save_transcript(
        db,
        meeting["id"],
        text="Original wording.",
        segments=[{"start": 0.0, "end": 1.0, "text": "Original wording."}],
        language="en",
    )
    meeting_dir = Path(meeting["meeting_dir"])
    transcript_md = meeting_dir / "transcript.md"
    assert "Original wording." in transcript_md.read_text(encoding="utf-8")

    # An external edit to meeting.json is picked up by a re-scan.
    record = json.loads((meeting_dir / "meeting.json").read_text(encoding="utf-8"))
    record["title"] = "Externally renamed"
    (meeting_dir / "meeting.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    db.scan()
    assert ops.get_meeting(db, meeting["id"])["title"] == "Externally renamed"

    # Re-saving the transcript regenerates the markdown from the record.
    ops.save_transcript(
        db,
        meeting["id"],
        text="Second wording.",
        segments=[{"start": 0.0, "end": 1.0, "text": "Second wording."}],
        language="en",
    )
    md_text = transcript_md.read_text(encoding="utf-8")
    assert "Second wording." in md_text
    assert "Externally renamed" in md_text
    assert "Original wording." not in md_text


def test_scan_picks_up_external_meeting_folders(db, user):
    folder = db.library_dir / "2026-01-01 10-00 External [abcd1234]"
    folder.mkdir(parents=True)
    record = {
        "id": "ext-0001",
        "profile_id": user["id"],
        "profile": user["display_name"],
        "title": "External meeting",
        "status": "recorded",
        "started_at": "2026-01-01T10:00:00",
        "ended_at": None,
        "error_message": None,
        "recording": {"audio": "audio.wav", "duration_seconds": 1.0},
        "transcript": None,
        "summary": None,
        "action_items": [],
        "created_at": "2026-01-01T10:00:00",
        "updated_at": "2026-01-01T10:00:00",
        "meeting_dir": str(folder),
    }
    (folder / "meeting.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # An unrelated folder without a record is ignored.
    (db.library_dir / "not-a-meeting").mkdir()

    assert db.scan() >= 1
    meetings = ops.list_meetings(db, user_id=user["id"])
    assert any(item["id"] == "ext-0001" for item in meetings)
    details = ops.get_meeting_details(db, "ext-0001")
    assert details["title"] == "External meeting"
    assert details["recording"]["audio"] == "audio.wav"


def test_empty_meeting_title_is_rejected(db, user):
    with pytest.raises(ValueError):
        ops.create_meeting(db, user["id"], "   ")


def test_search_covers_title_summary_transcript_and_action_items(db, user):
    first = ops.create_meeting(db, user["id"], "Zebra planning")
    second = ops.create_meeting(db, user["id"], "Ordinary meeting")

    ops.save_transcript(
        db,
        first["id"],
        text="We discussed the quasar migration.",
        segments=[{"start": 0, "end": 1, "text": "quasar migration"}],
        language="en",
    )
    ops.save_analysis(
        db,
        first["id"],
        valid_analysis(task="Prepare nebula report"),
        model=None,
    )
    ops.save_transcript(
        db,
        second["id"],
        text="Nothing unusual here.",
        segments=[],
        language="en",
    )

    assert [item["id"] for item in ops.list_meetings(db, user_id=user["id"], query="Zebra")] == [
        first["id"]
    ]
    assert [item["id"] for item in ops.list_meetings(db, user_id=user["id"], query="quasar")] == [
        first["id"]
    ]
    assert [item["id"] for item in ops.list_meetings(db, user_id=user["id"], query="nebula")] == [
        first["id"]
    ]
    assert ops.list_meetings(db, user_id=user["id"], query="missing-term") == []


def test_date_range_filter(db, user):
    meeting = ops.create_meeting(db, user["id"], "Date filter")
    started = meeting["started_at"]
    assert ops.list_meetings(
        db,
        user_id=user["id"],
        start_date=started - timedelta(minutes=1),
        end_date=started + timedelta(minutes=1),
    )
    assert not ops.list_meetings(
        db,
        user_id=user["id"],
        start_date=started + timedelta(days=1),
    )


def test_analysis_rerun_replaces_action_items(db, user):
    meeting = ops.create_meeting(db, user["id"], "Rerun analysis")
    ops.save_analysis(db, meeting["id"], valid_analysis(task="First task"), model=None)
    ops.save_analysis(
        db,
        meeting["id"],
        valid_analysis(task="Second task", owner=None, deadline=None),
        model=None,
    )

    details = ops.get_meeting_details(db, meeting["id"])
    assert len(details["action_items"]) == 1
    assert details["action_items"][0]["task"] == "Second task"
    assert details["action_items"][0]["owner"] is None
    assert details["action_items"][0]["deadline"] is None


def test_delete_meeting_removes_folder(db, processed_meeting):
    meeting_id = processed_meeting["id"]
    meeting_dir = Path(processed_meeting["meeting_dir"])
    assert meeting_dir.is_dir()

    removed = ops.delete_meeting(db, meeting_id)

    assert ops.get_meeting(db, meeting_id) is None
    assert not meeting_dir.exists()
    assert "meeting.json" in removed
    assert "audio.wav" in removed
    assert "transcript.md" in removed
    assert "analysis.md" in removed


def test_missing_recording_file_does_not_break_details(db, user):
    meeting = ops.create_meeting(db, user["id"], "Missing file")
    ops.finish_recording(
        db,
        meeting["id"],
        relative_path="audio.wav",
        format="wav",
        duration_seconds=3.0,
        size_bytes=1000,
        sample_rate=48000,
        channels=2,
    )
    details = ops.get_meeting_details(db, meeting["id"])
    assert details["recording"]["audio"] == "audio.wav"
    assert not (Path(details["meeting_dir"]) / "audio.wav").exists()


def test_action_item_status_validation(db, processed_meeting):
    details = ops.get_meeting_details(db, processed_meeting["id"])
    item_id = details["action_items"][0]["id"]
    assert item_id == f"{processed_meeting['id']}#1"

    ops.update_action_item_status(db, item_id, "done")
    with pytest.raises(ValueError):
        ops.update_action_item_status(db, item_id, "invalid")

    refreshed = ops.get_meeting_details(db, processed_meeting["id"])
    assert refreshed["action_items"][0]["status"] == "done"


def test_meeting_list_is_newest_first(db, user):
    first = ops.create_meeting(db, user["id"], "First")
    second = ops.create_meeting(db, user["id"], "Second")

    # Rewind the first meeting's timestamp so the second clearly sorts first.
    first_dir = Path(first["meeting_dir"])
    record = json.loads((first_dir / "meeting.json").read_text(encoding="utf-8"))
    record["started_at"] = "2020-01-01T00:00:00"
    (first_dir / "meeting.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    db.scan()

    meetings = ops.list_meetings(db, user_id=user["id"])
    assert meetings[0]["id"] == second["id"]
    assert meetings[1]["id"] == first["id"]


def test_clear_analysis_removes_summary_and_action_items(db, recorded_meeting):
    ops.save_transcript(
        db,
        recorded_meeting["id"],
        text="Transcript stays.",
        segments=[],
        language="en",
    )
    ops.save_analysis(
        db,
        recorded_meeting["id"],
        valid_analysis(),
        model="fake-model",
    )

    ops.clear_analysis(db, recorded_meeting["id"])

    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["summary"] is None
    assert details["action_items"] == []
    assert details["status"] == "transcribed"
    # The transcript itself is untouched.
    assert details["transcript"]["text"] == "Transcript stays."
    # The markdown no longer lists the analysis.
    analysis_md = Path(recorded_meeting["meeting_dir"]) / "analysis.md"
    assert "No analysis has been generated" in analysis_md.read_text(encoding="utf-8")


def test_resolve_meeting_file_rejects_escaping_paths(db, recorded_meeting):
    with pytest.raises(ValueError):
        db.resolve_meeting_file(recorded_meeting["id"], "../outside.wav")
    resolved = db.resolve_meeting_file(recorded_meeting["id"], "audio.wav")
    assert resolved.name == "audio.wav"
