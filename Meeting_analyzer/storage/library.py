"""File-backed meeting library.

Every meeting lives in its own folder inside a user-chosen library directory::

    <library>\
      2026-11-13 10-26 Release planning [6bcae10c]\
        meeting.json    # the machine-readable record (source of truth)
        audio.wav       # mixed recording used for playback and transcription
        mic.wav         # separate microphone track (when recorded)
        system.wav      # separate system-audio track (when recorded)
        transcript.md   # human-readable transcript with timestamps + labels
        analysis.md     # human-readable meeting notes

There is no SQLite database.  ``meeting.json`` is authoritative for the
application; the ``.md`` files are regenerated from it on every save so they
can be opened, shared, or versioned freely.

This module exposes both a :class:`MeetingLibrary` repository and thin
module-level functions with the same names/signatures the previous
``database.operations`` module had (taking the store as first argument), so
call sites keep working with a one-line import change.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from config import Settings

logger = logging.getLogger(__name__)

AUDIO_FILENAME = "audio.wav"
MIC_FILENAME = "mic.wav"
SYSTEM_FILENAME = "system.wav"
MEETING_FILENAME = "meeting.json"
TRANSCRIPT_FILENAME = "transcript.md"
ANALYSIS_FILENAME = "analysis.md"
PROFILES_FILENAME = "profiles.json"

ACTION_ITEM_STATUSES = {"open", "in_progress", "done"}
_TITLE_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class LibraryError(RuntimeError):
    """Raised when the meeting library cannot be read or written."""


def utc_now_iso() -> str:
    """Current UTC time as a naive ISO string (matches the legacy schema)."""

    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _format_duration_md(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "unknown"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def _md_timestamp(seconds: float) -> str:
    total = max(0, int(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _sanitize_title(title: str) -> str:
    cleaned = _TITLE_UNSAFE.sub(" ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return cleaned[:60] or "meeting"


class MeetingLibrary:
    """Repository for meeting folders inside the library directory."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if settings.library_dir is None:
            raise LibraryError(
                "No meeting library directory is configured. Choose one on "
                "first launch or set TEAMMEETINGAI_LIBRARY_DIR."
            )
        self.library_dir: Path = Path(settings.library_dir)
        try:
            self.library_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LibraryError(
                f"The meeting library folder is not usable: {exc}"
            ) from exc
        self._index: dict[str, dict[str, Any]] = {}
        self.scan()

    # -- scanning -------------------------------------------------------

    def scan(self) -> int:
        """Rebuild the in-memory meeting index from the library folders."""

        self._index.clear()
        if not self.library_dir.is_dir():
            return 0
        for folder in sorted(self.library_dir.iterdir()):
            if not folder.is_dir():
                continue
            record_path = folder / MEETING_FILENAME
            if not record_path.is_file():
                continue
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping unreadable meeting folder %s: %s", folder.name, exc
                )
                continue
            meeting_id = str(record.get("id") or "")
            if not meeting_id or meeting_id in self._index:
                continue
            record["meeting_dir"] = str(folder)
            self._index[meeting_id] = record
        logger.info("Meeting library scanned: %s meetings.", len(self._index))
        return len(self._index)

    # -- internal helpers ----------------------------------------------

    def _record_path(self, meeting_id: str) -> Path:
        folder = self._index.get(meeting_id, {}).get("meeting_dir")
        if folder:
            return Path(folder) / MEETING_FILENAME
        return self.library_dir / meeting_id / MEETING_FILENAME

    def _load_record(self, meeting_id: str) -> dict[str, Any]:
        record = self._index.get(meeting_id)
        if record is None:
            raise LookupError("Meeting not found.")
        try:
            fresh = json.loads(
                (Path(record["meeting_dir"]) / MEETING_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, TypeError, ValueError) as exc:
            raise LibraryError(
                "The meeting record could not be read. The library folder "
                "may have been moved or is not accessible."
            ) from exc
        if not isinstance(fresh, dict):
            raise LibraryError("The meeting record is invalid.")
        return fresh

    def _save_record(self, record: Mapping[str, Any]) -> None:
        folder = Path(record["meeting_dir"])
        record = dict(record)
        record["updated_at"] = utc_now_iso()
        self._atomic_write_json(folder / MEETING_FILENAME, record)
        self._index[str(record["id"])] = dict(record)

    @staticmethod
    def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temp_path, path)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)

    def meeting_dir(self, meeting_id: str) -> Path:
        folder = self._index.get(meeting_id, {}).get("meeting_dir")
        if not folder:
            raise LookupError("Meeting not found.")
        return Path(folder)

    def resolve_meeting_file(self, meeting_id: str, filename: str) -> Path:
        """Resolve a file inside a meeting folder, refusing to escape it."""

        folder = self.meeting_dir(meeting_id)
        candidate = (folder / filename).resolve()
        try:
            candidate.relative_to(folder.resolve())
        except ValueError as exc:
            raise ValueError("File path escapes the meeting folder.") from exc
        return candidate

    # -- profiles -------------------------------------------------------

    def _load_profiles(self) -> dict[str, Any]:
        path = self.library_dir / PROFILES_FILENAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {}
        data.setdefault("next_id", 1)
        profiles = data.get("profiles")
        if not isinstance(profiles, list):
            profiles = []
        data["profiles"] = profiles
        return data

    def _save_profiles(self, data: Mapping[str, Any]) -> None:
        self._atomic_write_json(
            self.library_dir / PROFILES_FILENAME,
            {
                "next_id": int(data["next_id"]),
                "profiles": list(data["profiles"]),
            },
        )

    def list_users(self) -> list[dict[str, Any]]:
        profiles = self._load_profiles()["profiles"]
        return sorted(
            (
                {
                    "id": int(profile["id"]),
                    "display_name": str(profile.get("display_name", "")),
                    "created_at": _parse_datetime(profile.get("created_at")),
                }
                for profile in profiles
                if isinstance(profile, dict) and "id" in profile
            ),
            key=lambda profile: profile["display_name"].lower(),
        )

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        for profile in self._load_profiles()["profiles"]:
            if isinstance(profile, dict) and int(profile.get("id", -1)) == int(user_id):
                return {
                    "id": int(profile["id"]),
                    "display_name": str(profile.get("display_name", "")),
                    "created_at": _parse_datetime(profile.get("created_at")),
                }
        return None

    def get_or_create_user(self, display_name: str) -> dict[str, Any]:
        """Create a local profile or return the existing profile with that name."""

        name = " ".join(str(display_name).split())
        if not name:
            raise ValueError("Please enter a profile name.")
        if len(name) > 120:
            raise ValueError("Profile name is too long (maximum 120 characters).")

        data = self._load_profiles()
        profiles = data["profiles"]
        for profile in profiles:
            if (
                isinstance(profile, dict)
                and str(profile.get("display_name", "")).lower() == name.lower()
            ):
                return {
                    "id": int(profile["id"]),
                    "display_name": str(profile.get("display_name", "")),
                    "created_at": _parse_datetime(profile.get("created_at")),
                }
        new_profile = {
            "id": int(data["next_id"]),
            "display_name": name,
            "created_at": utc_now_iso(),
        }
        profiles.append(new_profile)
        data["next_id"] = int(data["next_id"]) + 1
        self._save_profiles(data)
        return {
            "id": new_profile["id"],
            "display_name": new_profile["display_name"],
            "created_at": _parse_datetime(new_profile["created_at"]),
        }

    # -- meetings -------------------------------------------------------

    def _brief(self, record: Mapping[str, Any]) -> dict[str, Any]:
        recording = record.get("recording") or {}
        return {
            "id": record["id"],
            "user_id": record.get("profile_id"),
            "user_name": record.get("profile"),
            "title": record.get("title", ""),
            "status": record.get("status", ""),
            "started_at": _parse_datetime(record.get("started_at")),
            "ended_at": _parse_datetime(record.get("ended_at")),
            "duration_seconds": recording.get("duration_seconds"),
            "meeting_dir": record.get("meeting_dir"),
            "has_recording": recording.get("audio") is not None,
            "has_transcript": record.get("transcript") is not None,
            "has_summary": record.get("summary") is not None,
            "action_item_count": len(record.get("action_items") or []),
            "error_message": record.get("error_message"),
            "created_at": _parse_datetime(record.get("created_at")),
            "updated_at": _parse_datetime(record.get("updated_at")),
        }

    def create_meeting(self, user_id: int, title: str) -> dict[str, Any]:
        cleaned_title = " ".join(str(title).split())
        if not cleaned_title:
            raise ValueError("Please enter a meeting title.")
        if len(cleaned_title) > 300:
            raise ValueError("Meeting title is too long (maximum 300 characters).")

        profile = self.get_user(int(user_id))
        if profile is None:
            raise ValueError("The selected local profile no longer exists.")

        meeting_id = str(uuid.uuid4())
        started = utc_now_iso()
        folder_name = self._unique_folder_name(started, cleaned_title, meeting_id)
        folder = self.library_dir / folder_name
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise LibraryError(f"Could not create the meeting folder: {exc}") from exc

        record: dict[str, Any] = {
            "id": meeting_id,
            "profile_id": profile["id"],
            "profile": profile["display_name"],
            "title": cleaned_title,
            "status": "recording",
            "started_at": started,
            "ended_at": None,
            "error_message": None,
            "recording": None,
            "transcript": None,
            "summary": None,
            "action_items": [],
            "created_at": started,
            "updated_at": started,
            "meeting_dir": str(folder),
        }
        self._save_record(record)
        logger.info("Created meeting %s in %s.", meeting_id, folder.name)
        return self._brief(record)

    def _unique_folder_name(self, started_iso: str, title: str, meeting_id: str) -> str:
        try:
            stamp = datetime.fromisoformat(started_iso).strftime("%Y-%m-%d %H-%M-%S")
        except ValueError:
            stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        base = f"{stamp} {_sanitize_title(title)} [{meeting_id[:8]}]"
        candidate = self.library_dir / base
        if not candidate.exists():
            return base
        for index in range(1, 1000):
            candidate = self.library_dir / f"{base} ({index})"
            if not candidate.exists():
                return candidate.name
        raise LibraryError("Could not choose a unique meeting folder name.")

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        record = self._index.get(meeting_id)
        return self._brief(record) if record is not None else None

    def get_meeting_details(self, meeting_id: str) -> dict[str, Any] | None:
        if meeting_id not in self._index:
            return None
        record = self._load_record(meeting_id)
        details = self._brief(record)
        details["recording"] = self._recording_dict(record.get("recording"))
        details["transcript"] = self._transcript_dict(record.get("transcript"))
        details["summary"] = self._summary_dict(record.get("summary"))
        details["action_items"] = [
            self._action_item_dict(item)
            for item in (record.get("action_items") or [])
        ]
        return details

    @staticmethod
    def _recording_dict(recording: Any) -> dict[str, Any] | None:
        if not isinstance(recording, dict):
            return None
        return {
            "audio": recording.get("audio"),
            "mic": recording.get("mic"),
            "system": recording.get("system"),
            "format": recording.get("format", "wav"),
            "duration_seconds": recording.get("duration_seconds"),
            "size_bytes": recording.get("size_bytes"),
            "sample_rate": recording.get("sample_rate"),
            "channels": recording.get("channels"),
            "created_at": _parse_datetime(recording.get("created_at")),
        }

    @staticmethod
    def _transcript_dict(transcript: Any) -> dict[str, Any] | None:
        if not isinstance(transcript, dict):
            return None
        segments = transcript.get("segments")
        if not isinstance(segments, list):
            segments = []
        return {
            "text": transcript.get("text", ""),
            "language": transcript.get("language"),
            "segments": segments,
            "created_at": _parse_datetime(transcript.get("created_at")),
        }

    @staticmethod
    def _summary_dict(summary: Any) -> dict[str, Any] | None:
        if not isinstance(summary, dict):
            return None

        def _list(key: str) -> list[str]:
            values = summary.get(key)
            if not isinstance(values, list):
                return []
            return [str(value) for value in values if str(value)]

        return {
            "overview": summary.get("overview", ""),
            "key_points": _list("key_points"),
            "decisions": _list("decisions"),
            "open_questions": _list("open_questions"),
            "risks_or_blockers": _list("risks_or_blockers"),
            "follow_ups": _list("follow_ups"),
            "model": summary.get("model"),
            "created_at": _parse_datetime(summary.get("created_at")),
        }

    @staticmethod
    def _action_item_dict(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "task": item.get("task", ""),
            "owner": item.get("owner"),
            "deadline": item.get("deadline"),
            "evidence": item.get("evidence"),
            "status": item.get("status", "open"),
            "created_at": _parse_datetime(item.get("created_at")),
        }

    def list_meetings(
        self,
        *,
        user_id: int | None = None,
        query: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """List meetings newest first, optionally filtered by text and dates.

        Text search covers the title, summary overview, transcript text, and
        action-item tasks by reading each meeting record from disk.
        """

        cleaned_query = " ".join((query or "").split()).lower()
        results: list[tuple[str, dict[str, Any]]] = []
        for meeting_id, index_record in self._index.items():
            if user_id is not None and index_record.get("profile_id") != int(user_id):
                continue
            started = _parse_datetime(index_record.get("started_at"))
            if start_date is not None and (started is None or started < start_date):
                continue
            if end_date is not None and (started is None or started > end_date):
                continue
            if cleaned_query and not self._matches_query(meeting_id, cleaned_query):
                continue
            results.append((str(index_record.get("started_at") or ""), self._brief(index_record)))

        results.sort(key=lambda item: item[0], reverse=True)
        return [brief for _stamp, brief in results[: max(1, int(limit))]]

    def _matches_query(self, meeting_id: str, query: str) -> bool:
        try:
            record = self._load_record(meeting_id)
        except LibraryError:
            return False
        haystacks: list[str] = [str(record.get("title", ""))]
        summary = record.get("summary")
        if isinstance(summary, dict):
            haystacks.append(str(summary.get("overview", "")))
        transcript = record.get("transcript")
        if isinstance(transcript, dict):
            haystacks.append(str(transcript.get("text", "")))
        for item in record.get("action_items") or []:
            if isinstance(item, dict):
                haystacks.append(str(item.get("task", "")))
        return any(query in text.lower() for text in haystacks)

    def finish_recording(
        self,
        meeting_id: str,
        *,
        relative_path: str,
        format: str,
        duration_seconds: float,
        size_bytes: int,
        sample_rate: int,
        channels: int,
        mic_relative_path: str | None = None,
        system_relative_path: str | None = None,
    ) -> None:
        """Attach the finalised recording files to the meeting record."""

        record = self._load_record(meeting_id)
        record["recording"] = {
            "audio": relative_path,
            "mic": mic_relative_path,
            "system": system_relative_path,
            "format": format,
            "duration_seconds": float(duration_seconds),
            "size_bytes": int(size_bytes),
            "sample_rate": int(sample_rate),
            "channels": int(channels),
            "created_at": utc_now_iso(),
        }
        record["status"] = "recorded"
        record["ended_at"] = record.get("ended_at") or utc_now_iso()
        record["error_message"] = None
        self._save_record(record)

    def set_meeting_status(
        self,
        meeting_id: str,
        status: str,
        *,
        error_message: str | None = None,
    ) -> None:
        record = self._load_record(meeting_id)
        record["status"] = status
        record["error_message"] = error_message
        self._save_record(record)

    def mark_meeting_failed(self, meeting_id: str, message: str) -> None:
        try:
            record = self._load_record(meeting_id)
        except LibraryError:
            return
        record["status"] = "failed"
        record["error_message"] = str(message)[:2000]
        if record.get("ended_at") is None:
            record["ended_at"] = utc_now_iso()
        self._save_record(record)

    # -- transcripts ----------------------------------------------------

    def save_transcript(
        self,
        meeting_id: str,
        *,
        text: str,
        segments: Sequence[Mapping[str, Any]],
        language: str | None,
    ) -> None:
        if not text or not text.strip():
            raise ValueError("Refusing to save an empty transcript.")

        record = self._load_record(meeting_id)
        record["transcript"] = {
            "text": text,
            "language": language,
            "segments": [dict(segment) for segment in segments],
            "created_at": utc_now_iso(),
        }
        if record.get("summary") is None:
            record["status"] = "transcribed"
        record["error_message"] = None
        self._save_record(record)
        self._write_transcript_md(record)
        logger.info("Transcript saved for meeting %s.", meeting_id)

    def get_transcript(self, meeting_id: str) -> dict[str, Any] | None:
        record = self._load_record(meeting_id)
        return self._transcript_dict(record.get("transcript"))

    # -- analysis -------------------------------------------------------

    def save_analysis(
        self,
        meeting_id: str,
        analysis: Mapping[str, Any],
        *,
        model: str | None,
    ) -> None:
        """Atomically replace the summary and action items for a meeting."""

        record = self._load_record(meeting_id)
        meeting_id_value = str(record["id"])
        items: list[dict[str, Any]] = []
        for index, raw_item in enumerate(analysis.get("action_items", []) or [], start=1):
            if not isinstance(raw_item, Mapping):
                continue
            task = str(raw_item.get("task", "")).strip()
            if not task:
                continue
            items.append(
                {
                    "id": f"{meeting_id_value}#{index}",
                    "task": task,
                    "owner": _optional_text(raw_item.get("owner")),
                    "deadline": _optional_text(raw_item.get("deadline")),
                    "evidence": _optional_text(raw_item.get("evidence")),
                    "status": "open",
                    "created_at": utc_now_iso(),
                }
            )
        record["summary"] = {
            "overview": str(analysis.get("overview", "")),
            "key_points": _clean_list(analysis.get("key_points")),
            "decisions": _clean_list(analysis.get("decisions")),
            "open_questions": _clean_list(analysis.get("open_questions")),
            "risks_or_blockers": _clean_list(analysis.get("risks_or_blockers")),
            "follow_ups": _clean_list(analysis.get("follow_ups")),
            "model": model,
            "created_at": utc_now_iso(),
        }
        record["action_items"] = items
        record["status"] = "completed"
        record["error_message"] = None
        self._save_record(record)
        self._write_analysis_md(record)
        logger.info("Analysis saved for meeting %s.", meeting_id)

    def get_analysis(self, meeting_id: str) -> dict[str, Any] | None:
        record = self._load_record(meeting_id)
        summary = self._summary_dict(record.get("summary"))
        if summary is None:
            return None
        summary["action_items"] = [
            self._action_item_dict(item)
            for item in (record.get("action_items") or [])
        ]
        return summary

    def clear_analysis(self, meeting_id: str) -> None:
        """Delete the saved summary and action items for a meeting."""

        record = self._load_record(meeting_id)
        record["summary"] = None
        record["action_items"] = []
        record["status"] = "transcribed"
        self._save_record(record)
        self._write_analysis_md(record)

    def update_action_item_status(
        self, action_item_id: str, status: str
    ) -> None:
        if status not in ACTION_ITEM_STATUSES:
            raise ValueError("Unsupported action-item status.")
        target = str(action_item_id)
        for meeting_id in list(self._index):
            try:
                record = self._load_record(meeting_id)
            except LibraryError:
                continue
            items = record.get("action_items") or []
            for item in items:
                if isinstance(item, dict) and str(item.get("id")) == target:
                    item["status"] = status
                    self._save_record(record)
                    self._write_analysis_md(record)
                    return
        raise LookupError("Action item not found.")

    # -- deletion -------------------------------------------------------

    def delete_meeting(self, meeting_id: str) -> list[str]:
        """Delete a meeting folder tree and return the removed file names."""

        folder = self.meeting_dir(meeting_id)
        removed: list[str] = []
        if folder.is_dir():
            removed = sorted(child.name for child in folder.iterdir() if child.is_file())
            shutil.rmtree(folder)
        self._index.pop(meeting_id, None)
        logger.info("Deleted meeting %s and its folder.", meeting_id)
        return removed

    # -- markdown documents ---------------------------------------------

    def _write_transcript_md(self, record: Mapping[str, Any]) -> None:
        folder = Path(record["meeting_dir"])
        transcript = record.get("transcript") or {}
        segments = transcript.get("segments") or []
        started = _parse_datetime(record.get("started_at"))
        lines: list[str] = [
            f"# Transcript — {record.get('title', 'Untitled meeting')}",
            "",
            f"- **Date:** {started.strftime('%Y-%m-%d %H:%M') if started else 'unknown'}",
            f"- **Duration:** {_format_duration_md((record.get('recording') or {}).get('duration_seconds'))}",
        ]
        language = transcript.get("language")
        if language:
            lines.append(f"- **Language:** {language}")
        lines += ["", "## Transcript", ""]
        if segments:
            for segment in segments:
                try:
                    stamp = _md_timestamp(float(segment.get("start", 0.0)))
                except (TypeError, ValueError):
                    stamp = "00:00"
                speaker = segment.get("speaker")
                text = str(segment.get("text", "")).replace("\n", " ").strip()
                if isinstance(speaker, str) and speaker.strip():
                    lines.append(f"**[{stamp}] {speaker.strip()}:** {text}")
                else:
                    lines.append(f"**[{stamp}]** {text}")
        else:
            body = str(transcript.get("text", "")).strip()
            if body:
                lines.append(body)
        lines.append("")
        try:
            self._atomic_write_text(folder / TRANSCRIPT_FILENAME, "\n".join(lines))
        except OSError as exc:
            logger.warning("Could not write transcript.md for meeting %s.", record.get("id"))
            logger.debug("transcript.md write error: %s", exc)

    def _write_analysis_md(self, record: Mapping[str, Any]) -> None:
        folder = Path(record["meeting_dir"])
        summary = record.get("summary")
        started = _parse_datetime(record.get("started_at"))
        lines: list[str] = [
            f"# Meeting analysis — {record.get('title', 'Untitled meeting')}",
            "",
            f"- **Date:** {started.strftime('%Y-%m-%d %H:%M') if started else 'unknown'}",
            f"- **Duration:** {_format_duration_md((record.get('recording') or {}).get('duration_seconds'))}",
        ]
        if not isinstance(summary, dict):
            lines += [
                "",
                "_No analysis has been generated for this meeting yet._",
                "",
            ]
            try:
                self._atomic_write_text(folder / ANALYSIS_FILENAME, "\n".join(lines))
            except OSError:
                logger.warning("Could not write analysis.md for meeting %s.", record.get("id"))
            return

        model = summary.get("model")
        if model:
            lines.append(f"- **Model:** {model}")
        lines += ["", "## Overview", "", str(summary.get("overview", "")).strip() or "(none)", ""]

        for title, key in (
            ("Key points", "key_points"),
            ("Decisions", "decisions"),
            ("Open questions", "open_questions"),
            ("Risks and blockers", "risks_or_blockers"),
            ("Suggested follow-ups", "follow_ups"),
        ):
            lines.append(f"## {title}")
            lines.append("")
            values = summary.get(key) or []
            if values:
                lines.extend(f"- {value}" for value in values)
            else:
                lines.append("_None recorded._")
            lines.append("")

        lines.append("## Action items")
        lines.append("")
        items = record.get("action_items") or []
        if items:
            lines.append("| Task | Owner | Deadline | Status | Evidence |")
            lines.append("| --- | --- | --- | --- | --- |")
            for item in items:
                lines.append(
                    "| {task} | {owner} | {deadline} | {status} | {evidence} |".format(
                        task=_md_escape(str(item.get("task", ""))),
                        owner=_md_escape(str(item.get("owner") or "Not specified")),
                        deadline=_md_escape(str(item.get("deadline") or "Not specified")),
                        status=_md_escape(str(item.get("status", "open"))),
                        evidence=_md_escape(str(item.get("evidence") or "")),
                    )
                )
        else:
            lines.append("_No action items were extracted._")
        lines.append("")
        try:
            self._atomic_write_text(folder / ANALYSIS_FILENAME, "\n".join(lines))
        except OSError:
            logger.warning("Could not write analysis.md for meeting %s.", record.get("id"))


def count_library_meetings(path: Path | str) -> int:
    """Count meeting folders (subfolders containing ``meeting.json``) in *path*.

    Used by the library settings dialog to preview a candidate folder.  Only
    the top level is inspected, so a large directory cannot slow the dialog
    down noticeably.
    """

    folder = Path(path)
    if not folder.is_dir():
        return 0
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return 0
    return sum(
        1 for entry in entries if entry.is_dir() and (entry / MEETING_FILENAME).is_file()
    )


def _md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _clean_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value).strip()]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "unknown", "n/a", "not specified"}:
        return None
    return text


# ---------------------------------------------------------------------------
# Thin function API mirroring the previous database.operations surface
# ---------------------------------------------------------------------------


def list_users(db: MeetingLibrary) -> list[dict[str, Any]]:
    return db.list_users()


def get_user(db: MeetingLibrary, user_id: int) -> dict[str, Any] | None:
    return db.get_user(int(user_id))


def get_or_create_user(db: MeetingLibrary, display_name: str) -> dict[str, Any]:
    return db.get_or_create_user(display_name)


def create_meeting(db: MeetingLibrary, user_id: int, title: str) -> dict[str, Any]:
    return db.create_meeting(int(user_id), title)


def get_meeting(db: MeetingLibrary, meeting_id: str) -> dict[str, Any] | None:
    return db.get_meeting(meeting_id)


def get_meeting_details(db: MeetingLibrary, meeting_id: str) -> dict[str, Any] | None:
    return db.get_meeting_details(meeting_id)


def list_meetings(db: MeetingLibrary, **kwargs: Any) -> list[dict[str, Any]]:
    return db.list_meetings(**kwargs)


def finish_recording(db: MeetingLibrary, meeting_id: str, **kwargs: Any) -> None:
    db.finish_recording(meeting_id, **kwargs)


def set_meeting_status(
    db: MeetingLibrary,
    meeting_id: str,
    status: str,
    *,
    error_message: str | None = None,
) -> None:
    db.set_meeting_status(meeting_id, status, error_message=error_message)


def mark_meeting_failed(db: MeetingLibrary, meeting_id: str, message: str) -> None:
    db.mark_meeting_failed(meeting_id, message)


def save_transcript(
    db: MeetingLibrary,
    meeting_id: str,
    *,
    text: str,
    segments: Sequence[Mapping[str, Any]],
    language: str | None,
) -> None:
    db.save_transcript(meeting_id, text=text, segments=segments, language=language)


def get_transcript(db: MeetingLibrary, meeting_id: str) -> dict[str, Any] | None:
    return db.get_transcript(meeting_id)


def save_analysis(
    db: MeetingLibrary,
    meeting_id: str,
    analysis: Mapping[str, Any],
    *,
    model: str | None,
) -> None:
    db.save_analysis(meeting_id, analysis, model=model)


def get_analysis(db: MeetingLibrary, meeting_id: str) -> dict[str, Any] | None:
    return db.get_analysis(meeting_id)


def clear_analysis(db: MeetingLibrary, meeting_id: str) -> None:
    db.clear_analysis(meeting_id)


def update_action_item_status(
    db: MeetingLibrary, action_item_id: str, status: str
) -> None:
    db.update_action_item_status(action_item_id, status)


def delete_meeting(db: MeetingLibrary, meeting_id: str) -> list[str]:
    return db.delete_meeting(meeting_id)
