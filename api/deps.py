"""Shared FastAPI dependencies.

``get_current_profile`` is the future authentication seam: today it resolves a
profile from the ``X-Profile-Id`` header (trusted-LAN mode); when real auth
arrives (e.g. JWT) only this dependency changes - routes keep working.
"""

from __future__ import annotations

import os
import secrets
import uuid
from typing import Annotated, Any, Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from api import errors

# Shared secret proving a request comes from the meeting host machine.  The
# host PC (which records and manages meetings) and the server share this value
# through ``TEAMMEETINGAI_HOST_TOKEN`` in their environment / .env file.  When
# it is not configured, the deployment is single-host and requests without a
# profile header (scripts, host tools) count as the host.


HOST_TOKEN_ENV = "TEAMMEETINGAI_HOST_TOKEN"
HOST_TOKEN_HEADER = "X-Host-Token"


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


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
    """Resolve the acting profile.

    Returns the host profile (a synthetic user) when no header is present so
    host-side tools work without configuration; otherwise validates the
    referenced user exists.
    """

    if not x_profile_id:
        return {"id": None, "display_name": "host", "is_host": True}
    try:
        profile_id = uuid.UUID(x_profile_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_profile", "message": "X-Profile-Id is not a valid id."},
        ) from None
    row = (
        conn.execute(
            text("SELECT id, display_name FROM users WHERE id = :id"),
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
    return {"id": row["id"], "display_name": row["display_name"], "is_host": False}


ProfileDep = Annotated[dict[str, Any], Depends(get_current_profile)]
ConnDep = Annotated[Connection, Depends(get_conn)]


def is_host_request(
    x_profile_id: str | None, x_host_token: str | None
) -> bool:
    """Decide whether a request may perform host-only operations.

    With ``TEAMMEETINGAI_HOST_TOKEN`` configured, possession of the matching
    ``X-Host-Token`` header is the only proof of host rights (the host client
    also carries a profile header for attribution).  Without the token the
    deployment is single-host and header-less requests are the host.
    """

    token = os.environ.get(HOST_TOKEN_ENV, "").strip()
    if token:
        return bool(x_host_token) and secrets.compare_digest(x_host_token, token)
    return not x_profile_id


def require_host(
    x_profile_id: Annotated[str | None, Header()] = None,
    x_host_token: Annotated[str | None, Header()] = None,
) -> None:
    """Gate mutating/privileged operations to the host machine.

    Recording, deletion, job control and attachment management are host-only;
    with the token absent (host tools, TestClient) this passes when no profile
    header is present.  When authentication is added (e.g. JWT), only this
    dependency changes - routes keep working.
    """

    if not is_host_request(x_profile_id, x_host_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "host_only",
                "message": "This operation may only be performed on the meeting host.",
            },
        )
