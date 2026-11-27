from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from services.pipeline import PipelineError, TranscriptionPipeline
from services.task_sync import extraction_key, sync_action_items_to_tasks
from ai.summarization import MeetingSummarizer
from ai.transcription import TranscriptionResult, TranscriptSegment


def _ensure_public_task_tables(conn):
    # Minimal Task Manager-compatible tables in isolated scratch DB only.
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS public"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS public.employees (
            id serial PRIMARY KEY,
            name varchar NOT NULL,
            email varchar,
            is_active boolean NOT NULL DEFAULT true
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS public.tasks (
            id serial PRIMARY KEY,
            task_title varchar NOT NULL,
            task_description varchar,
            assigned_to varchar,
            created_by_employee_id integer,
            assigned_to_employee_id integer,
            assigned_date date,
            assigned_time time,
            deadline_date date,
            deadline_time time,
            reminder_date date,
            reminder_time time,
            status varchar DEFAULT 'PENDING',
            progress integer DEFAULT 0
        )
    """))


def _mk_meeting(conn):
    uid = conn.execute(text("INSERT INTO meeting_analyzer.users(id,display_name) VALUES (gen_random_uuid(),:n) RETURNING id"), {"n":"Alice"}).scalar_one()
    mid = conn.execute(text("INSERT INTO meeting_analyzer.meetings(id,user_id,title,status) VALUES (gen_random_uuid(),:u,'M','transcribed') RETURNING id"), {"u":uid}).scalar_one()
    tid = conn.execute(text("""
        INSERT INTO meeting_analyzer.transcripts(id,meeting_id,provider,model,text,is_primary)
        VALUES (gen_random_uuid(),:m,'test','m','hello',true) RETURNING id
    """), {"m":mid}).scalar_one()
    return str(mid), str(tid)


class _FakeSummarizer:
    model_name = "fake"
    def __init__(self, result):
        self._result = result
    def analyze(self, *args, **kwargs):
        if isinstance(self._result, Exception): raise self._result
        return self._result


class _FakeTranscription:
    provider_name = "fake"
    model_name = "fake"
    transcript_mode = "text"
    def transcribe(self, *args, **kwargs):
        return TranscriptionResult(text="hi", segments=[TranscriptSegment(0,1,"hi").to_dict()], language="en", provider="fake", model="fake")


def test_sync_creates_tasks_and_is_idempotent(pg):
    _ensure_public_task_tables(pg)
    pg.execute(text("INSERT INTO public.employees(name,email,is_active) VALUES ('Rahul','r@example.com',true),('Priya','p@example.com',true)"))
    mid, tid = _mk_meeting(pg)
    items = [
        {"task":"Prepare migration docs","owner":"Rahul","deadline":"2026-12-01","evidence":"Rahul will prepare..."},
        {"task":"Review API integration","owner":"Priya","deadline":None,"evidence":"Priya will review..."},
    ]
    r1 = sync_action_items_to_tasks(pg, meeting_id=mid, transcript_id=tid, action_items=items)
    pg.commit()
    r2 = sync_action_items_to_tasks(pg, meeting_id=mid, transcript_id=tid, action_items=items)
    pg.commit()
    assert r1["created"] == 2
    assert r2["created"] == 0 and r2["existing"] == 2
    tasks = pg.execute(text("SELECT task_title, assigned_to, assigned_to_employee_id, status, progress, deadline_date::text FROM public.tasks ORDER BY id")).mappings().all()
    assert len(tasks) == 2
    assert tasks[0]["assigned_to_employee_id"] is not None
    assert all(t["status"] == "PENDING" and t["progress"] == 0 for t in tasks)


def test_sync_handles_unknown_ambiguous_and_missing_deadline(pg):
    _ensure_public_task_tables(pg)
    pg.execute(text("INSERT INTO public.employees(name,email,is_active) VALUES ('Alex','a1@example.com',true),('Alex','a2@example.com',true)"))
    mid, tid = _mk_meeting(pg)
    items = [
        {"task":"Do thing","owner":"Unknown Person","deadline":"next Friday","evidence":"..."},
        {"task":"Do other","owner":"Alex","deadline":None,"evidence":"..."},
    ]
    sync_action_items_to_tasks(pg, meeting_id=mid, transcript_id=tid, action_items=items)
    pg.commit()
    rows = pg.execute(text("SELECT assigned_to, assigned_to_employee_id, deadline_date, deadline_time FROM public.tasks ORDER BY id")).mappings().all()
    assert rows[0]["assigned_to"] == "Unknown Person"
    assert rows[0]["assigned_to_employee_id"] is None
    assert rows[0]["deadline_date"] is None
    assert rows[1]["assigned_to"] == "Alex"
    assert rows[1]["assigned_to_employee_id"] is None  # ambiguous


def test_pipeline_sync_failure_preserves_summary(pg, pg_engine):
    _ensure_public_task_tables(pg)
    mid, tid = _mk_meeting(pg)
    # Break public.tasks to force sync failure after summary save
    pg.execute(text("DROP TABLE public.tasks"))
    pg.commit()
    summarizer = _FakeSummarizer({"overview":"ok","key_points":[],"decisions":[],"action_items":[{"task":"T1","owner":None,"deadline":None,"evidence":"E"}],"open_questions":[],"risks_or_blockers":[],"follow_ups":[]})
    pipe = TranscriptionPipeline(pg_engine, _FakeTranscription(), summarizer)
    job_id = str(uuid.uuid4())
    pg.execute(text("INSERT INTO meeting_analyzer.processing_jobs(id,meeting_id,job_type,status,message) VALUES (:id,:m,'analysis','queued','q')"), {"id":job_id,"m":mid})
    pg.commit()
    pipe._run_analysis(job_id, mid)
    job = pg.execute(text("SELECT status,error FROM meeting_analyzer.processing_jobs WHERE id=:id"), {"id":job_id}).mappings().one()
    summary = pg.execute(text("SELECT overview FROM meeting_analyzer.summaries WHERE meeting_id=:m"), {"m":mid}).mappings().one_or_none()
    assert job["status"] == "failed"
    err = (job["error"] or "").lower()
    assert "task sync" in err or "cannot sync" in err or "public.tasks" in err
    assert summary is not None and summary["overview"] == "ok"
