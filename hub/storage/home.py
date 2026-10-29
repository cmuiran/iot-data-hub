"""Home and maker extras (0016): homes, device groups, home settings (place, tariff), the SMS
fallback of a device, support tickets and warranty claims, maker branding, plans and invoices,
the maker's trend figures. Service code: backend checks the caller's organizations first."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from hub.storage.accounts import new_org_id


class GroupRecord(BaseModel):
    group_id: int
    org_id: str
    name: str
    icon: str | None = None
    device_ids: list[str]
    sections: list[dict[str, Any]] = []
    position: int = 0


class LabelRecord(BaseModel):
    device_id: str
    key: str
    name: str | None = None
    icon: str | None = None
    favorite: bool = False


class TicketRecord(BaseModel):
    ticket_id: int
    device_id: str
    identifier: str
    product_name: str
    maker_org_id: str
    owner_org_id: str
    kind: str
    subject: str
    status: str
    opened_by_name: str
    created_at: datetime
    updated_at: datetime
    messages: int = 0
    last_side: str | None = None


class MessageRecord(BaseModel):
    message_id: int
    side: str
    author_name: str
    body: str
    created_at: datetime


class PlanRecord(BaseModel):
    plan_id: str
    name: str
    monthly_fee: int
    included_devices: int
    per_device_fee: int
    max_devices: int | None = None


class InvoiceRecord(BaseModel):
    invoice_id: int
    org_id: str
    period: str
    plan_id: str
    active_devices: int
    amount: int
    status: str
    created_at: datetime
    paid_at: datetime | None = None


class HomeStore:
    def __init__(self, store: Any) -> None:
        self._store = store

    def _conn(self) -> Any:
        return self._store._conn()

    # --- homes --------------------------------------------------------------------------------

    async def create_home(self, user_id: int, name: str) -> str:
        """A new home (customer organization) the user owns."""
        org_id = new_org_id()
        async with self._conn() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO organizations (org_id, name, kind, source) "
                "VALUES (%s,%s,'customer','platform')", (org_id, name))
            await conn.execute(
                "INSERT INTO user_roles (user_id, role_id, org_id) VALUES (%s,'customer_owner',%s)",
                (user_id, org_id))
        return org_id

    async def rename_home(self, org_id: str, name: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE organizations SET name=%s WHERE org_id=%s AND kind='customer'",
                (name, org_id))

    # --- groups -------------------------------------------------------------------------------

    async def groups(self, org_ids: Sequence[str]) -> list[GroupRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM device_groups WHERE org_id = ANY(%s) ORDER BY position, name",
                (list(org_ids),))).fetchall()
        return [GroupRecord.model_validate(r) for r in rows]

    async def group(self, group_id: int) -> GroupRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT * FROM device_groups WHERE group_id=%s",
                                            (group_id,))).fetchone()
        return GroupRecord.model_validate(row) if row else None

    async def save_group(self, org_id: str, group_id: int | None, *, name: str,
                         icon: str | None, device_ids: list[str], position: int,
                         user_id: int | None,
                         sections: list[dict[str, Any]] | None = None) -> GroupRecord:
        parts = Jsonb(sections or [])
        async with self._conn() as conn:
            if group_id is None:
                row = await (await conn.execute(
                    "INSERT INTO device_groups (org_id, name, icon, device_ids, sections, "
                    "position, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (org_id, name, icon, device_ids, parts, position, user_id))).fetchone()
            else:
                row = await (await conn.execute(
                    "UPDATE device_groups SET name=%s, icon=%s, device_ids=%s, sections=%s, "
                    "position=%s, updated_at=now() WHERE group_id=%s AND org_id=%s RETURNING *",
                    (name, icon, device_ids, parts, position, group_id, org_id))).fetchone()
        return GroupRecord.model_validate(row)

    async def delete_group(self, org_id: str, group_id: int) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute("DELETE FROM device_groups WHERE group_id=%s AND org_id=%s",
                                     (group_id, org_id))
        return cur.rowcount > 0

    # --- home settings ------------------------------------------------------------------------

    async def settings(self, org_id: str) -> dict[str, Any]:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT latitude, longitude, radius_m, tariff FROM home_settings WHERE org_id=%s",
                (org_id,))).fetchone()
        return dict(row) if row else {"latitude": None, "longitude": None, "radius_m": 150,
                                      "tariff": {}}

    async def save_settings(self, org_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current = await self.settings(org_id)
        merged = current | values
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO home_settings (org_id, latitude, longitude, radius_m, tariff) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (org_id) DO UPDATE SET "
                "latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude, "
                "radius_m=EXCLUDED.radius_m, tariff=EXCLUDED.tariff, updated_at=now()",
                (org_id, merged["latitude"], merged["longitude"], merged["radius_m"],
                 Jsonb(merged["tariff"])))
        return merged

    # --- the home's names for outputs ----------------------------------------------------------

    async def labels(self, org_ids: Sequence[str], device_ids: Sequence[str] | None = None
                     ) -> dict[tuple[str, str], LabelRecord]:
        """(device_id, key) -> the home's name, icon and favourite mark."""
        sql = ("SELECT device_id, key, name, icon, favorite FROM output_labels "
               "WHERE org_id = ANY(%s)")
        args: list[Any] = [list(org_ids)]
        if device_ids is not None:
            sql += " AND device_id = ANY(%s)"
            args.append(list(device_ids))
        async with self._conn() as conn:
            rows = await (await conn.execute(sql, args)).fetchall()
        return {(r["device_id"], r["key"]): LabelRecord.model_validate(r) for r in rows}

    async def save_label(self, org_id: str, device_id: str, key: str, *, name: str | None,
                         icon: str | None, favorite: bool) -> None:
        async with self._conn() as conn:
            if name is None and icon is None and not favorite:
                await conn.execute("DELETE FROM output_labels WHERE org_id=%s AND device_id=%s "
                                   "AND key=%s", (org_id, device_id, key))
                return
            await conn.execute(
                "INSERT INTO output_labels (org_id, device_id, key, name, icon, favorite) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (org_id, device_id, key) DO UPDATE SET "
                "name=EXCLUDED.name, icon=EXCLUDED.icon, favorite=EXCLUDED.favorite, "
                "updated_at=now()", (org_id, device_id, key, name, icon, favorite))

    # --- SMS fallback -------------------------------------------------------------------------

    async def sms(self, device_id: str) -> dict[str, Any] | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT device_id, org_id, phone, key, updated_at FROM device_sms "
                "WHERE device_id=%s", (device_id,))).fetchone()
        return dict(row) if row else None

    async def save_sms(self, device_id: str, org_id: str, phone: str | None, key: str,
                       user_id: int | None) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO device_sms (device_id, org_id, phone, key, updated_by) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (device_id) DO UPDATE SET "
                "org_id=EXCLUDED.org_id, phone=EXCLUDED.phone, key=EXCLUDED.key, "
                "updated_by=EXCLUDED.updated_by, updated_at=now()",
                (device_id, org_id, phone, key, user_id))

    async def delete_sms(self, device_id: str) -> None:
        async with self._conn() as conn:
            await conn.execute("DELETE FROM device_sms WHERE device_id=%s", (device_id,))

    # --- tickets ------------------------------------------------------------------------------

    _TICKET_SELECT = (
        "SELECT t.*, pd.identifier, p.name AS product_name, u.display_name AS opened_by_name, "
        "(SELECT count(*) FROM ticket_messages m WHERE m.ticket_id = t.ticket_id) AS messages, "
        "(SELECT m.side FROM ticket_messages m WHERE m.ticket_id = t.ticket_id "
        " ORDER BY m.created_at DESC LIMIT 1) AS last_side "
        "FROM tickets t JOIN platform_devices pd USING (device_id) "
        "JOIN products p ON p.product_id = pd.product_id JOIN users u ON u.user_id = t.opened_by ")

    async def open_ticket(self, *, device_id: str, maker_org_id: str, owner_org_id: str,
                          user_id: int, author_name: str, kind: str, subject: str,
                          body: str) -> int:
        async with self._conn() as conn, conn.transaction():
            row = await (await conn.execute(
                "INSERT INTO tickets (device_id, maker_org_id, owner_org_id, opened_by, kind, "
                "subject) VALUES (%s,%s,%s,%s,%s,%s) RETURNING ticket_id",
                (device_id, maker_org_id, owner_org_id, user_id, kind, subject))).fetchone()
            await conn.execute(
                "INSERT INTO ticket_messages (ticket_id, side, author_id, author_name, body) "
                "VALUES (%s,'buyer',%s,%s,%s)", (row["ticket_id"], user_id, author_name, body))
        return int(row["ticket_id"])

    async def tickets(self, *, maker_orgs: Sequence[str] | None = None,
                      owner_orgs: Sequence[str] | None = None, device_id: str | None = None,
                      status: str | None = None, limit: int = 200) -> list[TicketRecord]:
        where, params = [], []
        if maker_orgs is not None:
            where.append("t.maker_org_id = ANY(%s)")
            params.append(list(maker_orgs))
        if owner_orgs is not None:
            where.append("t.owner_org_id = ANY(%s)")
            params.append(list(owner_orgs))
        if device_id is not None:
            where.append("t.device_id = %s")
            params.append(device_id)
        if status == "open":
            where.append("t.status NOT IN ('closed', 'rejected')")
        elif status is not None:
            where.append("t.status = %s")
            params.append(status)
        sql = self._TICKET_SELECT + ("WHERE " + " AND ".join(where) if where else "")
        async with self._conn() as conn:
            rows = await (await conn.execute(
                sql + " ORDER BY t.updated_at DESC LIMIT %s", (*params, limit))).fetchall()
        return [TicketRecord.model_validate(r) for r in rows]

    async def ticket(self, ticket_id: int) -> tuple[TicketRecord, list[MessageRecord]] | None:
        async with self._conn() as conn:
            row = await (await conn.execute(self._TICKET_SELECT + "WHERE t.ticket_id=%s",
                                            (ticket_id,))).fetchone()
            if row is None:
                return None
            msgs = await (await conn.execute(
                "SELECT message_id, side, author_name, body, created_at FROM ticket_messages "
                "WHERE ticket_id=%s ORDER BY created_at, message_id", (ticket_id,))).fetchall()
        return TicketRecord.model_validate(row), [MessageRecord.model_validate(m) for m in msgs]

    async def add_message(self, ticket_id: int, side: str, user_id: int, author_name: str,
                          body: str | None, status: str | None) -> None:
        async with self._conn() as conn, conn.transaction():
            if body:
                await conn.execute(
                    "INSERT INTO ticket_messages (ticket_id, side, author_id, author_name, body) "
                    "VALUES (%s,%s,%s,%s,%s)", (ticket_id, side, user_id, author_name, body))
            await conn.execute(
                "UPDATE tickets SET status=coalesce(%s, status), updated_at=now() "
                "WHERE ticket_id=%s", (status, ticket_id))

    # --- branding -----------------------------------------------------------------------------

    async def branding(self, org_id: str) -> dict[str, Any]:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT o.org_id, o.name, b.brand_name, b.accent, b.support_email, "
                "b.support_phone, (b.logo IS NOT NULL) AS has_logo, b.updated_at "
                "FROM organizations o LEFT JOIN org_branding b USING (org_id) "
                "WHERE o.org_id=%s", (org_id,))).fetchone()
        if row is None:
            return {}
        out = dict(row)
        out["has_logo"] = bool(out["has_logo"])
        out["display_name"] = out["brand_name"] or out["name"]
        return out

    async def save_branding(self, org_id: str, values: dict[str, Any]) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO org_branding (org_id, brand_name, accent, support_email, "
                "support_phone) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (org_id) DO UPDATE SET "
                "brand_name=EXCLUDED.brand_name, accent=EXCLUDED.accent, "
                "support_email=EXCLUDED.support_email, support_phone=EXCLUDED.support_phone, "
                "updated_at=now()",
                (org_id, values.get("brand_name"), values.get("accent"),
                 values.get("support_email"), values.get("support_phone")))

    async def set_logo(self, org_id: str, data: bytes | None, content_type: str | None) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO org_branding (org_id, logo, logo_type) VALUES (%s,%s,%s) "
                "ON CONFLICT (org_id) DO UPDATE SET logo=EXCLUDED.logo, "
                "logo_type=EXCLUDED.logo_type, updated_at=now()", (org_id, data, content_type))

    async def logo(self, org_id: str) -> tuple[bytes, str] | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT logo, logo_type FROM org_branding WHERE org_id=%s AND logo IS NOT NULL",
                (org_id,))).fetchone()
        return (bytes(row["logo"]), row["logo_type"]) if row else None

    # --- plans and invoices -------------------------------------------------------------------

    async def plans(self) -> list[PlanRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM plans ORDER BY monthly_fee, plan_id")).fetchall()
        return [PlanRecord.model_validate(r) for r in rows]

    async def org_plan(self, org_id: str) -> PlanRecord:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT p.* FROM org_plans o JOIN plans p USING (plan_id) WHERE o.org_id=%s",
                (org_id,))).fetchone()
            if row is None:
                row = await (await conn.execute(
                    "SELECT * FROM plans WHERE plan_id='starter'")).fetchone()
        return PlanRecord.model_validate(row)

    async def set_plan(self, org_id: str, plan_id: str, user_id: int | None) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO org_plans (org_id, plan_id, set_by) VALUES (%s,%s,%s) "
                "ON CONFLICT (org_id) DO UPDATE SET plan_id=EXCLUDED.plan_id, since=now(), "
                "set_by=EXCLUDED.set_by", (org_id, plan_id, user_id))

    async def active_devices(self, org_id: str) -> int:
        """Devices of the maker that a buyer has activated (what the plan charges for)."""
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT count(*) AS n FROM platform_devices WHERE maker_org_id=%s AND "
                "lifecycle='active' AND retired_at IS NULL", (org_id,))).fetchone()
        return int(row["n"])

    async def invoices(self, org_id: str) -> list[InvoiceRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM invoices WHERE org_id=%s ORDER BY period DESC", (org_id,))
            ).fetchall()
        return [InvoiceRecord.model_validate(r) for r in rows]

    async def issue_invoice(self, org_id: str, period: str, plan_id: str, devices: int,
                            amount: int) -> InvoiceRecord:
        """The month's invoice (made again while unpaid, so it follows the device count)."""
        async with self._conn() as conn:
            row = await (await conn.execute(
                "INSERT INTO invoices (org_id, period, plan_id, active_devices, amount) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (org_id, period) DO UPDATE SET "
                "plan_id=EXCLUDED.plan_id, active_devices=EXCLUDED.active_devices, "
                "amount=EXCLUDED.amount WHERE invoices.status='issued' RETURNING *",
                (org_id, period, plan_id, devices, amount))).fetchone()
            if row is None:
                row = await (await conn.execute(
                    "SELECT * FROM invoices WHERE org_id=%s AND period=%s",
                    (org_id, period))).fetchone()
        return InvoiceRecord.model_validate(row)

    async def set_invoice_status(self, invoice_id: int, status: str) -> InvoiceRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "UPDATE invoices SET status=%s, paid_at=CASE WHEN %s='paid' THEN now() END "
                "WHERE invoice_id=%s RETURNING *", (status, status, invoice_id))).fetchone()
        return InvoiceRecord.model_validate(row) if row else None

    # --- the maker's trends -------------------------------------------------------------------

    async def trend(self, org_ids: Sequence[str], product_id: Any | None, weeks: int,
                    now: datetime) -> dict[str, Any]:
        """Per week (oldest first): devices registered, activated by buyers, tickets opened;
        now: activated devices offline for more than three days ("faulty")."""
        start = now - timedelta(weeks=weeks)
        where, params = "maker_org_id = ANY(%s) AND retired_at IS NULL", [list(org_ids)]
        if product_id is not None:
            where += " AND product_id = %s"
            params.append(product_id)
        bucket = "date_bin('7 days', {col}, %s)"
        async with self._conn() as conn:
            async def series(col: str, table_where: str, extra: list[Any]) -> dict[str, int]:
                rows = await (await conn.execute(
                    f"SELECT {bucket.format(col=col)} AS w, count(*) AS n FROM "  # nosec B608
                    f"{table_where} AND {col} >= %s GROUP BY 1",
                    (start, *extra, start))).fetchall()
                return {r["w"].date().isoformat(): int(r["n"]) for r in rows}
            registered = await series("registered_at", "platform_devices WHERE " + where,
                                      params)
            activated = await series("activated_at", "platform_devices WHERE " + where, params)
            t_where = "tickets WHERE maker_org_id = ANY(%s)"
            t_params: list[Any] = [list(org_ids)]
            if product_id is not None:
                t_where += (" AND device_id IN (SELECT device_id FROM platform_devices WHERE "
                            "product_id = %s)")
                t_params.append(product_id)
            tickets = await series("created_at", t_where, t_params)
            faulty = (await (await conn.execute(
                "SELECT count(*) AS n FROM platform_devices pd JOIN device_status s "
                "USING (device_id) WHERE " + where.replace("maker_org_id", "pd.maker_org_id")
                .replace("retired_at", "pd.retired_at").replace("product_id", "pd.product_id")
                + " AND pd.lifecycle='active' AND (s.state IS DISTINCT FROM 'online') AND "
                "(s.last_seen IS NULL OR s.last_seen < %s)",  # nosec B608
                (*params, now - timedelta(days=3)))).fetchone())["n"]
        keys = sorted(set(registered) | set(activated) | set(tickets))
        return {"weeks": keys, "registered": [registered.get(k, 0) for k in keys],
                "activated": [activated.get(k, 0) for k in keys],
                "tickets": [tickets.get(k, 0) for k in keys], "faulty_now": int(faulty)}

    # --- account deletion ---------------------------------------------------------------------

    async def anonymize_user(self, user_id: int) -> None:
        """What is left of a deleted account: no name, e-mail or password, no roles; the
        audit log keeps its user id."""
        async with self._conn() as conn, conn.transaction():
            await conn.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
            await conn.execute(
                "UPDATE users SET is_active=false, email=NULL, email_verified_at=NULL, "
                "display_name='—', username=%s, password_hash='!' WHERE user_id=%s",
                (f"deleted.{user_id}", user_id))
