"""Voice speaker profiles: review endpoints and profile management.

Identity policy: voice similarity only ever *suggests*.  Assignments made
here are explicit user decisions (host-only); the transcript UI shows a
profile name only for confirmed assignments.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text

from api import errors
from api.deps import ConnDep, ProfilerDep, require_host
from api.schemas import (
    JobOut,
    MeetingSpeakerOut,
    SpeakerAssignRequest,
    SpeakerProfileCreate,
    SpeakerProfileOut,
    SpeakerProfileRename,
)
from services.speaker_profiles import SpeakerProfilingError

router = APIRouter(tags=["speakers"])
profiles_router = APIRouter(prefix="/profiles", tags=["speakers"])


def _meeting_exists(conn, meeting_id) -> None:
    if conn.execute(
        text("SELECT 1 FROM meetings WHERE id = :m"), {"m": meeting_id}
    ).scalar_one_or_none() is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")


def _profiling_error(exc: SpeakerProfilingError):
    message = str(exc)
    if "not found" in message.lower():
        return errors.not_found("speaker_not_found", message)
    return errors.bad_request("invalid_speaker_operation", message)


@router.get(
    "/meetings/{meeting_id}/speakers", response_model=list[MeetingSpeakerOut]
)
def list_meeting_speakers(
    meeting_id: uuid.UUID, conn: ConnDep, profiler: ProfilerDep
) -> list[dict]:
    """Diarized speakers of the meeting with their review state and candidates."""

    _meeting_exists(conn, meeting_id)
    return profiler.list_meeting_speakers(str(meeting_id))


@router.post(
    "/meetings/{meeting_id}/speakers/{local_label}/assign",
    response_model=MeetingSpeakerOut,
    status_code=200,
)
def assign_meeting_speaker(
    _: Annotated[None, Depends(require_host)],
    meeting_id: uuid.UUID,
    local_label: str,
    payload: SpeakerAssignRequest,
    conn: ConnDep,
    profiler: ProfilerDep,
) -> dict:
    """Host-only: confirm, reject, or reset a speaker label's profile match."""

    _meeting_exists(conn, meeting_id)
    if payload.profile_id is not None:
        exists = conn.execute(
            text("SELECT 1 FROM speaker_profiles WHERE id = :p"),
            {"p": payload.profile_id},
        ).scalar_one_or_none()
        if exists is None:
            raise errors.not_found("profile_not_found", "Speaker profile not found.")
    try:
        profiler.assign_speaker(
            meeting_id=str(meeting_id),
            local_label=local_label,
            profile_id=str(payload.profile_id) if payload.profile_id else None,
            new_profile_name=payload.new_profile_name,
            status=payload.status,
        )
    except SpeakerProfilingError as exc:
        raise _profiling_error(exc) from None
    speakers = profiler.list_meeting_speakers(str(meeting_id))
    for speaker in speakers:
        if speaker["local_label"] == local_label:
            return speaker
    raise errors.not_found("speaker_not_found", "Speaker label not found.")


@router.post("/meetings/{meeting_id}/speakers/profile", response_model=JobOut, status_code=202)
def request_profiling(
    _: Annotated[None, Depends(require_host)],
    meeting_id: uuid.UUID,
    conn: ConnDep,
    profiler: ProfilerDep,
) -> dict:
    """Host-only: (re-)queue the voice-profiling job for this meeting."""

    _meeting_exists(conn, meeting_id)
    if not profiler.enabled:
        raise errors.bad_request(
            "profiling_disabled",
            "Voice profiling is disabled. Set TEAMMEETINGAI_VOICE_PROFILING=1 "
            "and install the 'resemblyzer' package.",
        )
    primary = conn.execute(
        text(
            "SELECT 1 FROM transcripts WHERE meeting_id = :m AND is_primary = true"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if primary is None:
        raise errors.bad_request(
            "no_transcript", "This meeting has no primary transcript yet."
        )
    active = conn.execute(
        text(
            "SELECT 1 FROM processing_jobs WHERE meeting_id = :m AND "
            "job_type = 'profiling' AND status IN ('queued', 'running')"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if active is not None:
        raise errors.ApiError(409, "job_active", "A profiling job is already queued.")
    row = (
        conn.execute(
            text(
                "INSERT INTO processing_jobs (id, meeting_id, job_type, status, message) "
                "VALUES (gen_random_uuid(), :m, 'profiling', 'queued', "
                "'Queued for voice profiling') RETURNING id, meeting_id, job_type, "
                "status, progress, message, error, provider, model, fallback_from, "
                "fallback_reason, created_at, finished_at"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one()
    )
    return dict(row)


@profiles_router.get("", response_model=list[SpeakerProfileOut])
def list_profiles(profiler: ProfilerDep) -> list[dict]:
    return profiler.list_profiles()


@profiles_router.post("", response_model=SpeakerProfileOut, status_code=201)
def create_profile(
    _: Annotated[None, Depends(require_host)],
    payload: SpeakerProfileCreate,
    profiler: ProfilerDep,
) -> dict:
    try:
        created = profiler.create_profile(payload.display_name)
    except SpeakerProfilingError as exc:
        raise _profiling_error(exc) from None
    return {**created, "embedding_count": 0, "updated_at": None}


@profiles_router.patch("/{profile_id}", response_model=SpeakerProfileOut)
def rename_profile(
    _: Annotated[None, Depends(require_host)],
    profile_id: uuid.UUID,
    payload: SpeakerProfileRename,
    profiler: ProfilerDep,
) -> dict:
    try:
        renamed = profiler.rename_profile(str(profile_id), payload.display_name)
    except SpeakerProfilingError as exc:
        raise _profiling_error(exc) from None
    return {
        **renamed,
        "embedding_count": profiler.profile_embedding_count(str(profile_id)),
    }


@profiles_router.delete("/{profile_id}", status_code=204)
def delete_profile(
    _: Annotated[None, Depends(require_host)],
    profile_id: uuid.UUID,
    profiler: ProfilerDep,
) -> None:
    try:
        profiler.delete_profile(str(profile_id))
    except SpeakerProfilingError as exc:
        raise _profiling_error(exc) from None
