#!/usr/bin/env python3
"""Reviewed migration: copy legacy Meeting Analyzer data from public -> meeting_analyzer.

SAFETY:
- Read-only on source rows (SELECT only); inserts/copies into meeting_analyzer only.
- Does NOT drop/truncate/delete/modify public source records.
- Does NOT run Task Manager migrations.
- Requires explicit --execute flag; default is dry-run.
- Verifies row counts and key integrity before commit.

Use on a backed-up DB after review.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

SCHEMA = "meeting_analyzer"
LEGACY_SCHEMA = "public"
TABLES_IN_ORDER = [
    "users",
    "meetings",
    "meeting_files",
    "transcripts",
    "transcript_metrics",
    "summaries",
    "action_items",
    "processing_jobs",
    "speaker_profiles",
    "speaker_embeddings",
    "speaker_assignments",
    "backup_runs",
]
# Large object OIDs live in meeting_files.lo_oid and must be preserved as-is.
LO_TABLE = "meeting_files"


@dataclass
class TableReport:
    table: str
    source: int
    destination_before: int
    copied: int
    destination_after: int


def _db_url() -> str:
    for k in ("TEAMMEETINGAI_DATABASE_URL", "DATABASE_URL"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    raise SystemExit("Set TEAMMEETINGAI_DATABASE_URL or DATABASE_URL")


def _table_exists(conn: Connection, schema: str, table: str) -> bool:
    return bool(conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema=:s AND table_name=:t AND table_type='BASE TABLE'
    """), {"s": schema, "t": table}).scalar())


def _columns(conn: Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=:s AND table_name=:t
        ORDER BY ordinal_position
    """), {"s": schema, "t": table}).all()
    return [r[0] for r in rows]


def _count(conn: Connection, schema: str, table: str) -> int:
    return int(conn.execute(text(f'SELECT count(*) FROM "{schema}"."{table}"')).scalar_one())


def _pk_columns(conn: Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(text("""
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = (:s || '.' || :t)::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
    """), {"s": schema, "t": table}).all()
    return [r[0] for r in rows]


def _ensure_schema(conn: Connection) -> None:
    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))


def _copy_table(conn: Connection, table: str, execute: bool) -> TableReport:
    src_exists = _table_exists(conn, LEGACY_SCHEMA, table)
    dst_exists = _table_exists(conn, SCHEMA, table)
    if not src_exists:
        return TableReport(table, 0, _count(conn, SCHEMA, table) if dst_exists else 0, 0, _count(conn, SCHEMA, table) if dst_exists else 0)
    if not dst_exists:
        raise RuntimeError(f"Destination table missing: {SCHEMA}.{table}. Run MA alembic upgrade head first.")

    src_cols = _columns(conn, LEGACY_SCHEMA, table)
    dst_cols = _columns(conn, SCHEMA, table)
    common = [c for c in dst_cols if c in src_cols]
    if not common:
        raise RuntimeError(f"No common columns for {table}")

    src_n = _count(conn, LEGACY_SCHEMA, table)
    dst_before = _count(conn, SCHEMA, table)
    pks = _pk_columns(conn, SCHEMA, table)
    copied = 0
    if execute and src_n > 0:
        cols = ", ".join(f'"{c}"' for c in common)
        select_cols = ", ".join(f's."{c}"' for c in common)
        if pks:
            pk_conflict = ", ".join(f'"{c}"' for c in pks)
            sql = f'''
                INSERT INTO "{SCHEMA}"."{table}" ({cols})
                SELECT {select_cols}
                FROM "{LEGACY_SCHEMA}"."{table}" s
                ON CONFLICT ({pk_conflict}) DO NOTHING
            '''
        else:
            sql = f'''
                INSERT INTO "{SCHEMA}"."{table}" ({cols})
                SELECT {select_cols}
                FROM "{LEGACY_SCHEMA}"."{table}" s
            '''
        result = conn.execute(text(sql))
        copied = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0
        # Reset sequence/identity if present
        if "id" in common:
            conn.execute(text(f"""
                DO $$
                DECLARE seq text;
                BEGIN
                    seq := pg_get_serial_sequence('"{SCHEMA}.{table}"', 'id');
                    IF seq IS NOT NULL THEN
                        EXECUTE format('SELECT setval(%L, COALESCE((SELECT max(id) FROM "{SCHEMA}"."{table}"), 1), true)', seq);
                    END IF;
                END $$;
            """))
    dst_after = _count(conn, SCHEMA, table)
    return TableReport(table, src_n, dst_before, copied, dst_after)


def _verify(conn: Connection) -> list[str]:
    issues = []
    for table in TABLES_IN_ORDER:
        if not _table_exists(conn, LEGACY_SCHEMA, table) or not _table_exists(conn, SCHEMA, table):
            continue
        s = _count(conn, LEGACY_SCHEMA, table)
        d = _count(conn, SCHEMA, table)
        if d < s:
            issues.append(f"{table}: destination count {d} < source {s}")
    # FK check for meeting_analyzer
    bad_fks = conn.execute(text("""
        SELECT conname, conrelid::regclass::text
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid=c.connamespace
        WHERE n.nspname=:s AND c.contype='f'
        AND NOT EXISTS (SELECT 1 FROM pg_constraint c2 WHERE c2.oid=c.oid) -- placeholder, real validation below
    """), {"s": SCHEMA}).fetchall()
    # Real FK validation by checking convalidated
    invalid = conn.execute(text("""
        SELECT conname, conrelid::regclass::text
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid=c.connamespace
        WHERE n.nspname=:s AND c.contype='f' AND c.convalidated = false
    """), {"s": SCHEMA}).all()
    for name, rel in invalid:
        issues.append(f"invalid FK {name} on {rel}")
    # LO references
    if _table_exists(conn, SCHEMA, "meeting_files"):
        missing_los = conn.execute(text(f'''
            SELECT mf.lo_oid FROM "{SCHEMA}".meeting_files mf
            LEFT JOIN pg_largeobject_metadata lom ON lom.oid = mf.lo_oid
            WHERE lom.oid IS NULL
        ''')).all()
        for (oid,) in missing_los:
            issues.append(f"missing large object {oid}")
    return issues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually copy data. Default dry-run.")
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL env var.")
    args = ap.parse_args()

    url = args.database_url or _db_url()
    engine = create_engine(url, future=True)
    reports: list[TableReport] = []
    with engine.begin() as conn:
        _ensure_schema(conn)
        for table in TABLES_IN_ORDER:
            reports.append(_copy_table(conn, table, execute=args.execute))
        issues = _verify(conn)
        if not args.execute:
            print("DRY RUN - no data copied. Re-run with --execute after review.")
        for r in reports:
            print(f"{r.table:24} src={r.source:6} dst_before={r.destination_before:6} copied={r.copied:6} dst_after={r.destination_after:6}")
        if issues:
            print("ISSUES:")
            for i in issues:
                print(" -", i)
            raise SystemExit(2)
        print("VERIFICATION: OK")
        if not args.execute:
            # rollback dry-run transaction to be safe
            conn.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
