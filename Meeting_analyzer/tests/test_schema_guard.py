"""Startup guard tests: refuse to run against a missing or newer schema."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.session import SchemaTooNewError, ensure_schema_current

EXPECTED_REVISION = "0004_meeting_task_sync"


def test_current_schema_is_accepted(pg_engine):
    revision = ensure_schema_current(pg_engine)
    assert revision == EXPECTED_REVISION


def test_newer_schema_is_rejected(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE meeting_analyzer.alembic_version SET version_num = '9999_future'"))
    try:
        with pytest.raises(SchemaTooNewError):
            ensure_schema_current(pg_engine)
    finally:
        with pg_engine.begin() as conn:
            conn.execute(
                text("UPDATE meeting_analyzer.alembic_version SET version_num = :rev"),
                {"rev": EXPECTED_REVISION},
            )


def test_missing_version_table_is_rejected(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS meeting_analyzer.alembic_version"))
    try:
        with pytest.raises(RuntimeError, match="alembic"):
            ensure_schema_current(pg_engine)
    finally:
        # restore for other tests
        with pg_engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS meeting_analyzer"))
            conn.execute(
                text("CREATE TABLE IF NOT EXISTS meeting_analyzer.alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            conn.execute(text("DELETE FROM meeting_analyzer.alembic_version"))
            conn.execute(
                text("INSERT INTO meeting_analyzer.alembic_version (version_num) VALUES (:rev)"),
                {"rev": EXPECTED_REVISION},
            )
