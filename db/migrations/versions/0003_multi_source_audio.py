"""Add additive multi-source audio metadata.

Adds optional capture metadata on meetings and allows extra meeting_files kinds
for synchronized source recordings. Existing kinds/constraints remain valid.
"""

from __future__ import annotations

from alembic import op

revision = "0003_multi_source_audio"
down_revision = "0002_speaker_profiles"


def upgrade() -> None:
    op.execute("ALTER TABLE meetings ADD COLUMN IF NOT EXISTS capture_manifest JSONB")
    op.execute("ALTER TABLE meetings ADD COLUMN IF NOT EXISTS capture_mode VARCHAR(32)")
    op.execute("ALTER TABLE meeting_files DROP CONSTRAINT IF EXISTS kind_allowed")
    op.execute(
        "ALTER TABLE meeting_files ADD CONSTRAINT kind_allowed CHECK (kind IN ("
        "'original_audio_48k','derived_audio_16k','attachment',"
        "'source_microphone','source_system','source_browser','mixed_master'"
        "))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_meeting_files_meeting_kind ON meeting_files (meeting_id, kind)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_meeting_files_meeting_kind")
    op.execute("ALTER TABLE meeting_files DROP CONSTRAINT IF EXISTS kind_allowed")
    op.execute(
        "ALTER TABLE meeting_files ADD CONSTRAINT kind_allowed CHECK (kind IN ("
        "'original_audio_48k','derived_audio_16k','attachment'))"
    )
    op.execute("ALTER TABLE meetings DROP COLUMN IF EXISTS capture_mode")
    op.execute("ALTER TABLE meetings DROP COLUMN IF EXISTS capture_manifest")
