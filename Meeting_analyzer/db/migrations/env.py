"""Alembic environment for TeamMeetingAI.

Reads the database URL from the environment (``TEAMMEETINGAI_DATABASE_URL`` or
``DATABASE_URL``) so secrets stay out of ``alembic.ini``.  Supports
``-x url=<sqlalchemy_url>`` for scripts and tests that target a scratch
database.

Meeting Analyzer uses schema ``meeting_analyzer`` and an independent Alembic
version table ``meeting_analyzer.alembic_version`` so it can coexist with
Task Manager's ``public.alembic_version`` in the same Supabase database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from db.base import MEETING_ANALYZER_SCHEMA, Base
from db.session import database_url_from_env

config = context.config

# ``alembic`` is normally run from the repository root; the search walks up
# from the CWD, so it also works from subdirectories.  Real environment
# variables always win over .env values.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - python-dotenv is a pinned dependency
    pass

# Allow callers (tests, scripts) to opt out of reconfiguring logging, which
# would otherwise clobber the application's logging setup.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
VERSION_TABLE = "alembic_version"
VERSION_TABLE_SCHEMA = MEETING_ANALYZER_SCHEMA


def _resolve_url() -> str:
    x_arg = context.get_x_argument(as_dictionary=True).get("url")
    if x_arg:
        return x_arg
    attr_url = config.attributes.get("database_url")
    if attr_url:
        return str(attr_url)
    return database_url_from_env()


def _ensure_version_schema(connection) -> None:
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {VERSION_TABLE_SCHEMA}"))


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
        version_table_schema=VERSION_TABLE_SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config_section = config.get_section(config.config_ini_section, {})
    config_section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        config_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _ensure_version_schema(connection)
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table=VERSION_TABLE,
            version_table_schema=VERSION_TABLE_SCHEMA,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
