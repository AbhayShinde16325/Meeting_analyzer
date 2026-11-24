"""Engine construction and schema-version guard.

The database URL comes from the environment (``TEAMMEETINGAI_DATABASE_URL`` or
``DATABASE_URL``, normally defined in ``.env``).  It is never hard-coded and
never logged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from db.base import MEETING_ANALYZER_SCHEMA

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost/teammeetingai"

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def database_url_from_env() -> str:
    """Return the configured database URL, or a sane default."""

    for name in ("TEAMMEETINGAI_DATABASE_URL", "DATABASE_URL"):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return raw.strip()
    return DEFAULT_DATABASE_URL


def build_engine(url: str | None = None) -> Engine:
    """Create a connection engine for the server process.

    ``pool_pre_ping`` reconnects transparently after PostgreSQL restarts.
    ``search_path`` is pinned to ``meeting_analyzer, public`` so Meeting
    Analyzer SQL targets its own schema while preserving access to shared
    extensions/catalogs. This engine must only be used by Meeting Analyzer.
    """

    resolved = url or database_url_from_env()
    return create_engine(
        resolved,
        pool_pre_ping=True,
        future=True,
        connect_args={"options": f"-csearch_path={MEETING_ANALYZER_SCHEMA},public"},
    )


def _known_revisions() -> set[str]:
    """Revision IDs that this version of the code ships."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(config)
    return {rev.revision for rev in script.walk_revisions()}


def _alembic_version_table(conn) -> str | None:
    """Locate Meeting Analyzer's alembic version table.

    Prefer ``meeting_analyzer.alembic_version``. Fallback to
    ``public.alembic_version`` only when its revision is a known Meeting
    Analyzer revision (legacy pre-schema install) to avoid touching Task
    Manager's independent Alembic history in the same database.
    """

    for schema in (MEETING_ANALYZER_SCHEMA, "public"):
        try:
            row = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_name = 'alembic_version'"
                ),
                {"schema": schema},
            ).first()
        except SQLAlchemyError:
            continue
        if row is None:
            continue
        table = f"{schema}.alembic_version"
        if schema == MEETING_ANALYZER_SCHEMA:
            return table
        try:
            rev = conn.execute(text(f"SELECT version_num FROM {table}")).scalar()
        except SQLAlchemyError:
            continue
        if rev and str(rev) in _known_revisions():
            return table
    return None


def current_db_revision(conn) -> str | None:
    """Return the Meeting Analyzer revision recorded in the database."""

    table = _alembic_version_table(conn)
    if table is None:
        return None
    try:
        row = conn.execute(text(f"SELECT version_num FROM {table}")).first()
    except SQLAlchemyError:
        return None
    if row is None:
        return None
    return str(row[0])


def ensure_schema_current(engine: Engine) -> str:
    """Verify the database schema version matches this code.

    Forward-only migrations mean an *older* database needs ``alembic upgrade
    head`` (report that); a *newer* database means the code is stale and the
    server must refuse to start rather than corrupt data.
    """

    known = _known_revisions()
    with engine.connect() as conn:
        db_revision = current_db_revision(conn)
    if db_revision is None:
        raise RuntimeError(
            "The Meeting Analyzer database schema is missing. Run "
            "'alembic -c Meeting_analyzer/alembic.ini upgrade head' with "
            "DATABASE_URL/TEAMMEETINGAI_DATABASE_URL pointing at the target database."
        )
    if db_revision not in known:
        raise SchemaTooNewError(
            f"The database schema ({db_revision}) is newer than this build. "
            "Upgrade the application before starting the server."
        )
    return db_revision


class SchemaTooNewError(RuntimeError):
    """Raised when the database schema is newer than the running code."""
