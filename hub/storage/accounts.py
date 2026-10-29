"""Account store (0010): self-registered users, one-time tokens, personal organizations,
memberships, invitations and device shares.

As in hub.storage.platform, methods taking ``orgs`` run as hub_tenant limited to those
organizations (row-level security); the others are service code, called by
backend.accounts after its own checks (a token proves an e-mail address, an invitation is
looked up by its token before anyone is a member).
"""

from __future__ import annotations

import base64
import secrets
from datetime import datetime
from typing import Any

import psycopg
from pydantic import BaseModel

from hub.storage.platform import PlatformError, _as_tenant
from hub.storage.records import UserRecord

# roles that can manage an organization's members; one must remain
MANAGER_ROLES = ("customer_owner", "manufacturer_admin")


class MembershipRecord(BaseModel):
    user_role_id: int
    org_id: str
    name: str
    kind: str
    role_id: str
    personal: bool


class MemberRecord(BaseModel):
    user_role_id: int
    user_id: int
    display_name: str
    email: str | None = None
    username: str
    role_id: str
    granted_at: datetime


class InvitationRecord(BaseModel):
    invitation_id: int
    kind: str
    org_id: str
    device_id: str | None = None
    role_id: str | None = None
    level: str | None = None
    email: str
    invited_by: int | None = None
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    org_name: str | None = None


class ShareRecord(BaseModel):
    access_id: int
    device_id: str
    org_id: str
    org_name: str | None = None
    level: str
    granted_at: datetime


def new_org_id() -> str:
    """``o`` + 16 base32 characters: a valid segment, not guessable."""
    return "o" + base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()


class AccountStore:
    def __init__(self, store: Any) -> None:
        self._store = store

    def _conn(self) -> Any:
        return self._store._conn()

    # --- users and tokens ---------------------------------------------------------------------

    async def create_user(self, *, username: str, display_name: str, password_hash: str,
                          email: str, language: str, verified_at: datetime | None
                          ) -> UserRecord:
        try:
            async with self._conn() as conn:
                row = await (await conn.execute(
                    "INSERT INTO users (username, display_name, password_hash, "
                    "must_change_password, language, email, email_verified_at, "
                    "password_changed_at) VALUES (%s,%s,%s,false,%s,%s,%s,now()) RETURNING *",
                    (username, display_name, password_hash, language, email, verified_at)
                )).fetchone()
        except psycopg.errors.UniqueViolation:
            raise PlatformError("this e-mail address has an account", "email_taken") from None
        return UserRecord.model_validate(row)

    async def add_token(self, user_id: int, purpose: str, token_hash: str,
                        expires_at: datetime) -> None:
        """A new token; earlier unused tokens of the same purpose stop working."""
        async with self._conn() as conn, conn.transaction():
            await conn.execute(
                "UPDATE account_tokens SET used_at=now() WHERE user_id=%s AND purpose=%s "
                "AND used_at IS NULL", (user_id, purpose))
            await conn.execute(
                "INSERT INTO account_tokens (token_hash, user_id, purpose, expires_at) "
                "VALUES (%s,%s,%s,%s)", (token_hash, user_id, purpose, expires_at))

    async def use_token(self, token_hash: str, purpose: str, now: datetime) -> int | None:
        """The user of a valid token, which is used up; None if unknown, used or expired."""
        async with self._conn() as conn:
            row = await (await conn.execute(
                "UPDATE account_tokens SET used_at=%s WHERE token_hash=%s AND purpose=%s AND "
                "used_at IS NULL AND expires_at > %s RETURNING user_id",
                (now, token_hash, purpose, now))).fetchone()
        return row["user_id"] if row else None

    async def restore_token(self, token_hash: str) -> None:
        """Undo :meth:`use_token` (the action it was used for was refused)."""
        async with self._conn() as conn:
            await conn.execute("UPDATE account_tokens SET used_at=NULL WHERE token_hash=%s",
                               (token_hash,))

    async def mark_verified(self, user_id: int, now: datetime) -> None:
        async with self._conn() as conn:
            await conn.execute("UPDATE users SET email_verified_at=coalesce(email_verified_at,"
                               " %s) WHERE user_id=%s", (now, user_id))

    # --- organizations and members ------------------------------------------------------------

    async def ensure_personal_org(self, user: UserRecord, name: str) -> str:
        """The user's own organization (kind customer), owned by the user; made once."""
        async with self._conn() as conn, conn.transaction():
            row = await (await conn.execute(
                "SELECT org_id FROM organizations WHERE personal_user_id=%s",
                (user.user_id,))).fetchone()
            if row is not None:
                return row["org_id"]
            org_id = new_org_id()
            await conn.execute(
                "INSERT INTO organizations (org_id, name, kind, source, personal_user_id) "
                "VALUES (%s,%s,'customer','platform',%s)", (org_id, name, user.user_id))
            await conn.execute(
                "INSERT INTO user_roles (user_id, role_id, org_id) VALUES (%s,'customer_owner',%s)",
                (user.user_id, org_id))
        return org_id

    async def personal_org(self, user_id: int) -> str | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT org_id FROM organizations WHERE personal_user_id=%s",
                (user_id,))).fetchone()
        return row["org_id"] if row else None

    async def organization(self, org_id: str) -> dict[str, Any] | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT org_id, name, kind, source, personal_user_id FROM organizations "
                "WHERE org_id=%s AND retired_at IS NULL", (org_id,))).fetchone()
        return dict(row) if row else None

    async def memberships(self, user_id: int) -> list[MembershipRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT r.user_role_id, o.org_id, o.name, o.kind, r.role_id, "
                "(o.personal_user_id IS NOT DISTINCT FROM r.user_id) AS personal "
                "FROM user_roles r JOIN organizations o USING (org_id) WHERE r.user_id=%s "
                "AND o.retired_at IS NULL ORDER BY personal DESC, o.name", (user_id,)
            )).fetchall()
        return [MembershipRecord.model_validate(r) for r in rows]

    async def members(self, org_id: str) -> list[MemberRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT r.user_role_id, u.user_id, u.display_name, u.email, u.username, "
                "r.role_id, r.granted_at FROM user_roles r JOIN users u USING (user_id) "
                "WHERE r.org_id=%s AND u.is_active ORDER BY u.display_name, r.role_id",
                (org_id,))).fetchall()
        return [MemberRecord.model_validate(r) for r in rows]

    async def add_member(self, user_id: int, org_id: str, role_id: str,
                         granted_by: int | None) -> bool:
        """False if the user already has this role there."""
        async with self._conn() as conn:
            cur = await conn.execute(
                "INSERT INTO user_roles (user_id, role_id, org_id, granted_by) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (user_id, role_id, org_id, granted_by))
            return cur.rowcount == 1

    async def remove_member(self, org_id: str, user_role_id: int) -> MemberRecord:
        """Remove one grant; the last member who can manage the organization stays."""
        async with self._conn() as conn, conn.transaction():
            await conn.execute("SELECT org_id FROM organizations WHERE org_id=%s FOR UPDATE",
                               (org_id,))
            rows = await (await conn.execute(
                "SELECT r.user_role_id, u.user_id, u.display_name, u.email, u.username, "
                "r.role_id, r.granted_at FROM user_roles r JOIN users u USING (user_id) "
                "WHERE r.org_id=%s", (org_id,))).fetchall()
            target = next((r for r in rows if r["user_role_id"] == user_role_id), None)
            if target is None:
                raise PlatformError("member not found", "not_found")
            managers = {r["user_id"] for r in rows if r["role_id"] in MANAGER_ROLES
                        and r["user_role_id"] != user_role_id}
            if target["role_id"] in MANAGER_ROLES and not managers:
                raise PlatformError("the organization needs at least one owner",
                                    "last_owner")
            personal = await (await conn.execute(
                "SELECT 1 FROM organizations WHERE org_id=%s AND personal_user_id=%s",
                (org_id, target["user_id"]))).fetchone()
            if personal is not None and target["role_id"] in MANAGER_ROLES:
                raise PlatformError("the owner of a personal organization cannot be removed",
                                    "personal_owner")
            await conn.execute("DELETE FROM user_roles WHERE user_role_id=%s", (user_role_id,))
        return MemberRecord.model_validate(target)

    # --- invitations --------------------------------------------------------------------------

    async def create_invitation(self, orgs: set[str] | None, *, kind: str, org_id: str,
                                email: str, token_hash: str, expires_at: datetime,
                                invited_by: int | None, role_id: str | None = None,
                                device_id: str | None = None, level: str | None = None
                                ) -> InvitationRecord:
        try:
            async with self._conn() as conn, conn.transaction():
                if orgs is not None:
                    await _as_tenant(conn, orgs)
                row = await (await conn.execute(
                    "INSERT INTO invitations (kind, org_id, device_id, role_id, level, email, "
                    "token_hash, invited_by, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "RETURNING *", (kind, org_id, device_id, role_id, level, email, token_hash,
                                    invited_by, expires_at))).fetchone()
        except psycopg.errors.InsufficientPrivilege:      # RLS: not the caller's org/device
            raise PlatformError("not found", "not_found") from None
        return InvitationRecord.model_validate(row)

    async def invitations(self, orgs: set[str] | None, org_id: str, *,
                          device_id: str | None = None) -> list[InvitationRecord]:
        sql = ("SELECT * FROM invitations WHERE org_id=%s AND accepted_at IS NULL AND "
               "revoked_at IS NULL AND expires_at > now()")
        params: list[Any] = [org_id]
        if device_id is not None:
            sql += " AND device_id=%s"
            params.append(device_id)
        async with self._conn() as conn, conn.transaction():
            if orgs is not None:
                await _as_tenant(conn, orgs)
            rows = await (await conn.execute(sql + " ORDER BY created_at DESC",
                                             params)).fetchall()
        return [InvitationRecord.model_validate(r) for r in rows]

    async def revoke_invitation(self, orgs: set[str] | None, org_id: str,
                                invitation_id: int) -> bool:
        async with self._conn() as conn, conn.transaction():
            if orgs is not None:
                await _as_tenant(conn, orgs)
            cur = await conn.execute(
                "UPDATE invitations SET revoked_at=now() WHERE invitation_id=%s AND org_id=%s "
                "AND accepted_at IS NULL AND revoked_at IS NULL", (invitation_id, org_id))
            return cur.rowcount == 1

    async def invitation_by_token(self, token_hash: str, now: datetime
                                  ) -> InvitationRecord | None:
        """An open invitation (not accepted, revoked or expired), with its org's name."""
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT i.*, o.name AS org_name FROM invitations i JOIN organizations o "
                "USING (org_id) WHERE i.token_hash=%s AND i.accepted_at IS NULL AND "
                "i.revoked_at IS NULL AND i.expires_at > %s", (token_hash, now))).fetchone()
        return InvitationRecord.model_validate(row) if row else None

    async def pending_for(self, email: str, now: datetime) -> list[InvitationRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT i.*, o.name AS org_name FROM invitations i JOIN organizations o "
                "USING (org_id) WHERE lower(i.email)=lower(%s) AND i.accepted_at IS NULL AND "
                "i.revoked_at IS NULL AND i.expires_at > %s ORDER BY i.created_at DESC",
                (email, now))).fetchall()
        return [InvitationRecord.model_validate(r) for r in rows]

    async def accept_invitation(self, invitation: InvitationRecord, user_id: int,
                                target_org: str, now: datetime) -> bool:
        """Apply an open invitation once: membership, or the device share for target_org."""
        async with self._conn() as conn, conn.transaction():
            row = await (await conn.execute(
                "UPDATE invitations SET accepted_at=%s, accepted_by=%s, accepted_org=%s "
                "WHERE invitation_id=%s AND accepted_at IS NULL AND revoked_at IS NULL AND "
                "expires_at > %s RETURNING invitation_id",
                (now, user_id, target_org, invitation.invitation_id, now))).fetchone()
            if row is None:
                return False
            if invitation.kind == "organization":
                await conn.execute(
                    "INSERT INTO user_roles (user_id, role_id, org_id, granted_by) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, invitation.role_id, target_org, invitation.invited_by))
                return True
            owner = await (await conn.execute(
                "SELECT org_id FROM device_access WHERE device_id=%s AND level='owner' AND "
                "revoked_at IS NULL", (invitation.device_id,))).fetchone()
            if owner is None or owner["org_id"] != invitation.org_id:
                raise PlatformError("the device is no longer owned by the organization that "
                                    "shared it", "share_invalid")
            if target_org == invitation.org_id:
                raise PlatformError("the device already belongs to this organization",
                                    "share_invalid")
            await conn.execute(
                "UPDATE device_access SET revoked_at=%s WHERE device_id=%s AND org_id=%s AND "
                "level <> 'owner' AND revoked_at IS NULL",
                (now, invitation.device_id, target_org))
            await conn.execute(
                "INSERT INTO device_access (device_id, org_id, level, granted_by) "
                "VALUES (%s,%s,%s,%s)",
                (invitation.device_id, target_org, invitation.level, invitation.invited_by))
        return True

    # --- devices of an organization and their shares ------------------------------------------

    async def set_owner(self, device_id: str, org_id: str, granted_by: int | None) -> None:
        """Service: make ``org_id`` the device's owner (the claim of phase 3 builds on this)."""
        async with self._conn() as conn, conn.transaction():
            await conn.execute(
                "UPDATE device_access SET revoked_at=now() WHERE device_id=%s AND "
                "revoked_at IS NULL", (device_id,))
            await conn.execute(
                "INSERT INTO device_access (device_id, org_id, level, granted_by) "
                "VALUES (%s,%s,'owner',%s)", (device_id, org_id, granted_by))
            await conn.execute("UPDATE platform_devices SET lifecycle='active', "
                               "updated_at=now() WHERE device_id=%s", (device_id,))

    async def access_levels(self, orgs: set[str], device_ids: list[str]) -> dict[str, str]:
        """The caller's best level per device (owner > operator > viewer), under RLS."""
        async with self._conn() as conn, conn.transaction():
            await _as_tenant(conn, orgs)
            rows = await (await conn.execute(
                "SELECT device_id, level FROM device_access WHERE device_id = ANY(%s) AND "
                "revoked_at IS NULL AND org_id = ANY(app_org_ids())",
                (device_ids,))).fetchall()
        rank = {"owner": 3, "operator": 2, "viewer": 1}
        out: dict[str, str] = {}
        for r in rows:
            if rank[r["level"]] > rank.get(out.get(r["device_id"], ""), 0):
                out[r["device_id"]] = r["level"]
        return out

    async def rooms(self, orgs: set[str], device_ids: list[str]) -> dict[str, str]:
        """The room each device has in the caller's organizations (the owner's room first)."""
        async with self._conn() as conn, conn.transaction():
            await _as_tenant(conn, orgs)
            rows = await (await conn.execute(
                "SELECT device_id, room FROM device_access WHERE device_id = ANY(%s) AND "
                "revoked_at IS NULL AND room IS NOT NULL AND org_id = ANY(app_org_ids()) "
                "ORDER BY (level = 'owner') DESC", (device_ids,))).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            out.setdefault(r["device_id"], r["room"])
        return out

    async def set_room(self, orgs: set[str], device_id: str, room: str | None) -> bool:
        """Put the device in a room of the caller's organizations that have access to it
        (service write after the RLS check); False if none of them has access."""
        async with self._conn() as conn, conn.transaction():
            await _as_tenant(conn, orgs)
            rows = await (await conn.execute(
                "SELECT access_id FROM device_access WHERE device_id=%s AND revoked_at IS NULL "
                "AND org_id = ANY(app_org_ids())", (device_id,))).fetchall()
            if not rows:
                return False
            await conn.execute("RESET ROLE")
            await conn.execute("UPDATE device_access SET room=%s WHERE access_id = ANY(%s)",
                               (room, [r["access_id"] for r in rows]))
        return True

    async def owner_org(self, orgs: set[str] | None, device_id: str) -> str | None:
        """The owner organization of the device, if it is one of the caller's (RLS; with
        ``orgs`` None, whoever owns it)."""
        sql = ("SELECT org_id FROM device_access WHERE device_id=%s AND level='owner' AND "
               "revoked_at IS NULL")
        async with self._conn() as conn, conn.transaction():
            if orgs is not None:
                await _as_tenant(conn, orgs)
                sql += " AND org_id = ANY(app_org_ids())"
            row = await (await conn.execute(sql, (device_id,))).fetchone()
        return row["org_id"] if row else None

    async def shares(self, orgs: set[str] | None, device_id: str) -> list[ShareRecord]:
        """Active shares of a device the caller owns (the owner row excluded)."""
        async with self._conn() as conn, conn.transaction():
            if orgs is not None:
                await _as_tenant(conn, orgs)
            rows = await (await conn.execute(
                "SELECT access_id, device_id, org_id, level, granted_at FROM device_access "
                "WHERE device_id=%s AND level <> 'owner' AND revoked_at IS NULL "
                "ORDER BY granted_at", (device_id,))).fetchall()
        if not rows:
            return []
        async with self._conn() as conn:
            names = {r["org_id"]: r["name"] for r in await (await conn.execute(
                "SELECT org_id, name FROM organizations WHERE org_id = ANY(%s)",
                ([r["org_id"] for r in rows],))).fetchall()}
        return [ShareRecord.model_validate(dict(r) | {"org_name": names.get(r["org_id"])})
                for r in rows]

    async def revoke_share(self, orgs: set[str] | None, device_id: str, access_id: int
                           ) -> ShareRecord | None:
        """The owner revokes a share, or the receiving organization gives it up (RLS)."""
        async with self._conn() as conn, conn.transaction():
            if orgs is not None:
                await _as_tenant(conn, orgs)
            row = await (await conn.execute(
                "UPDATE device_access SET revoked_at=now() WHERE access_id=%s AND device_id=%s "
                "AND level <> 'owner' AND revoked_at IS NULL RETURNING access_id, device_id, "
                "org_id, level, granted_at", (access_id, device_id))).fetchone()
        return ShareRecord.model_validate(row) if row else None
