"""Relational schema for the hosted TeamMeetingAI server.

Design rules (from the architecture decision):

* Binary content lives in PostgreSQL **Large Objects**; this table set stores
  only the LO OID plus metadata (SHA-256, size, MIME type, audio properties).
  Recording files are never BYTEA and never on the filesystem library.
* ``meeting_files.kind`` distinguishes legacy WAV artifacts
  (``original_audio_48k``, ``derived_audio_16k``), the canonical compressed
  Opus artifact for new recordings (``audio_opus``), source/mixed tracks, and
  attachments.
* Exactly one ``is_primary`` transcript per meeting is enforced with a partial
  unique index (summaries and chat answers always read the primary).
* Deletion is hard delete: FK cascades plus a ``BEFORE DELETE`` trigger that
  unlinks each Large Object in the same transaction.  There is no soft delete.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, OID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# Attachment rules shared by the API layer and the database constraints.
ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB
ATTACHMENT_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".png", ".jpg", ".jpeg")
ATTACHMENT_MIME_TYPES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "image/png",
    "image/jpeg",
)

AUDIO_KINDS = ("original_audio_48k", "derived_audio_16k")
CANONICAL_AUDIO_KINDS = ("audio_opus",)
SOURCE_AUDIO_KINDS = ("source_microphone", "source_system", "source_browser", "mixed_master")
FILE_KINDS = AUDIO_KINDS + CANONICAL_AUDIO_KINDS + SOURCE_AUDIO_KINDS + ("attachment",)
MEETING_STATUSES = ("recording", "recorded", "transcribed", "analyzing", "completed", "failed")
JOB_TYPES = ("transcription", "analysis", "profiling")
JOB_STATUSES = ("queued", "running", "cancelling", "succeeded", "failed", "cancelled")
# Voice-profile matching states. Clear automatic matches and explicit user
# decisions are confirmed; uncertain matches may remain suggested or unassigned.
SPEAKER_ASSIGNMENT_STATUSES = ("suggested", "confirmed", "rejected")
SPEAKER_ASSIGNMENT_ORIGINS = ("auto", "manual")

# SQL fragment shared by the attachment CHECK constraints: the rules only apply
# when the row actually is an attachment.
_IS_ATTACHMENT = "kind = 'attachment'"
_EXTENSION_RE = "\\." + "|\\.".join(ext.lstrip(".") for ext in ATTACHMENT_EXTENSIONS)
_MIME_LIST = ", ".join(f"'{mime}'" for mime in ATTACHMENT_MIME_TYPES)
_SEGMENTS_DEFAULT = text("'[]'::jsonb")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class User(Base):
    """A local profile (employee).  Authentication is out of scope for now."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    display_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()


class Meeting(Base):
    """One recorded meeting."""

    __tablename__ = "meetings"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in MEETING_STATUSES) + ")",
            name="status_allowed",
        ),
        Index("ix_meetings_recorded_at", "recorded_at"),
        Index("ix_meetings_status", "status"),
        Index("ix_meetings_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="recorded", server_default="recorded")
    # Wall-clock time the meeting was recorded; set at creation and kept stable.
    recorded_at: Mapped[datetime] = _created_at()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capture_manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class MeetingFile(Base):
    """Metadata for one Large Object belonging to a meeting.

    ``lo_oid`` references the Large Object; ``sha256`` lets us verify content
    after restore/backup and drive client-side caching.
    """

    __tablename__ = "meeting_files"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in FILE_KINDS) + ")",
            name="kind_allowed",
        ),
        # Attachment rules (defence in depth; the API validates as well).
        CheckConstraint(
            f"NOT {_IS_ATTACHMENT} OR size_bytes <= {ATTACHMENT_MAX_BYTES}",
            name="attachment_max_size",
        ),
        CheckConstraint(
            f"NOT {_IS_ATTACHMENT} OR mime_type IN ({_MIME_LIST})",
            name="attachment_mime_allowed",
        ),
        CheckConstraint(
            f"NOT {_IS_ATTACHMENT} OR original_filename ~* '{_EXTENSION_RE}'",
            name="attachment_extension_allowed",
        ),
        # One original and one derived audio track per meeting (legacy WAV).
        Index(
            "uq_meeting_files_original_audio",
            "meeting_id",
            unique=True,
            postgresql_where=text(f"kind = 'original_audio_48k'"),
        ),
        Index(
            "uq_meeting_files_derived_audio",
            "meeting_id",
            unique=True,
            postgresql_where=text(f"kind = 'derived_audio_16k'"),
        ),
        # One canonical compressed Opus artifact per meeting (new recordings).
        Index(
            "uq_meeting_files_audio_opus",
            "meeting_id",
            unique=True,
            postgresql_where=text(f"kind = 'audio_opus'"),
        ),
        Index("ix_meeting_files_meeting_id", "meeting_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    lo_oid: Mapped[int] = mapped_column(OID, nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Audio properties (null for attachments).
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    container: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Transcript(Base):
    """One provider transcript version for a meeting."""

    __tablename__ = "transcripts"
    __table_args__ = (
        CheckConstraint("provider <> ''", name="provider_not_empty"),
        Index(
            "uq_transcripts_primary",
            "meeting_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index("ix_transcripts_meeting_id", "meeting_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # Sarvam output mode, e.g. 'translit' (Roman transliteration).
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    # Set when this transcript was produced by the Whisper fallback.
    fallback_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    segments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=_SEGMENTS_DEFAULT)
    provider_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Summary(Base):
    """Validated structured analysis for a meeting (one row per meeting)."""

    __tablename__ = "summaries"
    __table_args__ = (
        Index("uq_summaries_meeting", "meeting_id", unique=True),
        Index("ix_summaries_transcript_id", "transcript_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The primary (Roman transliteration) transcript the summary was built from.
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meeting_analyzer.transcripts.id", ondelete="SET NULL"), nullable=True
    )
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class ActionItem(Base):
    """One action item extracted from a meeting; editable independently."""

    __tablename__ = "action_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'done', 'dismissed')", name="status_allowed"
        ),
        Index("ix_action_items_meeting_id", "meeting_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("meeting_analyzer.summaries.id", ondelete="SET NULL"), nullable=True
    )
    task: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = _created_at()


class ProcessingJob(Base):
    """Background job for transcription/analysis, with progress and errors."""

    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN (" + ", ".join(f"'{t}'" for t in JOB_TYPES) + ")",
            name="job_type_allowed",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in JOB_STATUSES) + ")",
            name="status_allowed",
        ),
        Index("uq_processing_jobs_active", "meeting_id", "job_type",
              unique=True,
              postgresql_where=text("status IN ('queued', 'running', 'cancelling')")),
        Index("ix_processing_jobs_meeting_id", "meeting_id"),
        Index("ix_processing_jobs_status_heartbeat", "status", "heartbeat_at"),
        Index("ix_processing_jobs_status_next_run", "status", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0"))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )


class SpeakerProfile(Base):
    """A persistent person identified by voice; names originate from users.

    Confirmed meeting assignments and explicit enrollment samples contribute
    reference embeddings to future matching.
    """

    __tablename__ = "speaker_profiles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    display_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )


class SpeakerReferenceEmbedding(Base):
    """An explicitly uploaded reference voice sample for a profile.

    References are the *only* data used to match future meeting speakers.
    Meeting-derived embeddings never contribute to this table.
    """

    __tablename__ = "speaker_reference_embeddings"
    __table_args__ = (
        Index("ix_speaker_reference_embeddings_profile_id", "profile_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.speaker_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    embedding: Mapped[list] = mapped_column(JSONB, nullable=False)
    source_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class SpeakerEmbedding(Base):
    """One voice embedding computed from a meeting speaker's segments.

    ``embedding`` is a fixed-length float vector stored as JSONB (audio
    itself stays in Large Objects; this is a derived, small numeric array).
    ``profile_id`` links the label to the profile chosen by review, but it is
    **not** reference data. References live in ``speaker_reference_embeddings``.
    """

    __tablename__ = "speaker_embeddings"
    __table_args__ = (
        Index(
            "uq_speaker_embeddings_label",
            "transcript_id",
            "local_label",
            unique=True,
        ),
        Index("ix_speaker_embeddings_profile_id", "profile_id"),
        Index("ix_speaker_embeddings_meeting_id", "meeting_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.speaker_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Last calculated matching result (persisted for reloads/observability).
    best_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.speaker_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    second_best_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.speaker_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    second_best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_gap: Mapped[float | None] = mapped_column(Float, nullable=True)
    ambiguity_margin_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    profiles_evaluated: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    all_scores: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The diarization label inside the transcript, e.g. "Speaker 0".
    local_label: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Total seconds of speech used to build this embedding (quality signal).
    source_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class SpeakerAssignment(Base):
    """User-reviewable mapping from a meeting-local speaker to a profile.

    One row per (transcript, local label). ``origin`` records whether the row
    came from automatic matching (``auto``) or a user action (``manual``).
    Manual confirmed/rejected decisions are preserved across re-profiling.
    """

    __tablename__ = "speaker_assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{s}'" for s in SPEAKER_ASSIGNMENT_STATUSES)
            + ")",
            name="status_allowed",
        ),
        CheckConstraint(
            "origin IN ("
            + ", ".join(f"'{s}'" for s in SPEAKER_ASSIGNMENT_ORIGINS)
            + ")",
            name="origin_allowed",
        ),
        Index(
            "uq_speaker_assignments_label",
            "transcript_id",
            "local_label",
            unique=True,
        ),
        Index("ix_speaker_assignments_meeting_id", "meeting_id"),
        Index("ix_speaker_assignments_profile_id", "profile_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.speaker_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    local_label: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="suggested", server_default="suggested"
    )
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )


class TranscriptMetrics(Base):
    """Reference-free quality metrics for one transcription run.

    Computed automatically after a transcription succeeds from the stored
    segments (speech coverage, timestamp ordering, duplication, speaker
    count, job latency).  Reference-based accuracy (WER and friends) is
    deliberately absent: there is no ground-truth transcript for real
    meetings, so none of those numbers are ever fabricated here.
    """

    __tablename__ = "transcript_metrics"
    __table_args__ = (
        Index(
            "uq_transcript_metrics_transcript",
            "transcript_id",
            unique=True,
        ),
        Index("ix_transcript_metrics_meeting_id", "meeting_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("meeting_analyzer.transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = _created_at()


class BackupRun(Base):
    """Record of one ``pg_dump`` backup execution for status monitoring."""

    __tablename__ = "backup_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="status_allowed"
        ),
        Index("ix_backup_runs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", server_default="running")
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
