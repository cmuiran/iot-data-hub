"""Automation store (0013): scenes, automations, their runs, holidays.

Methods taking ``orgs`` run as hub_tenant limited to those organizations (row-level
security); the engine (backend.automations) uses the service methods.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from hub.storage.platform import PlatformError, _as_tenant


class SceneRecord(BaseModel):
    scene_id: int
    org_id: str
    name: str
    icon: str | None = None
    actions: list[dict[str, Any]]
    created_by: int | None = None
    updated_at: datetime


class AutomationRecord(BaseModel):
    automation_id: int
    org_id: str
    name: str
    enabled: bool
    definition: dict[str, Any]
    created_by: int
    created_at: datetime
    updated_at: datetime
    last_fired_at: datetime | None = None


class RunRecord(BaseModel):
    run_id: int
    automation_id: int
    trigger_key: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    detail: list[dict[str, Any]]


class HolidayRecord(BaseModel):
    holiday_id: int
    org_id: str | None = None
    day: date
    name: str


class AutomationStore:
    def __init__(self, store: Any) -> None:
        self._store = store

    @contextlib.asynccontextmanager
    async def _tx(self, orgs: set[str] | None) -> AsyncIterator[Any]:
        async with self._store._conn() as conn, conn.transaction():
            if orgs is not None:
                await _as_tenant(conn, orgs)
            yield conn

    # --- scenes -------------------------------------------------------------------------------

    async def scenes(self, orgs: set[str] | None, org_id: str) -> list[SceneRecord]:
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(
                "SELECT * FROM scenes WHERE org_id=%s ORDER BY name", (org_id,))).fetchall()
        return [SceneRecord.model_validate(r) for r in rows]

    async def scene(self, orgs: set[str] | None, scene_id: int) -> SceneRecord:
        async with self._tx(orgs) as conn:
            row = await (await conn.execute("SELECT * FROM scenes WHERE scene_id=%s",
                                            (scene_id,))).fetchone()
        if row is None:
            raise PlatformError("scene not found", "not_found")
        return SceneRecord.model_validate(row)

    async def save_scene(self, orgs: set[str] | None, org_id: str, scene_id: int | None, *,
                         name: str, icon: str | None, actions: list[dict[str, Any]],
                         user_id: int | None) -> SceneRecord:
        try:
            async with self._tx(orgs) as conn:
                if scene_id is None:
                    row = await (await conn.execute(
                        "INSERT INTO scenes (org_id, name, icon, actions, created_by) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING *",
                        (org_id, name, icon, Jsonb(actions), user_id))).fetchone()
                else:
                    row = await (await conn.execute(
                        "UPDATE scenes SET name=%s, icon=%s, actions=%s, updated_at=now() "
                        "WHERE scene_id=%s AND org_id=%s RETURNING *",
                        (name, icon, Jsonb(actions), scene_id, org_id))).fetchone()
        except psycopg.errors.InsufficientPrivilege:
            raise PlatformError("organization not found", "not_found") from None
        if row is None:
            raise PlatformError("scene not found", "not_found")
        return SceneRecord.model_validate(row)

    async def delete_scene(self, orgs: set[str] | None, org_id: str, scene_id: int) -> bool:
        async with self._tx(orgs) as conn:
            cur = await conn.execute("DELETE FROM scenes WHERE scene_id=%s AND org_id=%s",
                                     (scene_id, org_id))
            return cur.rowcount == 1

    # --- automations --------------------------------------------------------------------------

    async def automations(self, orgs: set[str] | None, org_id: str | None = None
                          ) -> list[AutomationRecord]:
        sql, params = "SELECT * FROM automations", []
        if org_id is not None:
            sql += " WHERE org_id=%s"
            params.append(org_id)
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(sql + " ORDER BY name, automation_id",
                                             params)).fetchall()
        return [AutomationRecord.model_validate(r) for r in rows]

    async def automation(self, orgs: set[str] | None, automation_id: int) -> AutomationRecord:
        async with self._tx(orgs) as conn:
            row = await (await conn.execute("SELECT * FROM automations WHERE automation_id=%s",
                                            (automation_id,))).fetchone()
        if row is None:
            raise PlatformError("automation not found", "not_found")
        return AutomationRecord.model_validate(row)

    async def save_automation(self, orgs: set[str] | None, org_id: str,
                              automation_id: int | None, *, name: str, enabled: bool,
                              definition: dict[str, Any], user_id: int) -> AutomationRecord:
        """A change starts the automation afresh: occurrences before it are not caught up."""
        try:
            async with self._tx(orgs) as conn:
                if automation_id is None:
                    row = await (await conn.execute(
                        "INSERT INTO automations (org_id, name, enabled, definition, created_by)"
                        " VALUES (%s,%s,%s,%s,%s) RETURNING *",
                        (org_id, name, enabled, Jsonb(definition), user_id))).fetchone()
                else:
                    row = await (await conn.execute(
                        "UPDATE automations SET name=%s, enabled=%s, definition=%s, "
                        "updated_at=now(), last_fired_at=NULL WHERE automation_id=%s AND "
                        "org_id=%s RETURNING *",
                        (name, enabled, Jsonb(definition), automation_id, org_id))).fetchone()
        except psycopg.errors.InsufficientPrivilege:
            raise PlatformError("organization not found", "not_found") from None
        if row is None:
            raise PlatformError("automation not found", "not_found")
        return AutomationRecord.model_validate(row)

    async def delete_automation(self, orgs: set[str] | None, org_id: str,
                                automation_id: int) -> bool:
        async with self._tx(orgs) as conn:
            cur = await conn.execute(
                "DELETE FROM automations WHERE automation_id=%s AND org_id=%s",
                (automation_id, org_id))
            return cur.rowcount == 1

    async def runs(self, orgs: set[str] | None, automation_id: int, limit: int = 50
                   ) -> list[RunRecord]:
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(
                "SELECT * FROM automation_runs WHERE automation_id=%s ORDER BY started_at DESC, "
                "run_id DESC LIMIT %s", (automation_id, limit))).fetchall()
        return [RunRecord.model_validate(r) for r in rows]

    # --- the engine (service) -----------------------------------------------------------------

    async def start_run(self, automation_id: int, trigger_key: str) -> int | None:
        """A new run for this occurrence; None if it already ran (another node, a restart)."""
        async with self._tx(None) as conn:
            row = await (await conn.execute(
                "INSERT INTO automation_runs (automation_id, trigger_key) VALUES (%s,%s) "
                "ON CONFLICT (automation_id, trigger_key) DO NOTHING RETURNING run_id",
                (automation_id, trigger_key))).fetchone()
        return row["run_id"] if row else None

    async def finish_run(self, run_id: int, status: str, detail: list[dict[str, Any]]
                         ) -> None:
        async with self._tx(None) as conn:
            await conn.execute(
                "UPDATE automation_runs SET status=%s, detail=%s, finished_at=now() "
                "WHERE run_id=%s", (status, Jsonb(detail), run_id))

    async def set_fired(self, automation_id: int, at: datetime) -> None:
        async with self._tx(None) as conn:
            await conn.execute(
                "UPDATE automations SET last_fired_at=greatest(coalesce(last_fired_at, %s), %s)"
                " WHERE automation_id=%s", (at, at, automation_id))

    async def last_manual_write(self, tag_path: str, since: datetime) -> datetime | None:
        """The last write applied to a tag by a person (not by an automation) since then."""
        async with self._tx(None) as conn:
            # the reason of a write is on its "requested" row; the outcome row says how it went
            row = await (await conn.execute(
                "SELECT max(o.ts) AS ts FROM write_audit o JOIN write_audit q ON "
                "q.request_id = o.request_id AND q.phase = 'requested' WHERE o.tag_path=%s AND "
                "o.phase='outcome' AND o.result='applied' AND o.ts >= %s AND "
                "coalesce(q.reason, '') NOT LIKE 'automation:%%' AND "
                "coalesce(q.reason, '') NOT LIKE 'scene:%%'", (tag_path, since))).fetchone()
        return row["ts"] if row else None

    # --- holidays -----------------------------------------------------------------------------

    async def holidays(self, orgs: set[str] | None, org_id: str | None, start: date,
                       end: date) -> list[HolidayRecord]:
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(
                "SELECT * FROM holidays WHERE day BETWEEN %s AND %s AND (org_id IS NULL OR "
                "org_id = %s) ORDER BY day", (start, end, org_id))).fetchall()
        return [HolidayRecord.model_validate(r) for r in rows]

    async def add_holiday(self, orgs: set[str] | None, org_id: str | None, day: date,
                          name: str, user_id: int | None) -> HolidayRecord:
        try:
            async with self._tx(orgs) as conn:
                row = await (await conn.execute(
                    "INSERT INTO holidays (org_id, day, name, created_by) VALUES (%s,%s,%s,%s) "
                    "RETURNING *", (org_id, day, name, user_id))).fetchone()
        except psycopg.errors.UniqueViolation:
            raise PlatformError("that day is already a holiday", "conflict") from None
        except psycopg.errors.InsufficientPrivilege:
            raise PlatformError("organization not found", "not_found") from None
        return HolidayRecord.model_validate(row)

    async def delete_holiday(self, orgs: set[str] | None, holiday_id: int) -> bool:
        async with self._tx(orgs) as conn:
            cur = await conn.execute("DELETE FROM holidays WHERE holiday_id=%s", (holiday_id,))
            return cur.rowcount == 1
