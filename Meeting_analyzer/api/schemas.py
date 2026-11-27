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
    codec: str | None = None
    bitrate_kbps: int | None = None
    container: str | None = None
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
    error_code: str | None = None
    error_detail: dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    fallback_from: str | None = None
    fallback_reason: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    next_run_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


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


class SpeakerScoreOut(BaseModel):
    profile_id: uuid.UUID
    profile_name: str
    score: float | None = None
    has_reference: bool = True


class MeetingSpeakerOut(BaseModel):
    """One meeting-local diarized label with its review/matching state."""

    local_label: str
    segment_count: int
    speech_seconds: float
    has_embedding: bool
    embedded_profile_id: uuid.UUID | None = None
    profile_id: uuid.UUID | None = None
    profile_name: str | None = None
    confidence: float | None = None
    status: str | None = None
    origin: str | None = None
    candidates: list[SpeakerCandidate] = Field(default_factory=list)
    all_scores: list[SpeakerScoreOut] = Field(default_factory=list)
    # Other labels in this meeting whose voice print is near-identical.
    same_voice_labels: list[str] = Field(default_factory=list)
    reference_available: bool = False
    reference_count: int = 0
    best_profile_id: uuid.UUID | None = None
    best_profile_name: str | None = None
    best_score: float | None = None
    second_best_profile_id: uuid.UUID | None = None
    second_best_profile_name: str | None = None
    second_best_score: float | None = None
    score_gap: float | None = None
    ambiguity_margin: float | None = None
    threshold: float | None = None
    decision: str | None = None
    decision_reason: str | None = None
    profiles_evaluated: int | None = None
    evaluated_at: datetime | None = None
    job_status: str | None = None
    job_error: str | None = None


class SpeakerAssignRequest(BaseModel):
    """A user decision for one meeting-local speaker label."""

    profile_id: uuid.UUID | None = None
    # Create a brand-new profile with this name and confirm the label into it.
    new_profile_name: str | None = Field(default=None, min_length=1, max_length=120)
    status: str = Field(pattern="^(suggested|confirmed|rejected)$")


class SpeakerReferenceOut(BaseModel):
    id: uuid.UUID
    profile_id: uuid.UUID
    source_seconds: float | None = None
    original_filename: str | None = None
    provider: str | None = None
    model: str | None = None
    created_at: datetime


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
