"""Add reference-only voice profiles, diagnostics, and assignment provenance.

Schema-aware for ``meeting_analyzer``. Does not touch Task Manager public tables.
Legacy explicit enrollment embeddings (provider='enrollment') are copied
idempotently into ``speaker_reference_embeddings``; original rows are preserved.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from db.base import MEETING_ANALYZER_SCHEMA

revision = "0005_reference_voice"
down_revision = "0004_meeting_task_sync"
branch_labels = None
depends_on = None

S = MEETING_ANALYZER_SCHEMA
REF_TABLE = "speaker_reference_embeddings"


def _has_table(insp, name: str) -> bool:
    try:
        return insp.has_table(name, schema=S)
    except Exception:
        return False


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

    if not _has_table(insp, REF_TABLE):
        op.create_table(
            REF_TABLE,
            sa.Column(
                "id",
                sa.Uuid(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "profile_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey(f"{S}.speaker_profiles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("embedding", sa.dialects.postgresql.JSONB(), nullable=False),
            sa.Column("source_seconds", sa.Float(), nullable=True),
            sa.Column("original_filename", sa.Text(), nullable=True),
            sa.Column("provider", sa.Text(), nullable=True),
            sa.Column("model", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            schema=S,
        )
        insp = inspect(bind)

    if _has_table(insp, REF_TABLE) and not _has_index(
        insp, REF_TABLE, f"ix_{REF_TABLE}_profile_id"
    ):
        op.create_index(
            f"ix_{REF_TABLE}_profile_id", REF_TABLE, ["profile_id"], schema=S
        )

    # Speaker embedding diagnostics
    if _has_table(insp, "speaker_embeddings"):
        if not _has_column(insp, "speaker_embeddings", "best_profile_id"):
            op.add_column(
                "speaker_embeddings",
                sa.Column("best_profile_id", sa.Uuid(as_uuid=True), nullable=True),
                schema=S,
            )
        if not _has_column(insp, "speaker_embeddings", "best_score"):
            op.add_column(
                "speaker_embeddings",
                sa.Column("best_score", sa.Float(), nullable=True),
                schema=S,
            )
        if not _has_column(insp, "speaker_embeddings", "decision"):
            op.add_column(
                "speaker_embeddings",
                sa.Column("decision", sa.String(32), nullable=True),
                schema=S,
            )
        if not _has_column(insp, "speaker_embeddings", "profiles_evaluated"):
            op.add_column(
                "speaker_embeddings",
                sa.Column("profiles_evaluated", sa.Integer(), nullable=True),
                schema=S,
            )
        if not _has_column(insp, "speaker_embeddings", "evaluated_at"):
            op.add_column(
                "speaker_embeddings",
                sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
                schema=S,
            )
        # FK for best_profile_id (best-effort, idempotent)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE c.conname = 'fk_speaker_embeddings_best_profile_id_speaker_profiles'
                      AND n.nspname = '{S}' AND t.relname = 'speaker_embeddings'
                ) THEN
                    ALTER TABLE {S}.speaker_embeddings
                    ADD CONSTRAINT fk_speaker_embeddings_best_profile_id_speaker_profiles
                    FOREIGN KEY (best_profile_id) REFERENCES {S}.speaker_profiles(id)
                    ON DELETE SET NULL;
                END IF;
            END $$;
            """
        )

    # Assignment provenance
    if _has_table(insp, "speaker_assignments"):
        if not _has_column(insp, "speaker_assignments", "origin"):
            op.add_column(
                "speaker_assignments",
                sa.Column(
                    "origin",
                    sa.String(16),
                    nullable=False,
                    server_default="manual",
                ),
                schema=S,
            )
        op.execute(
            f"UPDATE {S}.speaker_assignments SET origin='manual' WHERE origin IS NULL"
        )
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE c.conname = 'ck_speaker_assignments_origin_allowed'
                      AND n.nspname = '{S}' AND t.relname = 'speaker_assignments'
                ) THEN
                    ALTER TABLE {S}.speaker_assignments
                    ADD CONSTRAINT ck_speaker_assignments_origin_allowed
                    CHECK (origin IN ('auto', 'manual'));
                END IF;
            END $$;
            """
        )

    # Idempotent copy of legacy explicit enrollment embeddings. Preserve originals.
    if _has_table(insp, REF_TABLE) and _has_table(insp, "speaker_embeddings"):
        op.execute(
            f"""
            INSERT INTO {S}.speaker_reference_embeddings
                (id, profile_id, embedding, source_seconds, original_filename, provider, model, created_at)
            SELECT gen_random_uuid(), e.profile_id, e.embedding, e.source_seconds,
                   NULL, COALESCE(t.provider, 'enrollment'), t.model, e.created_at
            FROM {S}.speaker_embeddings e
            JOIN {S}.transcripts t ON t.id = e.transcript_id
            WHERE e.profile_id IS NOT NULL
              AND t.provider = 'enrollment'
              AND NOT EXISTS (
                  SELECT 1 FROM {S}.speaker_reference_embeddings r
                  WHERE r.profile_id = e.profile_id
                    AND r.embedding = e.embedding
                    AND COALESCE(r.source_seconds, -1) = COALESCE(e.source_seconds, -1)
                    AND r.created_at = e.created_at
              )
            """
        )


def downgrade() -> None:
    # Non-destructive by default for shared production DB.
    print(
        "WARNING: downgrade for 0005_reference_voice is non-destructive and does not "
        "remove meeting_analyzer reference/diagnostic/provenance data. Manual reviewed "
        "rollback required."
    )
