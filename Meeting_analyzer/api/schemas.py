"""Pydantic response/request models for the TeamMeetingAI API.

Kept deliberately small: these mirror the database rows and validate output
shape; the repositories own the actual queries.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: uuid.UUID
    display_name: str
    created_at: datetime


class UserCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class MeetingOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    status: str
    recorded_at: datetime
    duration_ms: int | None = None
    language: str | None = None
    notes: str | None = None


class MeetingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    user_id: uuid.UUID | None = None
    notes: str | None = None


class MeetingFileOut(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    kind: str
    sha256: str
    size_bytes: int
    mime_type: str | None = None
    original_filename: str | None = None
    duration_ms: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    created_at: datetime


class TranscriptOut(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    provider: str
    model: str
    mode: str | None = None
    language: str | None = None
    is_primary: bool
    fallback_from: str | None = None
    fallback_reason: str | None = None
    text: str
    segments: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class SummaryOut(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    transcript_id: uuid.UUID | None = None
    overview: str | None = None
    payload: dict[str, Any]
    model: str
    created_at: datetime


class ActionItemOut(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    task: str
    owner: str | None = None
    deadline: str | None = None
    evidence: str | None = None
    status: str
    position: int


class ActionItemPatch(BaseModel):
    task: str | None = None
    owner: str | None = None
    deadline: str | None = None
    status: str | None = Field(default=None, pattern="^(open|done|dismissed)$")


class JobOut(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    job_type: str
    status: str
    progress: float
    message: str | None = None
    error: str | None = None
    provider: str | None = None
    model: str | None = None
    fallback_from: str | None = None
    fallback_reason: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class SearchHit(BaseModel):
    kind: str
    meeting_id: uuid.UUID
    title: str
    snippet: str
    recorded_at: datetime


class SystemStatus(BaseModel):
    schema_version: str
    meeting_count: int
    db_size_bytes: int
    disk_free_bytes: int
    last_backup: dict[str, Any] | None = None
    can_record: bool = True


class TranscriptMetricsOut(BaseModel):
    """Post-transcription metrics for the primary transcription.

    All values are measured from stored data: audio and transcription
    durations, RTF, speaker counts and labelled/unknown speech percentages
    (derived from segment durations), review-needed segment count, and the
    job status/error.
    """

    transcript_id: uuid.UUID
    computed_at: datetime
    metrics: dict[str, Any]


class SpeakerCandidate(BaseModel):
    profile_id: uuid.UUID
    display_name: str
    confidence: float


class MeetingSpeakerOut(BaseModel):
    """One meeting-local diarized label with its review state."""

    local_label: str
    segment_count: int
    speech_seconds: float
    has_embedding: bool
    embedded_profile_id: uuid.UUID | None = None
    profile_id: uuid.UUID | None = None
    profile_name: str | None = None
    confidence: float | None = None
    status: str | None = None
    candidates: list[SpeakerCandidate] = Field(default_factory=list)
    # Other labels in this meeting whose voice print is near-identical
    # (diarization split one person into several labels).
    same_voice_labels: list[str] = Field(default_factory=list)


class SpeakerAssignRequest(BaseModel):
    """A user decision for one meeting-local speaker label."""

    profile_id: uuid.UUID | None = None
    # Create a brand-new profile with this name and confirm the label into it.
    new_profile_name: str | None = Field(default=None, min_length=1, max_length=120)
    status: str = Field(pattern="^(suggested|confirmed|rejected)$")


class SpeakerProfileOut(BaseModel):
    id: uuid.UUID
    display_name: str
    created_at: datetime
    updated_at: datetime | None = None
    embedding_count: int = 0


class SpeakerProfileCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class SpeakerProfileRename(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class AttachmentUploadResult(BaseModel):
    file: MeetingFileOut
    message: str = "Attachment stored."
