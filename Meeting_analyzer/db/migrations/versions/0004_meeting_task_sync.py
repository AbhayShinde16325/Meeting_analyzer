"""Add meeting_analyzer.meeting_task_sync mapping to public.tasks.

MA-owned integration table; no Task Manager schema/migration changes.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from db.base import MEETING_ANALYZER_SCHEMA

revision = "0004_meeting_task_sync"
down_revision = "0003_multi_source_audio"
branch_labels = None
depends_on = None

TABLE = "meeting_task_sync"


def _has_table(insp, name: str) -> bool:
    try:
        return insp.has_table(name, schema=MEETING_ANALYZER_SCHEMA)
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {MEETING_ANALYZER_SCHEMA}")
    insp = inspect(bind)
    if _has_table(insp, TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("meeting_id", sa.Uuid(as_uuid=True), sa.ForeignKey(f"{MEETING_ANALYZER_SCHEMA}.meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("transcript_id", sa.Uuid(as_uuid=True), sa.ForeignKey(f"{MEETING_ANALYZER_SCHEMA}.transcripts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("extraction_key", sa.Text(), nullable=False),
        sa.Column("action_item_position", sa.Integer(), nullable=True),
        sa.Column("owner_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("meeting_id", "extraction_key", name="uq_meeting_task_sync_meeting_key"),
        sa.UniqueConstraint("task_id", name="uq_meeting_task_sync_task_id"),
        schema=MEETING_ANALYZER_SCHEMA,
    )
    op.create_index(f"ix_{TABLE}_meeting_id", TABLE, ["meeting_id"], schema=MEETING_ANALYZER_SCHEMA)
    op.create_index(f"ix_{TABLE}_transcript_id", TABLE, ["transcript_id"], schema=MEETING_ANALYZER_SCHEMA)


def downgrade() -> None:
    # Non-destructive in shared/prod; keep data unless explicitly reviewed.
    print("WARNING: downgrade for 0004_meeting_task_sync is non-destructive.")
