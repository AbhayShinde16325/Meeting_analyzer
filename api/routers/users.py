"""User profiles (local identities; authentication is a later phase)."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from api import errors
from api.deps import ConnDep
from api.schemas import UserCreate, UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(conn: ConnDep) -> list[dict]:
    rows = (
        conn.execute(
            text("SELECT id, display_name, created_at FROM users ORDER BY display_name")
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@router.post("", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, conn: ConnDep) -> dict:
    existing = conn.execute(
        text("SELECT id FROM users WHERE display_name = :name"),
        {"name": payload.display_name},
    ).scalar_one_or_none()
    if existing is not None:
        raise errors.ApiError(409, "user_exists", "A profile with this name already exists.")
    row = (
        conn.execute(
            text(
                "INSERT INTO users (display_name) VALUES (:name) RETURNING id, "
                "display_name, created_at"
            ),
            {"name": payload.display_name},
        )
        .mappings()
        .one()
    )
    return dict(row)
