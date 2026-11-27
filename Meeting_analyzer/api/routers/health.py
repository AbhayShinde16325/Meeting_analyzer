"""Liveness and database connectivity."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api.deps import ConnDep

router = APIRouter(tags=["health"])

STALE_JOB_SECONDS = 15 * 60


@router.get("/health")
def health(conn: ConnDep, request: Request):
    """Liveness/readiness probe: reports DB writability and worker state."""

    db_ok = False
    db_read_only = True
    schema = None
    db_error = None
    stale_jobs = 0
    try:
        db_ok = conn.execute(text("SELECT 1")).scalar_one() == 1
        schema = conn.execute(
            text("SELECT version_num FROM meeting_analyzer.alembic_version")
        ).scalar_one_or_none()
        try:
            tx_ro = conn.execute(text("SHOW transaction_read_only")).scalar_one()
            def_ro = conn.execute(text("SHOW default_transaction_read_only")).scalar_one()
            db_read_only = str(tx_ro).lower() == "on" or str(def_ro).lower() == "on"
        except Exception:
            # If we cannot determine read-only state, treat as read-only/unhealthy.
            db_read_only = True
        try:
            stale_jobs = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM meeting_analyzer.processing_jobs
                        WHERE status IN ('running', 'cancelling')
                          AND COALESCE(heartbeat_at, updated_at, started_at, created_at) < now() - (:seconds * interval '1 second')
                        """
                    ),
                    {"seconds": STALE_JOB_SECONDS},
                ).scalar_one()
                or 0
            )
        except Exception:
            stale_jobs = 0
    except Exception as exc:
        db_ok = False
        db_read_only = True
        db_error = str(exc.__class__.__name__)

    runner = getattr(request.app.state, "job_runner", None)
    worker = runner.health() if runner is not None and hasattr(runner, "health") else None
    worker_stalled = bool(worker and worker.get("stalled"))
    worker_running = bool(worker and worker.get("running"))
    worker_read_only = bool(worker and worker.get("db_read_only"))

    unhealthy_reasons: list[str] = []
    if not db_ok:
        unhealthy_reasons.append("database_unreachable")
    if db_read_only:
        unhealthy_reasons.append("database_read_only")
    if worker is None:
        unhealthy_reasons.append("worker_missing")
    elif not worker_running:
        unhealthy_reasons.append("worker_not_running")
    if worker_stalled:
        unhealthy_reasons.append("worker_stalled")
    if stale_jobs > 0:
        unhealthy_reasons.append("stale_jobs_detected")
    if worker_read_only and "database_read_only" not in unhealthy_reasons:
        unhealthy_reasons.append("worker_detected_database_read_only")

    status = "ok" if not unhealthy_reasons else "unhealthy"
    payload = {
        "status": status,
        "database": db_ok,
        "database_read_only": db_read_only,
        "schema": schema,
        "worker": worker,
        "stale_jobs": stale_jobs,
        "reasons": unhealthy_reasons,
    }
    if db_error:
        payload["database_error"] = db_error
    if status == "ok":
        return payload
    return JSONResponse(status_code=503, content=payload)
