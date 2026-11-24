"""Liveness and database connectivity."""

from __future__ import annotations


from fastapi import APIRouter
from sqlalchemy import text

from api.deps import ConnDep

router = APIRouter(tags=["health"])


@router.get("/health")
def health(conn: ConnDep) -> dict:
    """Liveness probe: pings the database and reports schema revision."""

    try:
        db_ok = conn.execute(text("SELECT 1")).scalar_one() == 1
        revision = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
    except Exception:
        db_ok, revision = False, None
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "schema": revision}
