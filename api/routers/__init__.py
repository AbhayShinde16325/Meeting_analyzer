"""API routers, grouped by resource."""

from __future__ import annotations

from api.routers import (
    files,
    health,
    meetings,
    search,
    summaries,
    system,
    transcripts,
    users,
)

__all__ = ["files", "health", "meetings", "search", "summaries", "system", "transcripts", "users"]
