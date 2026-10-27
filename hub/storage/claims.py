"""Claim store (0011): claim codes, device credentials, ownership changes, edge-hub children.

Service code only (backend.claims checks who may do what): hub_tenant has no privilege on the
code and credential tables. Ownership changes act on a device and every device behind it
(an edge hub moves together with its children).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel


class ClaimTarget(BaseModel):
    device_id: str
    identifier: str
    lifecycle: str
    retired: bool
    parent_device_id: str | None = None
    product_kind: str
    code_hash: str | None = None
    purpose: str | None = None
    failed_attempts: int = 0
    locked_until: datetime | None = None
    used_at: datetime | None = None
    owner_org: str | None = None


class ClaimStore:
    def __init__(self, store: Any) -> None:
        self._store = store

    def _conn(self) -> Any:
        return self._store._conn()

    async def issue_codes(self, codes: dict[str, str], purpose: str,
                          created_by: int | None) -> None:
        """device_id -> code hash; replaces any earlier code (which stops working)."""
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO device_claim_codes (device_id, code_hash, purpose, created_by) "
                "SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::int[]) "
                "ON CONFLICT (device_id) DO UPDATE SET code_hash=EXCLUDED.code_hash, "
                "purpose=EXCLUDED.purpose, created_by=EXCLUDED.created_by, created_at=now(), "
                "failed_attempts=0, locked_until=NULL, used_at=NULL",
                (list(codes), list(codes.values()), [purpose] * len(codes),
                 [created_by] * len(codes)))

    async def target(self, identifier: str) -> ClaimTarget | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT pd.device_id, pd.identifier, pd.lifecycle, pd.parent_device_id, "
                "(pd.retired_at IS NOT NULL) AS retired, p.kind AS product_kind, c.code_hash, "
                "c.purpose, coalesce(c.failed_attempts, 0) AS failed_attempts, c.locked_until, "
                "c.used_at, (SELECT a.org_id FROM device_access a WHERE a.device_id = "
                "pd.device_id AND a.level = 'owner' AND a.revoked_at IS NULL) AS owner_org "
                "FROM platform_devices pd JOIN products p USING (product_id) "
                "LEFT JOIN device_claim_codes c USING (device_id) "
                "WHERE upper(pd.identifier) = upper(%s)", (identifier,))).fetchone()
        return ClaimTarget.model_validate(row) if row else None

    async def record_failure(self, device_id: str, now: datetime, max_attempts: int,
                             lock_minutes: int) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE device_claim_codes SET failed_attempts = failed_attempts + 1, "
                "locked_until = CASE WHEN failed_attempts + 1 >= %s THEN %s ELSE locked_until "
                "END WHERE device_id=%s",
                (max_attempts, now + timedelta(minutes=lock_minutes), device_id))

    async def complete_claim(self, device_id: str, org_id: str, user_id: int | None,
                             now: datetime) -> bool:
        """Use the code (once) and make org_id the owner of the device and its children;
        every earlier access (owner, shares) ends. False if the code was used meanwhile."""
        async with self._conn() as conn, conn.transaction():
            row = await (await conn.execute(
                "UPDATE device_claim_codes SET used_at=%s, failed_attempts=0, locked_until=NULL "
                "WHERE device_id=%s AND used_at IS NULL RETURNING device_id",
                (now, device_id))).fetchone()
            if row is None:
                return False
            ids = await self._family(conn, device_id)
            await self._revoke_all(conn, ids, now)
            await conn.execute(
                "INSERT INTO device_access (device_id, org_id, level, granted_by) "
                "SELECT unnest(%s::text[]), %s, 'owner', %s", (ids, org_id, user_id))
            await conn.execute(
                "UPDATE platform_devices SET lifecycle='active', updated_at=now() "
                "WHERE device_id = ANY(%s)", (ids,))
        return True

    async def release(self, device_id: str, lifecycle: str, now: datetime) -> list[str]:
        """End every access to the device and its children; returns the devices concerned."""
        async with self._conn() as conn, conn.transaction():
            ids = await self._family(conn, device_id)
            await self._revoke_all(conn, ids, now)
            await conn.execute(
                "UPDATE platform_devices SET lifecycle=%s, updated_at=now() "
                "WHERE device_id = ANY(%s)", (lifecycle, ids))
            await conn.execute(
                "UPDATE invitations SET revoked_at=%s WHERE device_id = ANY(%s) AND "
                "accepted_at IS NULL AND revoked_at IS NULL", (now, ids))
        return ids

    @staticmethod
    async def _family(conn: Any, device_id: str) -> list[str]:
        rows = await (await conn.execute(
            "WITH RECURSIVE f(device_id) AS (SELECT %s::text UNION SELECT pd.device_id FROM "
            "platform_devices pd JOIN f ON pd.parent_device_id = f.device_id WHERE "
            "pd.retired_at IS NULL) SELECT device_id FROM f", (device_id,))).fetchall()
        return [r["device_id"] for r in rows]

    @staticmethod
    async def _revoke_all(conn: Any, ids: list[str], now: datetime) -> None:
        await conn.execute("UPDATE device_access SET revoked_at=%s WHERE device_id = ANY(%s) "
                           "AND revoked_at IS NULL", (now, ids))

    async def retire(self, device_id: str, now: datetime) -> None:
        """A device removed for good (an edge-hub child): history stays, it never runs again."""
        async with self._conn() as conn, conn.transaction():
            await self._revoke_all(conn, [device_id], now)
            await conn.execute("UPDATE platform_devices SET retired_at=%s, updated_at=now() "
                               "WHERE device_id=%s", (now, device_id))
            await conn.execute("UPDATE devices SET enabled=false, retired_at=%s, "
                               "updated_at=now() WHERE device_id=%s", (now, device_id))
            await conn.execute("UPDATE tags SET retired_at=%s WHERE device_id=%s AND "
                               "retired_at IS NULL", (now, device_id))

    async def disable(self, device_ids: Sequence[str]) -> None:
        async with self._conn() as conn:
            await conn.execute("UPDATE devices SET enabled=false, updated_at=now() WHERE "
                               "device_id = ANY(%s) AND source='platform'", (list(device_ids),))

    async def children(self, hub_id: str) -> list[str]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT device_id FROM platform_devices WHERE parent_device_id=%s AND "
                "retired_at IS NULL ORDER BY identifier", (hub_id,))).fetchall()
        return [r["device_id"] for r in rows]

    async def next_child_identifier(self, hub_identifier: str) -> str:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT identifier FROM platform_devices WHERE identifier LIKE %s",
                (hub_identifier.replace("%", "") + "-%",))).fetchall()
        taken = {r["identifier"] for r in rows}
        n = 1
        while f"{hub_identifier}-{n:03d}" in taken:
            n += 1
        return f"{hub_identifier}-{n:03d}"

    # --- device credentials -------------------------------------------------------------------

    async def issue_credentials(self, hashes: dict[str, str], created_by: int | None) -> None:
        """device_id -> secret hash; the previous credential of each device is revoked."""
        async with self._conn() as conn, conn.transaction():
            await conn.execute(
                "UPDATE device_credentials SET revoked_at=now() WHERE device_id = ANY(%s) AND "
                "revoked_at IS NULL", (list(hashes),))
            await conn.execute(
                "INSERT INTO device_credentials (device_id, secret_hash, created_by) "
                "SELECT * FROM unnest(%s::text[], %s::text[], %s::int[])",
                (list(hashes), list(hashes.values()), [created_by] * len(hashes)))

    async def credential_valid(self, device_id: str, secret_hash: str) -> bool:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT 1 AS ok FROM device_credentials c JOIN platform_devices pd "
                "USING (device_id) WHERE c.device_id=%s AND c.secret_hash=%s AND "
                "c.revoked_at IS NULL AND pd.retired_at IS NULL AND pd.lifecycle <> 'disabled'",
                (device_id, secret_hash))).fetchone()
        return row is not None
