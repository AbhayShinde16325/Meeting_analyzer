"""pg_restore for a TeamMeetingAI database backup.

Restores a dump produced by ``scripts/backup_database.py`` (custom format,
large objects included) into a target database.

Usage::

    python scripts/restore_database.py BACKUP_FILE [--database URL] [--yes]

The restore runs ``pg_restore --clean --if-exists``: existing objects in the
target database are dropped and replaced.  Because that is destructive, the
command refuses to run without ``--yes``.

Configuration is resolved like the backup script: ``--database`` argument,
then ``TEAMMEETINGAI_DATABASE_URL`` / ``DATABASE_URL`` (normally from
``.env``); the ``pg_restore`` binary comes from ``TEAMMEETINGAI_PG_RESTORE``,
``PATH``, or the standard Windows install locations.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from db.session import database_url_from_env  # noqa: E402
from backup_database import find_client_tool, server_major_version  # noqa: E402


def run_restore(dump_path: Path, url: str) -> list[str]:
    """Restore ``dump_path`` into the target database (destructive).

    Returns the list of errors pg_restore chose to ignore (empty on a clean
    run); raises on a failed restore.
    """

    pg_restore = find_client_tool(
        "pg_restore", "TEAMMEETINGAI_PG_RESTORE", server_major_version(url)
    )
    parsed = make_url(url)

    command: list[str] = [
        str(pg_restore),
        "--clean",
        "--if-exists",
        "--no-owner",
        "--dbname",
        parsed.database or "",
        str(dump_path),
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
    stderr = result.stderr.strip()
    if result.returncode != 0:
        errors = [
            line
            for line in stderr.splitlines()
            if "error:" in line.lower()
        ]
        # A client/server version mismatch makes pg_restore emit
        # server-version-specific "SET ..." statements the target does not
        # know (e.g. "unrecognized configuration parameter
        # \"transaction_timeout\"").  Those are harmless: everything else
        # restored and pg_restore reports them as ignored.  Any other error
        # means a real failure.
        benign = all("unrecognized configuration parameter" in line for line in errors)
        if result.returncode == 1 and benign:
            return errors
        raise RuntimeError(
            f"pg_restore failed (exit {result.returncode}): {stderr}"
        )
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a TeamMeetingAI backup.")
    parser.add_argument("dump_file", type=Path, help="Backup file from backup_database.py")
    parser.add_argument(
        "--database",
        default=None,
        help="Target database URL (defaults to TEAMMEETINGAI_DATABASE_URL / DATABASE_URL)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the destructive restore (existing data is replaced)",
    )
    args = parser.parse_args()

    if not args.yes:
        print(
            "Refusing to restore without --yes: existing objects in the target "
            "database are dropped and replaced.",
            file=sys.stderr,
        )
        return 2
    if not args.dump_file.is_file():
        print(f"Backup file not found: {args.dump_file}", file=sys.stderr)
        return 2

    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    url = args.database or database_url_from_env()
    try:
        ignored = run_restore(args.dump_file.resolve(), url)
    except Exception as exc:
        print(f"Restore FAILED: {exc}", file=sys.stderr)
        return 1
    for line in ignored:
        print(f"Ignored (client/server version mismatch): {line}", file=sys.stderr)
    print(f"Restore succeeded: {args.dump_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
