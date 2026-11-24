"""pg_dump backup for the TeamMeetingAI PostgreSQL database.

Produces a custom-format dump (large objects included) and records the run
in the ``backup_runs`` table so ``GET /system/status`` can report it.

Usage::

    python scripts/backup_database.py [--output-dir DIR]

Configuration (in order):

* Database URL: ``--database`` argument, then ``TEAMMEETINGAI_DATABASE_URL``,
  then ``DATABASE_URL`` (normally loaded from ``.env``), then the built-in
  default from ``db.session``.
* ``pg_dump`` binary: ``TEAMMEETINGAI_PG_DUMP``, then ``PATH``, then the
  standard Windows PostgreSQL install locations.
* Output directory: ``--output-dir``, then ``TEAMMEETINGAI_BACKUP_DIR``,
  then ``<project>/backups``.

The exit code is non-zero when the backup fails; the error is recorded in
``backup_runs`` and printed without echoing the database URL or password.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from db.session import database_url_from_env  # noqa: E402


def _tool_version(path: Path) -> int:
    """Return the tool's major version (0 when it cannot be determined)."""

    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, check=True
        )
        for token in result.stdout.split():
            if token.isdigit():
                return int(token.split(".")[0])
    except Exception:
        pass
    return 0


def find_client_tool(tool_name: str, env_var: str, server_major: int | None) -> Path:
    """Locate a pg_dump/pg_restore binary matching the server major version.

    ``<env_var>`` wins outright; then ``PATH``; then the standard Windows
    install locations.  When several are installed, the one whose major
    version matches the server is preferred (a mismatched pg_dump embeds
    server-version-specific SET statements that break pg_restore).
    """

    override = os.environ.get(env_var)
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise FileNotFoundError(f"{env_var} does not exist: {path}")

    candidates: list[Path] = []
    found = shutil.which(tool_name)
    if found:
        candidates.append(Path(found))
    install_root = Path("C:/Program Files/PostgreSQL")
    if install_root.is_dir():
        candidates.extend(install_root.glob("*/bin/" + tool_name + ".exe"))
    if not candidates:
        raise FileNotFoundError(
            f"{tool_name} was not found. Install PostgreSQL client tools or "
            f"set {env_var} to the executable."
        )

    def preference(path: Path) -> tuple[int, int]:
        major = _tool_version(path)
        version_match = 1 if server_major and major == server_major else 0
        return (version_match, major)

    return max(candidates, key=preference)


def server_major_version(url: str) -> int | None:
    """Return the target server's major version, when it can be queried."""

    try:
        engine = create_engine(url, future=True)
        try:
            with engine.connect() as conn:
                version = conn.execute(
                    text("SHOW server_version")
                ).scalar_one()
        finally:
            engine.dispose()
        return int(str(version).split(".")[0])
    except Exception:
        return None


def run_backup(url: str, output_dir: Path) -> Path:
    """Dump the database to ``output_dir`` and return the dump file path."""

    pg_dump = find_client_tool("pg_dump", "TEAMMEETINGAI_PG_DUMP", server_major_version(url))
    parsed = make_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dump_path = output_dir / f"teammeetingai-{stamp}.dump"

    command: list[str] = [
        str(pg_dump),
        "--format=custom",
        "--file",
        str(dump_path),
        "--dbname",
        parsed.database or "",
    ]
    if parsed.host:
        command += ["--host", parsed.host]
    if parsed.port:
        command += ["--port", str(parsed.port)]
    if parsed.username:
        command += ["--username", parsed.username]

    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    result = subprocess.run(
        command, capture_output=True, text=True, env=env, check=False
    )
    if result.returncode != 0:
        if dump_path.exists():
            dump_path.unlink()
        raise RuntimeError(
            f"pg_dump failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    if not dump_path.is_file() or dump_path.stat().st_size == 0:
        raise RuntimeError("pg_dump produced no output.")
    return dump_path


def start_run(url: str) -> Any:
    """Insert the 'running' ``backup_runs`` row and return its id."""

    engine = create_engine(url, future=True)
    try:
        with engine.begin() as conn:
            return conn.execute(
                text("INSERT INTO meeting_analyzer.backup_runs (status) VALUES ('running') RETURNING id")
            ).scalar_one()
    finally:
        engine.dispose()


def finish_run(
    url: str,
    run_id: Any,
    *,
    status: str,
    output_path: str | None,
    size_bytes: int | None,
    error: str | None,
) -> None:
    """Update the ``backup_runs`` row with the terminal state."""

    engine = create_engine(url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE meeting_analyzer.backup_runs SET status = :status, output_path = "
                    ":output_path, size_bytes = :size_bytes, error = :error, "
                    "finished_at = now() WHERE id = :id"
                ),
                {
                    "status": status,
                    "output_path": output_path,
                    "size_bytes": size_bytes,
                    "error": error,
                    "id": run_id,
                },
            )
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the TeamMeetingAI database.")
    parser.add_argument(
        "--database",
        default=None,
        help="Database URL (defaults to TEAMMEETINGAI_DATABASE_URL / DATABASE_URL)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Backup destination directory (default: TEAMMEETINGAI_BACKUP_DIR "
        "or <project>/backups)",
    )
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    url = args.database or database_url_from_env()
    output_dir = Path(
        args.output_dir
        or os.environ.get("TEAMMEETINGAI_BACKUP_DIR")
        or PROJECT_ROOT / "backups"
    )

    run_id: Any | None = None
    try:
        run_id = start_run(url)
        dump_path = run_backup(url, output_dir)
        finish_run(
            url,
            run_id,
            status="succeeded",
            output_path=str(dump_path),
            size_bytes=dump_path.stat().st_size,
            error=None,
        )
    except Exception as exc:
        print(f"Backup FAILED: {exc}", file=sys.stderr)
        if run_id is not None:
            try:
                finish_run(
                    url,
                    run_id,
                    status="failed",
                    output_path=None,
                    size_bytes=None,
                    error=str(exc),
                )
            except Exception:
                pass  # the dump failure is the primary error
        return 1
    print(f"Backup succeeded: {dump_path} ({dump_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
