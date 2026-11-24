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

from db.base import Base

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

    ``pool_pre_ping`` reconnects transparently after PostgreSQL restarts
    (which happens with Windows updates) instead of failing requests.
    """

    resolved = url or database_url_from_env()
    return create_engine(resolved, pool_pre_ping=True, future=True)


def _known_revisions() -> set[str]:
    """Revision IDs that this version of the code ships."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(config)
    return {rev.revision for rev in script.walk_revisions()}


def current_db_revision(conn) -> str | None:
    """Return the revision recorded in the database, if the table exists."""

    try:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
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
            "The database schema is missing. Run 'alembic upgrade head' with "
            "TEAMMEETINGAI_DATABASE_URL pointing at the server database."
        )
    if db_revision not in known:
        raise SchemaTooNewError(
            f"The database schema ({db_revision}) is newer than this build. "
            "Upgrade the application before starting the server."
        )
    return db_revision


class SchemaTooNewError(RuntimeError):
    """Raised when the database schema is newer than the running code."""
