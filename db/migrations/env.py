"""Alembic environment for TeamMeetingAI.

Reads the database URL from the environment (``TEAMMEETINGAI_DATABASE_URL`` or
``DATABASE_URL``) so secrets stay out of ``alembic.ini``.  Supports
``-x url=<sqlalchemy_url>`` for scripts and tests that target a scratch
database.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from db.base import Base
from db.session import database_url_from_env

config = context.config

# Allow callers (tests, scripts) to opt out of reconfiguring logging, which
# would otherwise clobber the application's logging setup.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    x_arg = context.get_x_argument(as_dictionary=True).get("url")
    if x_arg:
        return x_arg
    return database_url_from_env()


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
