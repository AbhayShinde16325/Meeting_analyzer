"""Declarative base with a stable constraint-naming convention.

The naming convention keeps auto-generated constraint names predictable, which
matters for Alembic migrations and for deterministic ``DROP``/``CASCADE``
behaviour in tests.

All Meeting Analyzer tables live in the dedicated ``meeting_analyzer`` schema
to coexist safely with existing Task Manager tables in ``public`` in the same
Supabase/PostgreSQL database.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

MEETING_ANALYZER_SCHEMA = "meeting_analyzer"

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(schema=MEETING_ANALYZER_SCHEMA, naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata
