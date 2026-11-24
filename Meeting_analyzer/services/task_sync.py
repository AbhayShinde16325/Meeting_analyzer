"""Sync extracted meeting action items to Task Manager public.tasks.

Idempotent, atomic per extracted item. MA owns mapping table; TM owns tasks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


class TaskSyncError(RuntimeError):
    pass


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def extraction_key(transcript_id: str | None, position: int, item: dict[str, Any]) -> str:
    base = json.dumps({
        "transcript_id": str(transcript_id) if transcript_id else None,
        "position": position,
        "task": _norm(item.get("task")),
        "owner": _norm(item.get("owner")),
        "deadline": _norm(item.get("deadline")),
        "evidence": _norm(item.get("evidence")),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _table_exists(conn: Connection, schema: str, table: str) -> bool:
    row = conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=:s AND table_name=:t"
    ), {"s": schema, "t": table}).scalar()
    return bool(row)


def _resolve_employee_id(conn: Connection, owner: str | None) -> int | None:
    name = str(owner or "").strip()
    if not name or not _table_exists(conn, "public", "employees"):
        return None
    rows = conn.execute(text(
        """
        SELECT id FROM public.employees
        WHERE is_active = true AND lower(name) = lower(:name)
        ORDER BY id LIMIT 2
        """
    ), {"name": name}).mappings().all()
    if len(rows) == 1:
        return int(rows[0]["id"])
    return None


def _resolve_creator_employee_id(conn: Connection, meeting_id: str) -> int | None:
    if not _table_exists(conn, "public", "employees"):
        return None
    cols = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema='meeting_analyzer' AND table_name='users'")).all()}
    sel_email = ", u.email" if "email" in cols else ", NULL::text AS email"
    row = conn.execute(text(
        f"""
        SELECT u.display_name{sel_email}
        FROM meeting_analyzer.meetings m
        JOIN meeting_analyzer.users u ON u.id = m.user_id
        WHERE m.id = :m
        """
    ), {"m": meeting_id}).mappings().one_or_none()
    if not row:
        return None
    email = str(row.get("email") or "").strip()
    name = str(row.get("display_name") or "").strip()
    if email:
        matches = conn.execute(text(
            "SELECT id FROM public.employees WHERE is_active=true AND lower(email)=lower(:email) ORDER BY id LIMIT 2"
        ), {"email": email}).mappings().all()
        if len(matches) == 1:
            return int(matches[0]["id"])
    if name:
        matches = conn.execute(text(
            "SELECT id FROM public.employees WHERE is_active=true AND lower(name)=lower(:name) ORDER BY id LIMIT 2"
        ), {"name": name}).mappings().all()
        if len(matches) == 1:
            return int(matches[0]["id"])
    return None


def _parse_deadline(deadline: str | None):
    """Parse explicit date/time only; never invent. Returns (date, time)."""
    value = str(deadline or "").strip()
    if not value:
        return None, None
    # Accept ISO-like explicit forms only for reliability.
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d":
                return dt.date(), None
            return dt.date(), dt.time().replace(microsecond=0)
        except Exception:
            pass
    # YYYY-MM-DD HH:MM:SS
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.date(), dt.time().replace(microsecond=0)
        except Exception:
            pass
    return None, None


def sync_action_items_to_tasks(conn: Connection, *, meeting_id: str, transcript_id: str | None, action_items: list[dict[str, Any]]) -> dict[str, Any]:
    if not _table_exists(conn, "meeting_analyzer", "meeting_task_sync"):
        raise TaskSyncError("meeting_analyzer.meeting_task_sync is missing; run migrations")
    if not _table_exists(conn, "public", "tasks"):
        raise TaskSyncError("public.tasks is missing; cannot sync meeting action items")

    created = 0
    existing = 0
    errors: list[str] = []
    creator_employee_id = _resolve_creator_employee_id(conn, meeting_id)
    now = datetime.now(timezone.utc)

    for pos, item in enumerate(action_items):
        key = extraction_key(transcript_id, pos, item)
        found = conn.execute(text(
            "SELECT task_id FROM meeting_analyzer.meeting_task_sync WHERE meeting_id=:m AND extraction_key=:k"
        ), {"m": meeting_id, "k": key}).scalar()
        if found is not None:
            existing += 1
            continue

        owner = str(item.get("owner") or "").strip() or None
        assigned_to_employee_id = _resolve_employee_id(conn, owner)
        deadline_date, deadline_time = _parse_deadline(item.get("deadline"))
        title = str(item.get("task") or "").strip() or "Follow-up action"
        evidence = str(item.get("evidence") or "").strip()
        description = evidence or title

        try:
            with conn.begin_nested():
                task_id = conn.execute(text(
                    """
                    INSERT INTO public.tasks (
                        task_title, task_description, assigned_to,
                        created_by_employee_id, assigned_to_employee_id,
                        assigned_date, assigned_time,
                        deadline_date, deadline_time,
                        status, progress
                    ) VALUES (
                        :title, :description, :assigned_to,
                        :created_by_employee_id, :assigned_to_employee_id,
                        :assigned_date, :assigned_time,
                        :deadline_date, :deadline_time,
                        'PENDING', 0
                    ) RETURNING id
                    """
                ), {
                    "title": title,
                    "description": description,
                    "assigned_to": owner,
                    "created_by_employee_id": creator_employee_id,
                    "assigned_to_employee_id": assigned_to_employee_id,
                    "assigned_date": now.date(),
                    "assigned_time": now.time().replace(microsecond=0),
                    "deadline_date": deadline_date,
                    "deadline_time": deadline_time,
                }).scalar_one()
                conn.execute(text(
                    """
                    INSERT INTO meeting_analyzer.meeting_task_sync (
                        id, meeting_id, transcript_id, task_id, extraction_key, action_item_position, owner_text
                    ) VALUES (
                        gen_random_uuid(), :m, :t, :task_id, :k, :pos, :owner
                    )
                    ON CONFLICT (meeting_id, extraction_key) DO NOTHING
                    """
                ), {
                    "m": meeting_id, "t": transcript_id, "task_id": int(task_id),
                    "k": key, "pos": pos, "owner": owner,
                })
                created += 1
        except Exception as exc:
            logger.exception("Task sync failed for meeting %s item %s", meeting_id, pos)
            errors.append(f"item {pos}: {exc}")
            raise TaskSyncError(f"Failed to sync action item {pos}: {exc}") from exc

    return {"created": created, "existing": existing, "count": len(action_items)}
