"""Shared FastAPI dependencies.

Authentication is currently a simple profile selection model: clients send
``X-Profile-Id`` for attribution. Host-token gating has been removed so any
selected profile can record/manage meetings in this deployment.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from api import errors


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_speaker_profiler(request: Request):
    """The voice-profile service wired at startup (always present, may be disabled)."""

    profiler = getattr(request.app.state, "speaker_profiler", None)
    if profiler is None:
        raise errors.ApiError(
            503, "profiling_unavailable", "Voice profiling is not configured."
        )
    return profiler


ProfilerDep = Annotated[Any, Depends(get_speaker_profiler)]


def get_conn(
    engine: Annotated[Engine, Depends(get_engine)],
) -> Iterator[Connection]:
    """One database connection per request; committed on success."""

    conn = engine.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_current_profile(
    conn: Annotated[Connection, Depends(get_conn)],
    x_profile_id: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Resolve the acting profile from X-Profile-Id."""

    if not x_profile_id:
        return {"id": None, "display_name": "anonymous", "is_host": True}
    try:
        profile_id = uuid.UUID(x_profile_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_profile", "message": "X-Profile-Id is not a valid id."},
        ) from None
    row = (
        conn.execute(
            text("SELECT id, display_name FROM meeting_analyzer.users WHERE id = :id"),
            {"id": profile_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "profile_not_found", "message": "Unknown profile."},
        )
    return {"id": row["id"], "display_name": row["display_name"], "is_host": True}


ProfileDep = Annotated[dict[str, Any], Depends(get_current_profile)]
ConnDep = Annotated[Connection, Depends(get_conn)]
