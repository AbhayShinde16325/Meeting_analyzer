"""Startup guard tests: refuse to run against a missing or newer schema."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.session import SchemaTooNewError, ensure_schema_current


def test_current_schema_is_accepted(pg_engine):
    revision = ensure_schema_current(pg_engine)
    assert revision == "0002_speaker_profiles"


def test_newer_schema_is_rejected(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '9999_future'"))
    try:
        with pytest.raises(SchemaTooNewError):
            ensure_schema_current(pg_engine)
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '0002_speaker_profiles'"))


def test_missing_version_table_is_rejected(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    try:
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            ensure_schema_current(pg_engine)
    finally:
        with pg_engine.begin() as conn:
            conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            conn.execute(
                text("INSERT INTO alembic_version VALUES ('0002_speaker_profiles')")
            )
