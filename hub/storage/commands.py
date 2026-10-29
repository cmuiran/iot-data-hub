"""Command store (0012): the life of each command, for every protocol.

Statuses only move forward (created < sent < delivered < executed < confirmed); failed and
expired end a command; an ended command never changes again. Each step keeps its time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel

ORDER = {"created": 0, "sent": 1, "delivered": 2, "executed": 3, "confirmed": 4}
FINAL = frozenset({"confirmed", "failed", "expired"})
_STAMP = {"sent": "sent_at", "delivered": "delivered_at", "executed": "executed_at"}


class CommandRecord(BaseModel):
    command_id: UUID
    device_id: str
    tag_path: str
    value: Any
    status: str
    transport: str | None = None
    created_by: int | None = None
    created_at: datetime
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    executed_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime
    error: str | None = None
    reason: str | None = None        # of the write: "app", "automation:<id> <name>", ...


class CommandStore:
    def __init__(self, store: Any) -> None:
        self._store = store

    def _conn(self) -> Any:
        return self._store._conn()

    async def create(self, *, command_id: UUID, device_id: str, tag_path: str, value: Any,
                     created_by: int | None, expires_at: datetime) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO commands (command_id, device_id, tag_id, tag_path, value, "
                "created_by, expires_at) SELECT %s, %s, p.tag_id, %s, %s, %s, %s "
                "FROM tag_paths p WHERE p.tag_path = %s",
                (command_id, device_id, tag_path, Jsonb(value), created_by, expires_at,
                 tag_path))

    async def advance(self, command_id: UUID, status: str, now: datetime, *,
                      transport: str | None = None, error: str | None = None,
                      device_id: str | None = None) -> CommandRecord | None:
        """Move a command forward (or end it); earlier steps get the same time when they
        were skipped. ``device_id``, when given, must match (a device acks only its own
        commands). None when unknown, ended, or the status would go backwards."""
        if status in FINAL:
            stamps = [c for s, c in _STAMP.items() if status == "confirmed"]
        else:
            stamps = [c for s, c in _STAMP.items() if ORDER[s] <= ORDER[status]]
        sets = ", ".join(f"{c} = coalesce({c}, %(now)s)" for c in stamps)
        allowed = [s for s, n in ORDER.items() if status in FINAL or n < ORDER[status]]
        sql = ("UPDATE commands SET status = %(status)s, "  # nosec B608 - constant columns
               + (sets + ", " if sets else "")
               + "finished_at = CASE WHEN %(final)s THEN %(now)s ELSE finished_at END, "
               "transport = coalesce(%(transport)s, transport), "
               "error = coalesce(%(error)s, error) WHERE command_id = %(id)s "
               "AND finished_at IS NULL AND status = ANY(%(allowed)s)")
        params: dict[str, Any] = {"status": status, "now": now, "final": status in FINAL,
                                  "transport": transport, "error": error, "id": command_id,
                                  "allowed": allowed}
        if device_id is not None:
            sql += " AND device_id = %(device)s"
            params["device"] = device_id
        async with self._conn() as conn:
            row = await (await conn.execute(sql + " RETURNING *", params)).fetchone()
        return CommandRecord.model_validate(row) if row else None

    async def get(self, command_id: UUID) -> CommandRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT * FROM commands WHERE command_id=%s",
                                            (command_id,))).fetchone()
        return CommandRecord.model_validate(row) if row else None

    async def for_device(self, device_id: str, limit: int = 100) -> list[CommandRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT c.*, w.reason FROM commands c LEFT JOIN write_audit w ON "
                "w.request_id = c.command_id AND w.phase = 'requested' WHERE c.device_id=%s "
                "ORDER BY c.created_at DESC LIMIT %s", (device_id, limit))).fetchall()
        return [CommandRecord.model_validate(r) for r in rows]

    async def expire(self, now: datetime) -> int:
        async with self._conn() as conn:
            cur = await conn.execute(
                "UPDATE commands SET status='expired', finished_at=%s, error=coalesce(error, "
                "'not executed in time') WHERE finished_at IS NULL AND expires_at < %s",
                (now, now))
            return cur.rowcount
