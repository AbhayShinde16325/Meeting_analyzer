"""Initial schema: users, meetings, meeting_files (LOs), transcripts,
summaries, action items, processing jobs, backup runs.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-15

Large Objects are created at runtime through db/lo.py (``lo_create``/``lo_put``
/``lo_get``); the schema stores OIDs only.  The BEFORE DELETE trigger on
``meeting_files`` unlinks each LO in the same transaction as the row delete so
hard delete never leaks Large Object storage.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

import db.models  # noqa: F401  (populates Base.metadata with all tables)
from db.base import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_UNLINK_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION tma_unlink_meeting_file_lo() RETURNS trigger AS $$
BEGIN
    PERFORM lo_unlink(OLD.lo_oid);
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

_DROP_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_meeting_files_unlink_lo ON meeting_files;
DROP FUNCTION IF EXISTS tma_unlink_meeting_file_lo();
"""


def _create_trigger() -> None:
    op.execute(_UNLINK_TRIGGER_SQL)
    op.execute(
        "CREATE TRIGGER trg_meeting_files_unlink_lo "
        "BEFORE DELETE ON meeting_files "
        "FOR EACH ROW EXECUTE FUNCTION tma_unlink_meeting_file_lo()"
    )


def upgrade() -> None:
    bind = op.get_bind()
    # Created from the shared declarative metadata so the schema can never
    # drift from db/models.py.  Partial indexes, CHECK constraints and FKs all
    # carry through.
    Base.metadata.create_all(bind)
    _create_trigger()


def downgrade() -> None:
    op.execute(_DROP_TRIGGER_SQL)
    bind = op.get_bind()
    Base.metadata.drop_all(bind)
