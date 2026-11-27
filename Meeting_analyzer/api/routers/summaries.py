"""Summaries and action items.

Summaries are always produced from the meeting's primary Roman-script
transcript; the stored row records which transcript it was built from.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import text

from api import errors
from api.deps import ConnDep
from api.schemas import ActionItemOut, ActionItemPatch, JobOut, SummaryOut
from config import load_settings
from ai.provider_factory import build_analysis_provider, ProviderConfigurationError

router = APIRouter(prefix="/meetings", tags=["summaries"])
action_router = APIRouter(prefix="/action-items", tags=["summaries"])


@router.get("/{meeting_id}/summary", response_model=SummaryOut | None)
def get_summary(meeting_id: uuid.UUID, conn: ConnDep) -> dict | None:
    row = (
        conn.execute(
            text(
                "SELECT id, meeting_id, transcript_id, overview, payload, model, "
                "created_at FROM meeting_analyzer.summaries WHERE meeting_id = :m"
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


@router.post("/{meeting_id}/analyse", response_model=JobOut, status_code=202)
def analyse(
    meeting_id: uuid.UUID,
    conn: ConnDep,
) -> dict:
    """(Re-)queue analysis of the primary transcript."""

    if conn.execute(
        text("SELECT 1 FROM meeting_analyzer.meetings WHERE id = :m"), {"m": meeting_id}
    ).scalar_one_or_none() is None:
        raise errors.not_found("meeting_not_found", "Meeting not found.")
    primary = conn.execute(
        text(
            "SELECT 1 FROM meeting_analyzer.transcripts WHERE meeting_id = :m AND is_primary = true"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if primary is None:
        raise errors.bad_request(
            "no_transcript", "Transcribe the meeting before requesting analysis."
        )
    settings = load_settings()
    try:
        provider = build_analysis_provider(settings)
    except ProviderConfigurationError as exc:
        raise errors.bad_request("analysis_not_configured", str(exc))
    except Exception as exc:
        raise errors.bad_request("analysis_not_configured", f"Analysis backend unavailable: {exc}")
    if provider is None:
        raise errors.bad_request(
            "analysis_not_configured",
            "No analysis backend configured. Set TEAMMEETINGAI_ANALYSIS_BACKEND=groq, TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS=1, TEAMMEETINGAI_GROQ_MODEL, and TEAMMEETINGAI_GROQ_API_KEY.",
        )
    job_fields = """
        id, meeting_id, job_type, status, progress, message, error, error_code, error_detail,
        provider, model, fallback_from, fallback_reason, attempt_count, max_attempts,
        next_run_at, heartbeat_at, cancel_requested_at, created_at, started_at, finished_at, updated_at
    """
    active = conn.execute(
        text(
            "SELECT 1 FROM meeting_analyzer.processing_jobs WHERE meeting_id = :m AND "
            "job_type = 'analysis' AND status IN ('queued', 'running', 'cancelling')"
        ),
        {"m": meeting_id},
    ).scalar_one_or_none()
    if active is not None:
        raise errors.conflict("job_active", "An analysis job is already active.")
    row = (
        conn.execute(
            text(
                f"""
                INSERT INTO meeting_analyzer.processing_jobs (
                    id, meeting_id, job_type, status, message, attempt_count, max_attempts, heartbeat_at, updated_at
                ) VALUES (gen_random_uuid(), :m, 'analysis', 'queued', 'Queued for analysis', 0, 3, now(), now())
                ON CONFLICT (meeting_id, job_type) WHERE status IN ('queued', 'running', 'cancelling')
                DO NOTHING RETURNING {job_fields}
                """
            ),
            {"m": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise errors.conflict("job_active", "An analysis job is already active.")
    return dict(row)


@router.get("/{meeting_id}/action-items", response_model=list[ActionItemOut])
def list_action_items(meeting_id: uuid.UUID, conn: ConnDep) -> list[dict]:
    # Prefer synced Task Manager tasks; fallback to legacy meeting_analyzer.action_items.
    has_sync = conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='meeting_analyzer' AND table_name='meeting_task_sync'"
    )).scalar()
    has_public_tasks = conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='tasks'"
    )).scalar()
    if has_sync and has_public_tasks:
        rows = conn.execute(text(
            """
            SELECT s.id, s.meeting_id,
                   t.task_title AS task,
                   COALESCE(t.assigned_to, s.owner_text) AS owner,
                   CASE
                     WHEN t.deadline_date IS NULL THEN NULL
                     WHEN t.deadline_time IS NULL THEN t.deadline_date::text
                     ELSE t.deadline_date::text || ' ' || to_char(t.deadline_time, 'HH24:MI')
                   END AS deadline,
                   t.task_description AS evidence,
                   CASE WHEN upper(COALESCE(t.status,'PENDING'))='COMPLETED' THEN 'done' ELSE 'open' END AS status,
                   COALESCE(s.action_item_position, 0) AS position
            FROM meeting_analyzer.meeting_task_sync s
            JOIN public.tasks t ON t.id = s.task_id
            WHERE s.meeting_id = :m
            ORDER BY s.action_item_position, s.created_at
            """
        ), {"m": meeting_id}).mappings().all()
        if rows:
            return [dict(r) for r in rows]
    rows = conn.execute(text(
        "SELECT id, meeting_id, task, owner, deadline, evidence, status, position "
        "FROM meeting_analyzer.action_items WHERE meeting_id = :m ORDER BY position, created_at"
    ), {"m": meeting_id}).mappings().all()
    return [dict(row) for row in rows]


@action_router.patch("/{item_id}", response_model=ActionItemOut)
def patch_action_item(item_id: uuid.UUID, payload: ActionItemPatch, conn: ConnDep) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise errors.bad_request("empty_patch", "Nothing to update.")

    # Synced mapping id takes precedence
    map_row = conn.execute(text(
        """SELECT s.id, s.meeting_id, s.task_id, s.action_item_position
           FROM meeting_analyzer.meeting_task_sync s WHERE s.id=:id"""
    ), {"id": item_id}).mappings().one_or_none()
    if map_row:
        task_updates = {}
        if "task" in fields: task_updates["task_title"] = fields["task"]
        if "owner" in fields: task_updates["assigned_to"] = fields["owner"]
        if "evidence" in fields: task_updates["task_description"] = fields["evidence"]
        if "status" in fields:
            task_updates["status"] = "COMPLETED" if fields["status"] == "done" else "PENDING"
            task_updates["progress"] = 100 if fields["status"] == "done" else 0
        if "deadline" in fields:
            # explicit parsing only; invalid/relative -> leave null
            from datetime import datetime as _dt
            d=t=None; v=str(fields["deadline"] or "").strip()
            for fmt in ("%Y-%m-%d %H:%M","%Y-%m-%dT%H:%M","%Y-%m-%d %H:%M:%S","%Y-%m-%dT%H:%M:%S","%Y-%m-%d"):
                try:
                    dt=_dt.strptime(v,fmt)
                    d=dt.date(); t=None if fmt=="%Y-%m-%d" else dt.time().replace(microsecond=0); break
                except Exception: pass
            task_updates["deadline_date"]=d; task_updates["deadline_time"]=t
        if task_updates:
            sets = ", ".join(f"{k}=:{k}" for k in task_updates)
            conn.execute(text(f"UPDATE public.tasks SET {sets} WHERE id=:tid"), {**task_updates, "tid": int(map_row["task_id"])})
        row = conn.execute(text(
            """SELECT s.id, s.meeting_id, t.task_title AS task, COALESCE(t.assigned_to,s.owner_text) AS owner,
                      CASE WHEN t.deadline_date IS NULL THEN NULL WHEN t.deadline_time IS NULL THEN t.deadline_date::text
                           ELSE t.deadline_date::text || ' ' || to_char(t.deadline_time,'HH24:MI') END AS deadline,
                      t.task_description AS evidence,
                      CASE WHEN upper(COALESCE(t.status,'PENDING'))='COMPLETED' THEN 'done' ELSE 'open' END AS status,
                      COALESCE(s.action_item_position,0) AS position
               FROM meeting_analyzer.meeting_task_sync s JOIN public.tasks t ON t.id=s.task_id WHERE s.id=:id"""
        ), {"id": item_id}).mappings().one_or_none()
        if row is None:
            raise errors.not_found("item_not_found", "Action item not found.")
        return dict(row)

    assignments = ", ".join(f"{key} = :{key}" for key in fields)
    row = conn.execute(text(
        f"UPDATE meeting_analyzer.action_items SET {assignments} WHERE id = :id RETURNING "
        "id, meeting_id, task, owner, deadline, evidence, status, position"
    ), {**fields, "id": item_id}).mappings().one_or_none()
    if row is None:
        raise errors.not_found("item_not_found", "Action item not found.")
    return dict(row)
