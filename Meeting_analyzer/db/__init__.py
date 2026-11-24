"""PostgreSQL persistence layer for TeamMeetingAI.

This package is the only place that talks to PostgreSQL directly.  The FastAPI
layer (``api/``) wraps it; UI clients never touch the database.
"""

from db.base import Base
from db.session import (
    DEFAULT_DATABASE_URL,
    SchemaTooNewError,
    build_engine,
    ensure_schema_current,
)

__all__ = [
    "Base",
    "DEFAULT_DATABASE_URL",
    "SchemaTooNewError",
    "build_engine",
    "ensure_schema_current",
]
