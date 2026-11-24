"""Add voice speaker profiles, embeddings, and assignments.

Schema-aware migration for ``meeting_analyzer``. Works with independent
``meeting_analyzer.alembic_version`` and does not touch Task Manager public tables.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from db.base import MEETING_ANALYZER_SCHEMA, Base

# Import models so metadata is populated.
import db.models  # noqa: F401

revision = "0002_speaker_profiles"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_TABLES = ("speaker_profiles", "speaker_embeddings", "speaker_assignments")
_JOB_TYPE_CHECK = "ck_processing_jobs_job_type_allowed"
_JOB_TYPE_OLD = "('transcription', 'analysis')"
_JOB_TYPE_NEW = "('transcription', 'analysis', 'profiling')"


def _table_exists(inspector, table: str) -> bool:
    try:
        return inspector.has_table(table, schema=MEETING_ANALYZER_SCHEMA)
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {MEETING_ANALYZER_SCHEMA}")

    for name in _TABLES:
        if not _table_exists(inspector, name):
            Base.metadata.tables[f"{MEETING_ANALYZER_SCHEMA}.{name}"].create(bind=bind, checkfirst=True)
            inspector = inspect(bind)

    # Expand processing_jobs.job_type check constraint in meeting_analyzer schema.
    op.execute(
        f"ALTER TABLE {MEETING_ANALYZER_SCHEMA}.processing_jobs "
        f"DROP CONSTRAINT IF EXISTS {_JOB_TYPE_CHECK}"
    )
    op.execute(
        f"ALTER TABLE {MEETING_ANALYZER_SCHEMA}.processing_jobs "
        f"ADD CONSTRAINT {_JOB_TYPE_CHECK} CHECK (job_type IN {_JOB_TYPE_NEW})"
    )


def downgrade() -> None:
    # Non-destructive by default for shared production DB. Manual reviewed rollback only.
    print(
        "WARNING: downgrade for 0002_speaker_profiles is non-destructive and does not "
        "drop meeting_analyzer speaker tables or alter Task Manager/public tables."
    )
