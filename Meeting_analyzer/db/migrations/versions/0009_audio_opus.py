"""Add canonical compressed Opus audio artifact support.

Schema-aware for ``meeting_analyzer``. Additive only:
- expands meeting_files.kind to include ``audio_opus``
- adds optional audio metadata columns (codec, bitrate_kbps, container)
- adds partial unique index for one canonical audio_opus per meeting

Existing WAV rows and legacy kinds remain valid and readable.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from db.base import MEETING_ANALYZER_SCHEMA

revision = "0009_audio_opus"
down_revision = "0008_job_reliability"
branch_labels = None
depends_on = None

S = MEETING_ANALYZER_SCHEMA
TABLE = "meeting_files"
INDEX = "uq_meeting_files_audio_opus"
KIND_ALLOWED = (
    "'original_audio_48k','derived_audio_16k','audio_opus','attachment',"
    "'source_microphone','source_system','source_browser','mixed_master'"
)


def _has_column(insp, table: str, column: str) -> bool:
    try:
        return any(c["name"] == column for c in insp.get_columns(table, schema=S))
    except Exception:
        return False


def _has_index(insp, table: str, index: str) -> bool:
    try:
        return any(i["name"] == index for i in insp.get_indexes(table, schema=S))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {S}")
    insp = inspect(bind)

    for name, col_type in (
        ("codec", sa.String(32)),
        ("bitrate_kbps", sa.Integer()),
        ("container", sa.String(32)),
    ):
        if not _has_column(insp, TABLE, name):
            op.add_column(TABLE, sa.Column(name, col_type, nullable=True), schema=S)
            insp = inspect(bind)

    op.execute(f"ALTER TABLE {S}.{TABLE} DROP CONSTRAINT IF EXISTS kind_allowed")
    op.execute(
        f"ALTER TABLE {S}.{TABLE} ADD CONSTRAINT kind_allowed CHECK (kind IN ({KIND_ALLOWED}))"
    )

    if not _has_index(insp, TABLE, INDEX):
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} "
            f"ON {S}.{TABLE} (meeting_id) WHERE kind = 'audio_opus'"
        )


def downgrade() -> None:
    # Non-destructive by default for shared production DB.
    print(
        "WARNING: downgrade for 0009_audio_opus is non-destructive and does not "
        "remove meeting_analyzer columns/constraints/indexes. Manual reviewed rollback required."
    )
