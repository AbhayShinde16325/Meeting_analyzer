"""Persist complete speaker-matching diagnostics per evaluation.

Schema-aware for ``meeting_analyzer``. Adds score matrix, second-best, gap,
ambiguity margin, and decision reason to speaker_embeddings so the UI/API can
explain why a speaker was matched, left unknown, or flagged ambiguous.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from db.base import MEETING_ANALYZER_SCHEMA

revision = "0007_matching_diagnostics"
down_revision = "0006_embedding_match_threshold"
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

    cols = {
        "second_best_profile_id": sa.Uuid(as_uuid=True),
        "second_best_score": sa.Float(),
        "score_gap": sa.Float(),
        "ambiguity_margin_used": sa.Float(),
        "decision_reason": sa.Text(),
        "all_scores": sa.dialects.postgresql.JSONB(),
    }
    for name, col_type in cols.items():
        if not _has_column(insp, "speaker_embeddings", name):
            op.add_column("speaker_embeddings", sa.Column(name, col_type, nullable=True), schema=S)
            insp = inspect(bind)

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE c.conname = 'fk_speaker_embeddings_second_best_profile_id_speaker_profiles'
                  AND n.nspname = '{S}' AND t.relname = 'speaker_embeddings'
            ) THEN
                ALTER TABLE {S}.speaker_embeddings
                ADD CONSTRAINT fk_speaker_embeddings_second_best_profile_id_speaker_profiles
                FOREIGN KEY (second_best_profile_id) REFERENCES {S}.speaker_profiles(id)
                ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    print(
        "WARNING: downgrade for 0007_matching_diagnostics is non-destructive and "
        "does not remove added speaker_embeddings diagnostic columns. Manual reviewed rollback required."
    )
