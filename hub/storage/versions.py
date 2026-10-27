"""Version store (0014): moving devices to another template version, firmware releases and
jobs. Service code (backend.versions checks who may do what with the RLS-limited reads of
hub.storage.platform first)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from hub.models import TagBase
from hub.storage.platform import PlatformError


class ReleaseRecord(BaseModel):
    release_id: int
    product_id: UUID
    version: str
    notes: str | None = None
    sha256: str
    size_bytes: int
    min_template_version: int | None = None
    status: str
    created_at: datetime


class JobRecord(BaseModel):
    job_id: UUID
    release_id: int
    device_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    reported_version: str | None = None
    error: str | None = None


_RELEASE_COLS = ("release_id, product_id, version, notes, sha256, size_bytes, "
                 "min_template_version, status, created_at")
JOB_ORDER = {"pending": 0, "sent": 1, "downloading": 2, "installed": 3, "failed": 3,
             "cancelled": 3}


class VersionStore:
    def __init__(self, store: Any) -> None:
        self._store = store

    def _conn(self) -> Any:
        return self._store._conn()

    # --- template versions --------------------------------------------------------------------

    async def devices_of(self, product_id: UUID, version: int | None = None,
                         device_ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT pd.device_id, pd.version, pd.identifier, d.protocol, d.enabled FROM "
               "platform_devices pd JOIN devices d USING (device_id) WHERE pd.product_id=%s "
               "AND pd.retired_at IS NULL")
        params: list[Any] = [product_id]
        if version is not None:
            sql += " AND pd.version=%s"
            params.append(version)
        if device_ids is not None:
            sql += " AND pd.device_id = ANY(%s)"
            params.append(list(device_ids))
        async with self._conn() as conn:
            rows = await (await conn.execute(sql + " ORDER BY pd.identifier", params)).fetchall()
        return [dict(r) for r in rows]

    async def move(self, device_id: str, version: int, tags: list[TagBase]) -> None:
        """The device uses ``version``: its tags follow (same key = same tag id and history;
        new keys added, vanished keys retired, never deleted)."""
        from hub.storage.postgres import _address

        async with self._conn() as conn, conn.transaction():
            for t in tags:
                await conn.execute(
                    "INSERT INTO tags (device_id, name, description, data_type, unit, scale, "
                    '"offset", access, min_value, max_value, address, write_class) VALUES '
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (device_id, name) DO "
                    "UPDATE SET description=EXCLUDED.description, data_type=EXCLUDED.data_type, "
                    'unit=EXCLUDED.unit, scale=EXCLUDED.scale, "offset"=EXCLUDED."offset", '
                    "access=EXCLUDED.access, min_value=EXCLUDED.min_value, "
                    "max_value=EXCLUDED.max_value, address=EXCLUDED.address, "
                    "write_class=EXCLUDED.write_class, updated_at=now(), retired_at=NULL",
                    (device_id, t.name, t.description, str(t.data_type), t.unit, t.scale,
                     t.offset, str(t.access), t.min, t.max, Jsonb(_address(t)),
                     t.write_class and str(t.write_class)))
            await conn.execute(
                "UPDATE tags SET retired_at=now() WHERE device_id=%s AND retired_at IS NULL "
                "AND NOT (name = ANY(%s))", (device_id, [t.name for t in tags]))
            await conn.execute("UPDATE platform_devices SET version=%s, updated_at=now() "
                               "WHERE device_id=%s", (version, device_id))

    async def automations_using(self, device_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Automations and scenes whose definition names one of these devices."""
        async with self._conn() as conn:
            autos = await (await conn.execute(
                "SELECT automation_id AS id, 'automation' AS kind, name, definition FROM "
                "automations WHERE EXISTS (SELECT 1 FROM unnest(%s::text[]) d WHERE "
                "definition::text LIKE '%%' || d || '%%')", (list(device_ids),))).fetchall()
            scenes = await (await conn.execute(
                "SELECT scene_id AS id, 'scene' AS kind, name, jsonb_build_object('actions', "
                "actions) AS definition FROM scenes WHERE EXISTS (SELECT 1 FROM "
                "unnest(%s::text[]) d WHERE actions::text LIKE '%%' || d || '%%')",
                (list(device_ids),))).fetchall()
        return [dict(r) for r in [*autos, *scenes]]

    # --- firmware -----------------------------------------------------------------------------

    async def add_release(self, *, product_id: UUID, version: str, notes: str | None,
                          sha256: str, image: bytes, min_template_version: int | None,
                          user_id: int | None) -> ReleaseRecord:
        try:
            async with self._conn() as conn:
                row = await (await conn.execute(
                    "INSERT INTO firmware_releases (product_id, version, notes, sha256, "
                    "size_bytes, image, min_template_version, created_by) VALUES "
                    f"(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_RELEASE_COLS}",  # nosec B608
                    (product_id, version, notes, sha256, len(image), image,
                     min_template_version, user_id))).fetchone()
        except psycopg.errors.UniqueViolation:
            raise PlatformError(f"version {version} exists for this product",
                                "conflict") from None
        return ReleaseRecord.model_validate(row)

    async def releases(self, product_id: UUID) -> list[ReleaseRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT {_RELEASE_COLS} FROM firmware_releases WHERE product_id=%s "  # nosec B608
                "ORDER BY created_at DESC", (product_id,))).fetchall()
        return [ReleaseRecord.model_validate(r) for r in rows]

    async def release(self, release_id: int) -> ReleaseRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                f"SELECT {_RELEASE_COLS} FROM firmware_releases WHERE release_id=%s",  # nosec B608
                (release_id,))).fetchone()
        return ReleaseRecord.model_validate(row) if row else None

    async def image(self, release_id: int) -> bytes | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT image FROM firmware_releases WHERE release_id=%s AND status='released'",
                (release_id,))).fetchone()
        return bytes(row["image"]) if row else None

    async def withdraw(self, release_id: int) -> None:
        async with self._conn() as conn, conn.transaction():
            await conn.execute("UPDATE firmware_releases SET status='withdrawn' WHERE "
                               "release_id=%s", (release_id,))
            await conn.execute(
                "UPDATE firmware_jobs SET status='cancelled', updated_at=now(), error="
                "'release withdrawn' WHERE release_id=%s AND status IN ('pending', 'sent')",
                (release_id,))

    async def create_jobs(self, release_id: int, jobs: dict[str, UUID], user_id: int | None
                          ) -> list[str]:
        """One job per device (device_id -> job_id); devices with an open job are skipped.
        Returns the devices that got a job."""
        made = []
        async with self._conn() as conn:
            for device_id, job_id in jobs.items():
                cur = await conn.execute(
                    "INSERT INTO firmware_jobs (job_id, release_id, device_id, created_by) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (job_id, release_id, device_id, user_id))
                if cur.rowcount == 1:
                    made.append(device_id)
        return made

    async def jobs(self, release_id: int | None = None, device_id: str | None = None,
                   open_only: bool = False) -> list[JobRecord]:
        sql, params = "SELECT * FROM firmware_jobs WHERE true", []
        if release_id is not None:
            sql += " AND release_id=%s"
            params.append(release_id)
        if device_id is not None:
            sql += " AND device_id=%s"
            params.append(device_id)
        if open_only:
            sql += " AND status IN ('pending', 'sent', 'downloading')"
        async with self._conn() as conn:
            rows = await (await conn.execute(sql + " ORDER BY created_at DESC",
                                             params)).fetchall()
        return [JobRecord.model_validate(r) for r in rows]

    async def advance_job(self, job_id: UUID, device_id: str, status: str, *,
                          version: str | None = None, error: str | None = None
                          ) -> JobRecord | None:
        """A device reports on its own job; statuses only move forward."""
        allowed = [s for s, n in JOB_ORDER.items() if n < JOB_ORDER[status]]
        async with self._conn() as conn:
            row = await (await conn.execute(
                "UPDATE firmware_jobs SET status=%s, updated_at=now(), "
                "reported_version=coalesce(%s, reported_version), error=coalesce(%s, error) "
                "WHERE job_id=%s AND device_id=%s AND status = ANY(%s) RETURNING *",
                (status, version, error, job_id, device_id, allowed))).fetchone()
        return JobRecord.model_validate(row) if row else None

    async def report_firmware(self, device_id: str, version: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE platform_devices SET firmware_version=%s, firmware_reported_at=now() "
                "WHERE device_id=%s AND firmware_version IS DISTINCT FROM %s",
                (version[:40], device_id, version[:40]))
