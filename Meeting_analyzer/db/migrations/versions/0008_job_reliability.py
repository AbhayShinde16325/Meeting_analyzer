"""Add job reliability fields: heartbeat, retries, cancelling, structured errors.

Schema-aware for `meeting_analyzer`. Additive only - no data deletion or destructive
column removal. Existing rows remain valid with defaults.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from db.base import MEETING_ANALYZER_SCHEMA

revision = "0008_job_reliability"
down_revision = "0007_matching_diagnostics"
branch_labels = None
depends_on = None

S = MEETING_ANALYZER_SCHEMA
TABLE = "processing_jobs"
STATUS_ALLOWED = "('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled')"
ACTIVE_STATUSES = "('queued', 'running', 'cancelling')"


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

    columns = {
        "heartbeat_at": sa.DateTime(timezone=True),
        "attempt_count": sa.Integer(),
        "max_attempts": sa.Integer(),
        "next_run_at": sa.DateTime(timezone=True),
        "cancel_requested_at": sa.DateTime(timezone=True),
        "error_code": sa.String(64),
        "error_detail": sa.dialects.postgresql.JSONB(),
    }
    for name, col_type in columns.items():
        if not _has_column(insp, TABLE, name):
            op.add_column(TABLE, sa.Column(name, col_type, nullable=True), schema=S)
            insp = inspect(bind)

    # Defaults/backfill for existing rows. Additive and idempotent.
    op.execute(
        f"""
        UPDATE {S}.{TABLE}
        SET attempt_count = COALESCE(attempt_count, CASE WHEN status IN ('running', 'succeeded', 'failed', 'cancelled') THEN 1 ELSE 0 END),
            max_attempts = COALESCE(max_attempts, 3),
            heartbeat_at = COALESCE(heartbeat_at, updated_at, started_at, created_at)
        WHERE attempt_count IS NULL OR max_attempts IS NULL OR heartbeat_at IS NULL
        """
    )
    op.execute(
        f"ALTER TABLE {S}.{TABLE} ALTER COLUMN attempt_count SET DEFAULT 0"
    )
    op.execute(
        f"ALTER TABLE {S}.{TABLE} ALTER COLUMN max_attempts SET DEFAULT 3"
    )
    op.execute(
        f"ALTER TABLE {S}.{TABLE} ALTER COLUMN attempt_count SET NOT NULL"
    )
    op.execute(
        f"ALTER TABLE {S}.{TABLE} ALTER COLUMN max_attempts SET NOT NULL"
    )

    # Expand status CHECK constraint to allow 'cancelling'.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE c.conname = 'status_allowed' AND n.nspname = '{S}' AND t.relname = '{TABLE}'
            ) THEN
                ALTER TABLE {S}.{TABLE} DROP CONSTRAINT status_allowed;
            END IF;
        END $$;
        """
    )
    op.execute(
        f"ALTER TABLE {S}.{TABLE} ADD CONSTRAINT status_allowed CHECK (status IN {STATUS_ALLOWED})"
    )

    # Active-job uniqueness must treat 'cancelling' as active until worker confirms terminal state.
    if _has_index(insp, TABLE, "uq_processing_jobs_active"):
        op.drop_index("uq_processing_jobs_active", table_name=TABLE, schema=S)
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_processing_jobs_active
        ON {S}.{TABLE} (meeting_id, job_type)
        WHERE status IN {ACTIVE_STATUSES}
        """
    )

    # Helpful lookup for stale/retry sweeps.
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_processing_jobs_status_heartbeat
        ON {S}.{TABLE} (status, heartbeat_at)
        """
    )
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS ix_processing_jobs_status_next_run
        ON {S}.{TABLE} (status, next_run_at)
        """
    )


def downgrade() -> None:
    print(
        "WARNING: downgrade for 0008_job_reliability is non-destructive and does not "
        "remove added processing_jobs reliability columns/constraints. Manual reviewed rollback required."
    )
