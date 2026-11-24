"""Deterministic search across titles, transcripts, summaries and items."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import text

from api.deps import ConnDep
from api.schemas import SearchHit

router = APIRouter(tags=["search"])


@router.get("/search", response_model=list[SearchHit])
def search(
    conn: ConnDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    kind: Literal["all", "transcript", "summary", "action_item"] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    """Substring search (ILIKE) over meeting content.

    Deliberately deterministic and dependency-free for the MVP; PostgreSQL FTS
    with a GIN index can replace the implementation later without changing
    this response shape.
    """

    pattern = f"%{q}%"
    hits: list[dict] = []
    if kind in ("all", "transcript"):
        rows = (
            conn.execute(
                text(
                    "SELECT m.id AS meeting_id, m.title, m.recorded_at, "
                    "substring(t.text, GREATEST(1, position(lower(:q) IN "
                    "lower(t.text)) - 80), 220) AS snippet "
                    "FROM meeting_analyzer.transcripts t JOIN meeting_analyzer.meetings m ON m.id = t.meeting_id "
                    "WHERE t.is_primary AND t.text ILIKE :p "
                    "ORDER BY m.recorded_at DESC LIMIT :limit"
                ),
                {"q": q, "p": pattern, "limit": limit},
            )
            .mappings()
            .all()
        )
        hits += [{"kind": "transcript", **row} for row in rows]
    if kind in ("all", "summary"):
        rows = (
            conn.execute(
                text(
                    "SELECT m.id AS meeting_id, m.title, m.recorded_at, "
                    "substring(s.overview, GREATEST(1, position(lower(:q) IN "
                    "lower(s.overview)) - 80), 220) AS snippet "
                    "FROM meeting_analyzer.summaries s JOIN meeting_analyzer.meetings m ON m.id = s.meeting_id "
                    "WHERE s.overview ILIKE :p OR s.payload::text ILIKE :p "
                    "ORDER BY m.recorded_at DESC LIMIT :limit"
                ),
                {"q": q, "p": pattern, "limit": limit},
            )
            .mappings()
            .all()
        )
        hits += [{"kind": "summary", **row} for row in rows]
    if kind in ("all", "action_item"):
        rows = (
            conn.execute(
                text(
                    "SELECT m.id AS meeting_id, m.title, m.recorded_at, "
                    "a.task || ' — ' || COALESCE(a.owner, '') AS snippet "
                    "FROM meeting_analyzer.action_items a JOIN meeting_analyzer.meetings m ON m.id = a.meeting_id "
                    "WHERE a.task ILIKE :p OR COALESCE(a.owner, '') ILIKE :p "
                    "ORDER BY m.recorded_at DESC LIMIT :limit"
                ),
                {"p": pattern, "limit": limit},
            )
            .mappings()
            .all()
        )
        hits += [{"kind": "action_item", **row} for row in rows]
    return hits[:limit]
