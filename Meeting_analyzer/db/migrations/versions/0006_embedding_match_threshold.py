"""Persist the match threshold used for speaker embedding evaluations.

Schema-aware for ``meeting_analyzer``. This allows the UI/API to show the actual
threshold used for each evaluation, even after the configured threshold changes.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from db.base import MEETING_ANALYZER_SCHEMA

revision = "0006_embedding_match_threshold"
down_revision = "0005_reference_voice"
branch_labels = None
depends_on = None

S = MEETING_ANALYZER_SCHEMA


def _has_column(insp, table: str, column: str) -> bool:
    try:
        return any(c["name"] == column for c in insp.get_columns(table, schema=S))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {S}")
    insp = inspect(bind)
    if not _has_column(insp, "speaker_embeddings", "match_threshold"):
        op.add_column(
            "speaker_embeddings",
            sa.Column("match_threshold", sa.Float(), nullable=True),
            schema=S,
        )


def downgrade() -> None:
    print(
        "WARNING: downgrade for 0006_embedding_match_threshold is non-destructive and "
        "does not remove meeting_analyzer.speaker_embeddings.match_threshold. "
        "Manual reviewed rollback required."
    )
