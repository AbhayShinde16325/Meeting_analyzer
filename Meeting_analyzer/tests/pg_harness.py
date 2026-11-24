"""PostgreSQL test harness.

The database layer tests need a real PostgreSQL server (Large Objects, partial
unique indexes and triggers cannot be exercised on SQLite).  The harness is
opt-in: set ``TEST_DATABASE_URL`` to a SQLAlchemy URL whose *server* the tests
may use, e.g.::

    set TEST_DATABASE_URL=postgresql+psycopg://tma@localhost:5433/postgres

A scratch database is dropped, re-created, migrated to head via Alembic, and
truncated between tests.  Without the variable the fixtures skip, so the
suite still runs everywhere else.
"""

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from db.base import Base

TEST_DB_NAME_PREFIX = "tma_test_"

_SAFE_DB_NAME = re.compile(r"[^a-z0-9_]")


def _scratch_db_name() -> str:
    suffix = _SAFE_DB_NAME.sub("", str(os.getpid()))
    return f"{TEST_DB_NAME_PREFIX}{suffix}"[:63]


def _normalize_test_url(url: str) -> str:
    """Allow psycopg v3 URLs in environments without psycopg2."""
    try:
        import psycopg2  # noqa: F401
        return url
    except Exception:
        return url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)


def _admin_engine() -> Engine:
    """Autocommit engine connected to the maintenance database for create/drop."""

    url = make_url(_normalize_test_url(os.environ["TEST_DATABASE_URL"]))
    admin_db = os.environ.get("TEST_DATABASE_ADMIN_DB", "postgres")
    connect_args = {}
    if url.drivername.endswith("+psycopg2"):
        connect_args["autocommit"] = True
    return create_engine(
        url.set(database=admin_db),
        future=True,
        isolation_level="AUTOCOMMIT",
        connect_args=connect_args,
    )


def create_scratch_database() -> str:
    """Drop and re-create the scratch test database; return its URL."""

    admin = _admin_engine()
    name = _scratch_db_name()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    url = make_url(_normalize_test_url(os.environ["TEST_DATABASE_URL"]))
    return url.set(database=name).render_as_string(hide_password=False)


def drop_scratch_database(database_url: str) -> None:
    admin = _admin_engine()
    name = make_url(database_url).database
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def migrate_to_head(database_url: str) -> None:
    """Run Alembic migrations to head against ``database_url``."""

    from alembic import command
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "db", "migrations"))
    config.attributes["configure_logger"] = False
    config.attributes["database_url"] = database_url
    os.environ["DATABASE_URL"] = database_url
    os.environ["TEAMMEETINGAI_DATABASE_URL"] = database_url
    command.upgrade(config, "head")


def truncate_all(engine: Engine) -> None:
    """Delete every row and every Large Object, keeping the schema in place."""

    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.schema}"."{table.name}" CASCADE'))
        # Test-only Task Manager tables in isolated scratch DB.
        for tbl in ("public.tasks", "public.employees"):
            try:
                with conn.begin_nested():
                    conn.execute(text(f"TRUNCATE TABLE {tbl} CASCADE"))
            except Exception:
                pass
        conn.execute(
            text(
                "SELECT lo_unlink(oid) FROM pg_largeobject_metadata "
                "WHERE oid NOT IN (SELECT lo_oid FROM meeting_analyzer.meeting_files)"
            )
        )


def lo_oid_count(engine: Engine) -> int:
    """Number of Large Objects currently stored (for leak assertions)."""

    with engine.connect() as conn:
        return int(
            conn.execute(text("SELECT COUNT(*) FROM pg_largeobject_metadata")).scalar_one()
        )
