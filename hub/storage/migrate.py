"""Apply db/migrations/*.sql in order, once each (recorded in schema_migrations).

A file whose first line contains ``migrate: no-transaction`` is run statement by statement in
autocommit mode (needed for TimescaleDB continuous aggregates); every other file runs in one
transaction. ``requires-extension <name>`` on that line skips the file when the extension is
not installed on the server (``timescale="auto"``), so plain PostgreSQL works out of the box.

    python -m hub.storage.migrate --database-url postgresql://... [--dry-run]

Rollback safety: migrations only go forward. A database that already has a migration this code
does not know (it was upgraded by a newer hub) is refused with :class:`SchemaTooNew` instead of
being used by older code; roll back by restoring the pre-upgrade backup (see docs/backend.md,
"Upgrade and rollback").
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg

from hub.paths import program_path

log = logging.getLogger(__name__)

MIGRATIONS_DIR = program_path("db", "migrations")
TimescaleMode = Literal["auto", "on", "off"]


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    transactional: bool
    requires_extension: str | None

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    out = []
    for path in sorted(directory.glob("*.sql")):
        first = path.read_text(encoding="utf-8").splitlines()[0] if path.stat().st_size else ""
        header = first if "migrate:" in first else ""
        ext = re.search(r"requires-extension\s+(\w+)", header)
        out.append(Migration(version=path.stem, path=path,
                             transactional="no-transaction" not in header,
                             requires_extension=ext.group(1) if ext else None))
    return out


class SchemaTooNew(RuntimeError):
    """The database was migrated by a newer version of the hub."""


def _applied(conn: psycopg.Connection) -> set[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def _check_not_newer(done: set[str], known: list[Migration]) -> None:
    unknown = sorted(done - {m.version for m in known})
    if unknown:
        raise SchemaTooNew(
            f"the database has migration(s) {', '.join(unknown)} that this version of the hub "
            "does not know: it was upgraded by a newer hub. Install that version again, or "
            "restore the backup taken before the upgrade (hub-admin restore-backup).")


def status(database_url: str, directory: Path = MIGRATIONS_DIR) -> dict[str, list[str]]:
    """Applied, pending and unknown (newer) migrations, without changing anything."""
    known = discover(directory)
    with psycopg.connect(database_url, autocommit=True) as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]  # type: ignore[index]
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")} \
            if exists else set()
        optional = {m.version for m in known if m.requires_extension
                    and not _extension_available(conn, m.requires_extension)}
    names = {m.version for m in known}
    todo = [m.version for m in known if m.version not in done]
    return {"applied": sorted(done & names), "pending": [v for v in todo if v not in optional],
            "skipped": [v for v in todo if v in optional], "unknown": sorted(done - names)}


def _statements(sql: str) -> list[str]:
    """Split a no-transaction file on ';' at line ends (such files must not contain $$ bodies)."""
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in re.split(r";\s*(?:\n|$)", body) if s.strip()]


def _extension_available(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = %s", (name,)).fetchone()
    return row is not None


def migrate(database_url: str, timescale: TimescaleMode = "auto",
            directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations; returns the versions applied in this call."""
    applied_now: list[str] = []
    known = discover(directory)
    with psycopg.connect(database_url, autocommit=True) as conn:
        done = _applied(conn)
        _check_not_newer(done, known)
        for m in known:
            if m.version in done:
                continue
            if m.requires_extension:
                wanted = timescale != "off" if m.requires_extension == "timescaledb" else True
                available = _extension_available(conn, m.requires_extension)
                if not wanted or (not available and timescale == "auto"):
                    log.info("skipping %s (%s not %s)", m.version, m.requires_extension,
                             "wanted" if not wanted else "available")
                    continue
            log.info("applying %s", m.version)
            if m.transactional:
                with conn.transaction():
                    conn.execute(m.sql)
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)",
                                 (m.version,))
            else:
                for statement in _statements(m.sql):
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (m.version,))
            applied_now.append(m.version)
    return applied_now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hub.storage.migrate")
    parser.add_argument("--database-url", default=os.environ.get("HUB_DATABASE_URL"))
    parser.add_argument("--timescale", choices=["auto", "on", "off"],
                        default=os.environ.get("HUB_DB_TIMESCALE", "auto"))
    parser.add_argument("--dry-run", action="store_true", help="only list pending migrations")
    args = parser.parse_args(argv)
    if not args.database_url:
        print("set HUB_DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.dry_run:
        state = status(args.database_url)
        print(f"applied: {len(state['applied'])}; pending: {', '.join(state['pending']) or 'none'}")
        if state["unknown"]:
            print(f"NEWER than this code: {', '.join(state['unknown'])}", file=sys.stderr)
            return 1
        return 0
    try:
        applied = migrate(args.database_url, args.timescale)
    except SchemaTooNew as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"applied: {', '.join(applied) or 'nothing (up to date)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
