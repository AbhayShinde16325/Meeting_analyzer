"""System status: database health, storage usage, backups, recording flag."""

from __future__ import annotations

import os
import shutil

from fastapi import APIRouter
from sqlalchemy import text

from api.deps import ConnDep
from api.schemas import SystemStatus

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status", response_model=SystemStatus)
def status(conn: ConnDep) -> dict:
    """Everything the dashboard footer and settings dialog need."""

    schema_version = conn.execute(
        text("SELECT version_num FROM meeting_analyzer.alembic_version")
    ).scalar_one_or_none()
    meeting_count = int(
        conn.execute(text("SELECT COUNT(*) FROM meeting_analyzer.meetings")).scalar_one()
    )
    db_size = int(
        conn.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar_one()
    )
    last_backup_row = (
        conn.execute(
            text(
                "SELECT started_at, finished_at, status, output_path, size_bytes, "
                "error FROM meeting_analyzer.backup_runs ORDER BY started_at DESC LIMIT 1"
            )
        )
        .mappings()
        .one_or_none()
    )
    last_backup = dict(last_backup_row) if last_backup_row else None
    disk_free = shutil.disk_usage(os.path.abspath(os.sep)).free
    return {
        "schema_version": schema_version or "",
        "meeting_count": meeting_count,
        "db_size_bytes": db_size,
        "disk_free_bytes": disk_free,
        "last_backup": last_backup,
        "can_record": True,
    }
