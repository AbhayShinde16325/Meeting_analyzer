"""Add additive multi-source audio metadata.

Schema-aware for ``meeting_analyzer``. Does not touch Task Manager public tables.
"""

from __future__ import annotations

from alembic import op

from db.base import MEETING_ANALYZER_SCHEMA

revision = "0003_multi_source_audio"
down_revision = "0002_speaker_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {MEETING_ANALYZER_SCHEMA}")
    op.execute(
        f"ALTER TABLE {MEETING_ANALYZER_SCHEMA}.meetings "
        "ADD COLUMN IF NOT EXISTS capture_manifest JSONB"
    )
    op.execute(
        f"ALTER TABLE {MEETING_ANALYZER_SCHEMA}.meetings "
        "ADD COLUMN IF NOT EXISTS capture_mode VARCHAR(32)"
    )
    op.execute(
        f"ALTER TABLE {MEETING_ANALYZER_SCHEMA}.meeting_files "
        "DROP CONSTRAINT IF EXISTS kind_allowed"
    )
    op.execute(
        f"ALTER TABLE {MEETING_ANALYZER_SCHEMA}.meeting_files "
        "ADD CONSTRAINT kind_allowed CHECK (kind IN ("
        "'original_audio_48k','derived_audio_16k','attachment',"
        "'source_microphone','source_system','source_browser','mixed_master'"
        "))"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_meeting_files_meeting_kind "
        f"ON {MEETING_ANALYZER_SCHEMA}.meeting_files (meeting_id, kind)"
    )


def downgrade() -> None:
    # Non-destructive by default for shared production DB.
    print(
        "WARNING: downgrade for 0003_multi_source_audio is non-destructive and does not "
        "remove meeting_analyzer columns/constraints. Manual reviewed rollback required."
    )
