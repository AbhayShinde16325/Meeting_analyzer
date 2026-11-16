"""System status: database health, storage usage, backups, recording flag."""

from __future__ import annotations

import os
import shutil

from fastapi import APIRouter, Header
from sqlalchemy import text

from api.deps import ConnDep, HOST_TOKEN_HEADER, is_host_request
from api.schemas import SystemStatus

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status", response_model=SystemStatus)
def status(
    conn: ConnDep,
    x_profile_id: str | None = Header(default=None),
    x_host_token: str | None = Header(default=None, alias=HOST_TOKEN_HEADER),
) -> dict:
    """Everything the dashboard footer and settings dialog need.

    ``can_record`` is true when this client may record (host machine or host
    tools); clients use it to enable or hide the recording UI.  ``last_backup``
    reflects the most recent ``backup_runs`` row (written by the nightly
    pg_dump script).
    """

    schema_version = conn.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()
    meeting_count = int(
        conn.execute(text("SELECT COUNT(*) FROM meetings")).scalar_one()
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
                "error FROM backup_runs ORDER BY started_at DESC LIMIT 1"
            )
        )
        .mappings()
        .one_or_none()
    )
    last_backup = dict(last_backup_row) if last_backup_row else None
    # Free space on the drive that hosts the server (approximate headroom
    # signal; PG may live on another volume in exotic setups).
    disk_free = shutil.disk_usage(os.path.abspath(os.sep)).free
    return {
        "schema_version": schema_version or "",
        "meeting_count": meeting_count,
        "db_size_bytes": db_size,
        "disk_free_bytes": disk_free,
        "last_backup": last_backup,
        # Recording happens on the host machine only (audio devices are local).
        "can_record": is_host_request(x_profile_id, x_host_token),
    }
