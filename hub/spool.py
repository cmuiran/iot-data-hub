"""Store & forward: a first-in, first-out queue on disk that survives restarts.

Used while a destination is unreachable: samples waiting for the database (hub.runtime) and
messages waiting for a northbound MQTT broker (backend.northbound). One SQLite file per queue
(WAL journal, synchronous=NORMAL: a power cut loses at most the last transaction, never the
file). The size is bounded: past ``max_bytes`` the oldest items are dropped and counted,
because a remote site can be cut off for longer than any disk lasts.

SQLite is used from a worker thread (``asyncio.to_thread``), one operation at a time.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class DiskQueue:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.dropped = 0
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY "
                         "AUTOINCREMENT, item BLOB NOT NULL)")
        self._bytes = self._db.execute(
            "SELECT coalesce(sum(length(item)), 0) FROM queue").fetchone()[0]
        self._count = self._db.execute("SELECT count(*) FROM queue").fetchone()[0]
        if self._count:
            log.info("%s: %d item(s) waiting from before the restart", self.path.name,
                     self._count)

    # --- synchronous core (one thread at a time) ----------------------------------------------

    def _put(self, items: list[bytes]) -> int:
        with self._lock:
            self._db.execute("BEGIN")
            self._db.executemany("INSERT INTO queue (item) VALUES (?)", [(i,) for i in items])
            self._db.execute("COMMIT")
            self._count += len(items)
            self._bytes += sum(len(i) for i in items)
            dropped = 0
            while self._bytes > self.max_bytes and self._count:
                row = self._db.execute(
                    "SELECT id, length(item) FROM queue ORDER BY id LIMIT 1").fetchone()
                self._db.execute("DELETE FROM queue WHERE id = ?", (row[0],))
                self._count -= 1
                self._bytes -= row[1]
                dropped += 1
            self.dropped += dropped
            return dropped

    def _peek(self, n: int) -> list[tuple[int, bytes]]:
        with self._lock:
            return [(r[0], bytes(r[1])) for r in self._db.execute(
                "SELECT id, item FROM queue ORDER BY id LIMIT ?", (n,))]

    def _ack(self, last_id: int) -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT count(*), coalesce(sum(length(item)), 0) FROM queue WHERE id <= ?",
                (last_id,)).fetchone()
            self._db.execute("DELETE FROM queue WHERE id <= ?", (last_id,))
            self._count -= row[0]
            self._bytes -= row[1]

    # --- async API ----------------------------------------------------------------------------

    @property
    def count(self) -> int:
        return self._count

    @property
    def size_bytes(self) -> int:
        return self._bytes

    async def put(self, items: list[bytes]) -> int:
        """Append; returns how many old items were dropped to stay within ``max_bytes``."""
        if not items:
            return 0
        dropped = await asyncio.to_thread(self._put, items)
        if dropped:
            log.warning("%s: full (%d MB), dropped the %d oldest item(s)", self.path.name,
                        self.max_bytes // 2**20, dropped)
        return dropped

    async def peek(self, n: int) -> list[tuple[int, bytes]]:
        """The ``n`` oldest items with their ids (still in the queue until ``ack``)."""
        if not self._count:
            return []
        return await asyncio.to_thread(self._peek, n)

    async def ack(self, last_id: int) -> None:
        """Remove every item up to ``last_id`` (they were delivered)."""
        await asyncio.to_thread(self._ack, last_id)

    def close(self) -> None:
        with self._lock:
            self._db.close()
