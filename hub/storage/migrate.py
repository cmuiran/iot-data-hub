"""Apply db/migrations/*.sql in order, once each (recorded in schema_migrations).

A file whose first line contains ``migrate: no-transaction`` is run statement by statement in
autocommit mode (needed for TimescaleDB continuous aggregates); every other file runs in one
transaction. ``requires-extension <name>`` on that line skips the file when the extension is
not installed on the server (``timescale="auto"``), so plain PostgreSQL works out of the box.

    python -m hub.storage.migrate --database-url postgresql://...
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

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
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
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for m in discover(directory):
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
    args = parser.parse_args(argv)
    if not args.database_url:
        print("set HUB_DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    applied = migrate(args.database_url, args.timescale)
    print(f"applied: {', '.join(applied) or 'nothing (up to date)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
