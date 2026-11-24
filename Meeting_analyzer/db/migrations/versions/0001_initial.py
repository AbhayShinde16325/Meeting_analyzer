"""Initial schema for TeamMeetingAI (Meeting Analyzer).

Safety-first bootstrap for shared Supabase/PostgreSQL deployments:

* Creates schema ``meeting_analyzer`` if missing.
* Creates Meeting Analyzer tables only in ``meeting_analyzer``.
* Never drops/modifies Task Manager ``public`` tables.
* Preserves PostgreSQL Large Objects for meeting files; adds LO unlink trigger.
* Supports fresh install and legacy pre-schema installs where Meeting Analyzer
  tables already exist in ``public`` (detected; migration stops with guidance).

Downgrade is intentionally non-destructive for shared/production databases.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from db.base import MEETING_ANALYZER_SCHEMA, Base

# Import models so Base.metadata is fully populated.
import db.models  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_LEGACY_TABLES = [
    "users",
    "meetings",
    "meeting_files",
    "transcripts",
    "summaries",
    "action_items",
    "processing_jobs",
]

_TRIGGER_FN = f"{MEETING_ANALYZER_SCHEMA}.tma_unlink_meeting_file_lo"
_TRIGGER_NAME = "trg_meeting_files_unlink_lo"

_UNLINK_TRIGGER_SQL = f"""
CREATE OR REPLACE FUNCTION {_TRIGGER_FN}() RETURNS trigger AS $$
BEGIN
    PERFORM lo_unlink(OLD.lo_oid);
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

_DROP_TRIGGER_SQL = f"""
DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON {MEETING_ANALYZER_SCHEMA}.meeting_files;
DROP FUNCTION IF EXISTS {_TRIGGER_FN}();
"""


def _table_exists(inspector, table: str, schema: str | None) -> bool:
    try:
        return inspector.has_table(table, schema=schema)
    except Exception:
        return False


def _ensure_trigger() -> None:
    # Ensure trigger/function exists in meeting_analyzer schema.
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {MEETING_ANALYZER_SCHEMA}")
    op.execute(_UNLINK_TRIGGER_SQL)
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON {MEETING_ANALYZER_SCHEMA}.meeting_files")
    op.execute(
        f"CREATE TRIGGER {_TRIGGER_NAME} "
        f"BEFORE DELETE ON {MEETING_ANALYZER_SCHEMA}.meeting_files "
        f"FOR EACH ROW EXECUTE FUNCTION {_TRIGGER_FN}()"
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    op.execute(f"CREATE SCHEMA IF NOT EXISTS {MEETING_ANALYZER_SCHEMA}")
    inspector = inspect(bind)

    schema_tables = {
        t for t in _LEGACY_TABLES if _table_exists(inspector, t, MEETING_ANALYZER_SCHEMA)
    }
    public_tables = {
        t for t in _LEGACY_TABLES if _table_exists(inspector, t, "public")
    }

    # If legacy Meeting Analyzer tables exist in public and not in meeting_analyzer,
    # stop to avoid unsafe automatic data moves in shared Task Manager DB.
    if public_tables and not schema_tables:
        raise RuntimeError(
            "Detected legacy Meeting Analyzer tables in public schema "
            f"({sorted(public_tables)}). Automatic move is disabled for safety. "
            "Backup DB and run a reviewed data migration into meeting_analyzer schema "
            "before applying this revision."
        )

    # Fresh install / schema already present: create missing Meeting Analyzer tables
    # in meeting_analyzer schema only.
    meeting_tables = [tbl for tbl in Base.metadata.sorted_tables if tbl.schema == MEETING_ANALYZER_SCHEMA]
    Base.metadata.create_all(bind=bind, tables=meeting_tables, checkfirst=True)

    # Ensure LO unlink trigger exists even for already-migrated schema tables.
    if _table_exists(inspect(bind), "meeting_files", MEETING_ANALYZER_SCHEMA):
        _ensure_trigger()


def downgrade() -> None:
    # Non-destructive downgrade: shared production DB may contain Task Manager data.
    # Intentionally no DROP TABLE / DROP SCHEMA. Use a reviewed manual rollback plan.
    print(
        "WARNING: downgrade for 0001_initial is non-destructive and does not drop "
        "meeting_analyzer schema/tables. Manual reviewed rollback required if needed."
    )
