"""Platform store: organizations, products, template versions, platform devices (0009).

Every tenant-facing method takes ``orgs``: the caller's organizations, or None for service code
and platform administrators. With a set, the queries run as the database role ``hub_tenant``
with ``app.org_ids`` set for the transaction, so row-level security limits them to those
organizations even if a query here forgot a condition (ADR-004). Runtime tables (devices, tags,
device_status) are written as the application role, after the tenant-checked write of the
platform row in the same transaction.
"""

from __future__ import annotations

import calendar
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError, computed_field

from hub.models import AnyDeviceModel, TagBase
from hub.platform.devices import (
    PLATFORM_AREA,
    PLATFORM_SITE,
    ProductIn,
    build_device,
    new_device_id,
)
from hub.platform.template import ProductTemplate, build_tags, calc_device, calc_rows

log = logging.getLogger(__name__)

TENANT_ROLE = "hub_tenant"
MAKER_LIFECYCLE = ("registered", "ready", "sold", "disabled")   # "active" comes from a claim


def warranty_end(activated_at: datetime | None, months: int | None) -> datetime | None:
    """When the warranty ends: ``months`` calendar months after the first activation."""
    if activated_at is None or months is None:
        return None
    month = activated_at.month - 1 + months
    year, month = activated_at.year + month // 12, month % 12 + 1
    day = min(activated_at.day, calendar.monthrange(year, month)[1])
    return activated_at.replace(year=year, month=month, day=day)


class PlatformError(ValueError):
    """A request the platform refuses; ``code`` is the API error code."""

    def __init__(self, message: str, code: str = "invalid",
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


class NotVisible(PlatformError):
    """Unknown, or belongs to another organization (answered like unknown)."""

    def __init__(self, what: str) -> None:
        super().__init__(f"{what} not found", "not_found")


class OrganizationRecord(BaseModel):
    org_id: str
    name: str
    kind: str
    source: str
    created_at: datetime


class ProductRecord(BaseModel):
    product_id: UUID
    org_id: str
    code: str
    name: str
    category: str | None = None
    description: str | None = None
    image_ref: str | None = None
    identifier_pattern: str
    kind: str = "device"
    catalog: bool = False
    created_at: datetime
    updated_at: datetime
    latest_published: int | None = None
    draft: int | None = None
    devices: int = 0
    has_image: bool = False


class VersionRecord(BaseModel):
    product_id: UUID
    version: int
    status: str
    definition: dict[str, Any]
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None = None
    devices: int = 0


class PlatformDeviceRecord(BaseModel):
    device_id: str
    identifier: str
    serial_number: str | None = None
    product_id: UUID
    product_code: str
    version: int
    maker_org_id: str
    name: str
    protocol: str
    enabled: bool
    lifecycle: str
    batch: str | None = None
    warranty_months: int | None = None
    connection: dict[str, Any] = {}
    options: dict[str, Any] = {}
    registered_at: datetime
    parent_device_id: str | None = None      # behind this edge hub
    firmware_version: str | None = None      # as the device last reported it
    firmware_reported_at: datetime | None = None
    activated_at: datetime | None = None      # first claim by a buyer; the warranty starts
    product_kind: str = "device"
    state: str | None = None
    last_seen: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def warranty_until(self) -> datetime | None:
        return warranty_end(self.activated_at, self.warranty_months)


# Visible platform rows (row-level security applies in tenant transactions) ...
_PD_SELECT = (
    "SELECT pd.device_id, pd.identifier, pd.serial_number, pd.product_id, p.code AS "
    "product_code, pd.version, pd.maker_org_id, pd.lifecycle, pd.batch, pd.warranty_months, "
    "pd.options, pd.registered_at, pd.parent_device_id, p.kind AS product_kind, "
    "pd.firmware_version, pd.firmware_reported_at, pd.activated_at "
    "FROM platform_devices pd JOIN products p USING (product_id) ")
# ... and their runtime part, read as the application role for exactly those devices
# (hub_tenant has no privilege on devices / device_status).
_RUNTIME_SELECT = (
    "SELECT d.device_id, d.name, d.protocol, d.enabled, d.connection, s.state, s.last_seen "
    "FROM devices d LEFT JOIN device_status s USING (device_id) WHERE d.device_id = ANY(%s)")


class PlatformStore:
    def __init__(self, store: Any) -> None:
        self._store = store            # hub.storage.postgres.PostgresStore (its pool)
        self._templates: dict[tuple[UUID, int], ProductTemplate] = {}

    # --- transactions -------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _tx(self, orgs: set[str] | None) -> AsyncIterator[Any]:
        """A transaction; with ``orgs`` it runs as hub_tenant limited to those organizations."""
        async with self._store._conn() as conn, conn.transaction():
            if orgs is not None:
                await _as_tenant(conn, orgs)
            yield conn

    # --- organizations ------------------------------------------------------------------------

    async def create_organization(self, org_id: str, name: str, kind: str) -> OrganizationRecord:
        try:
            async with self._tx(None) as conn:
                row = await (await conn.execute(
                    "INSERT INTO organizations (org_id, name, kind, source) "
                    "VALUES (%s,%s,%s,'platform') RETURNING *", (org_id, name, kind))).fetchone()
        except psycopg.errors.UniqueViolation:
            raise PlatformError(f"organization {org_id} exists", "conflict") from None
        return OrganizationRecord.model_validate(row)

    async def organizations(self, orgs: set[str] | None) -> list[OrganizationRecord]:
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(
                "SELECT * FROM organizations WHERE retired_at IS NULL ORDER BY org_id"
            )).fetchall()
        return [OrganizationRecord.model_validate(r) for r in rows]

    async def organization_ids(self) -> set[str]:
        async with self._tx(None) as conn:
            rows = await (await conn.execute(
                "SELECT org_id FROM organizations WHERE retired_at IS NULL")).fetchall()
        return {r["org_id"] for r in rows}

    # --- products -----------------------------------------------------------------------------

    async def create_product(self, orgs: set[str] | None, org_id: str, product: ProductIn,
                             user_id: int | None) -> ProductRecord:
        try:
            async with self._tx(orgs) as conn:
                row = await (await conn.execute(
                    "INSERT INTO products (org_id, code, name, category, description, image_ref,"
                    " identifier_pattern, kind, catalog, created_by) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING product_id",
                    (org_id, product.code, product.name, product.category, product.description,
                     product.image_ref, product.identifier_pattern, product.kind,
                     product.catalog, user_id))).fetchone()
        except psycopg.errors.UniqueViolation:
            raise PlatformError(f"product code {product.code} exists", "conflict") from None
        except psycopg.errors.ForeignKeyViolation:
            raise NotVisible(f"organization {org_id}") from None
        except psycopg.errors.InsufficientPrivilege:        # RLS: not one of the caller's orgs
            raise NotVisible(f"organization {org_id}") from None
        return await self.product(orgs, row["product_id"])

    async def products(self, orgs: set[str] | None, org_id: str | None = None
                       ) -> list[ProductRecord]:
        sql, params = self._product_sql(), []
        if org_id is not None:
            sql += " WHERE p.org_id = %s"
            params.append(org_id)
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(sql + " ORDER BY p.org_id, p.code",
                                             params)).fetchall()
        return [ProductRecord.model_validate(r) for r in rows]

    async def product(self, orgs: set[str] | None, product_id: UUID) -> ProductRecord:
        async with self._tx(orgs) as conn:
            row = await (await conn.execute(self._product_sql() + " WHERE p.product_id = %s",
                                            (product_id,))).fetchone()
        if row is None:
            raise NotVisible("product")
        return ProductRecord.model_validate(row)

    async def update_product(self, orgs: set[str] | None, product_id: UUID,
                             changes: dict[str, Any]) -> ProductRecord:
        """Name, category, description, kind, catalog; the identifier pattern only while the
        product has no devices (identifiers already printed on labels never change)."""
        allowed = {"name", "category", "description", "kind", "catalog", "identifier_pattern"}
        bad = sorted(set(changes) - allowed)
        if bad:
            raise PlatformError(f"cannot change {', '.join(bad)}")
        if not changes:
            return await self.product(orgs, product_id)
        current = await self.product(orgs, product_id)
        pattern = changes.get("identifier_pattern", current.identifier_pattern)
        if current.devices and pattern != current.identifier_pattern:
            raise PlatformError("the identifier pattern is fixed once devices are registered",
                                "conflict")
        columns = sorted(changes)               # names from the allow-list above
        assignments = ", ".join(f"{c}=%s" for c in columns)
        try:
            async with self._tx(orgs) as conn:
                row = await (await conn.execute(
                    f"UPDATE products SET {assignments}, updated_at=now() "  # nosec B608
                    "WHERE product_id=%s RETURNING product_id",
                    (*[changes[c] for c in columns], product_id))).fetchone()
        except psycopg.errors.InsufficientPrivilege:
            raise NotVisible("product") from None
        if row is None:
            raise NotVisible("product")
        return await self.product(orgs, product_id)

    async def set_image(self, orgs: set[str] | None, product_id: UUID,
                        image: bytes | None, content_type: str | None) -> None:
        try:
            async with self._tx(orgs) as conn:
                row = await (await conn.execute(
                    "UPDATE products SET image=%s, image_type=%s, updated_at=now() "
                    "WHERE product_id=%s RETURNING product_id",
                    (image, content_type, product_id))).fetchone()
        except psycopg.errors.InsufficientPrivilege:
            raise NotVisible("product") from None
        if row is None:
            raise NotVisible("product")

    async def image(self, orgs: set[str] | None, product_id: UUID) -> tuple[bytes, str] | None:
        async with self._tx(orgs) as conn:
            row = await (await conn.execute(
                "SELECT image, image_type FROM products WHERE product_id=%s",
                (product_id,))).fetchone()
        if row is None:
            raise NotVisible("product")
        return (bytes(row["image"]), row["image_type"]) if row["image"] is not None else None

    async def stats(self, orgs: set[str] | None, product_id: UUID | None = None
                    ) -> dict[str, Any]:
        """Counts for the maker's dashboard: by lifecycle, online now, firmware versions,
        warranty (devices whose warranty ends within 30 days)."""
        where, params = "pd.retired_at IS NULL AND pd.parent_device_id IS NULL", []
        if product_id is not None:
            where += " AND pd.product_id = %s"
            params.append(product_id)
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(
                "SELECT pd.device_id, pd.lifecycle, pd.firmware_version, pd.activated_at, "
                "pd.warranty_months FROM platform_devices pd WHERE " + where,  # nosec B608
                params)).fetchall()
        ids = [r["device_id"] for r in rows]
        online: set[str] = set()
        if ids:
            async with self._tx(None) as conn:
                online = {r["device_id"] for r in await (await conn.execute(
                    "SELECT device_id FROM device_status WHERE device_id = ANY(%s) AND "
                    "state='online'", (ids,))).fetchall()}
        by_lifecycle = dict.fromkeys(("registered", "ready", "sold", "active", "disabled"), 0)
        firmware: dict[str, int] = {}
        expiring = 0
        now = datetime.now(UTC)
        for r in rows:
            by_lifecycle[r["lifecycle"]] = by_lifecycle.get(r["lifecycle"], 0) + 1
            if r["firmware_version"]:
                firmware[r["firmware_version"]] = firmware.get(r["firmware_version"], 0) + 1
            end = warranty_end(r["activated_at"], r["warranty_months"])
            if end is not None and now <= end <= now + timedelta(days=30):
                expiring += 1
        return {"total": len(rows), "lifecycle": by_lifecycle, "online": len(online),
                "firmware": firmware, "warranty_expiring": expiring}

    @staticmethod
    def _product_sql() -> str:
        return ("SELECT p.product_id, p.org_id, p.code, p.name, p.category, p.description, "
                "p.image_ref, p.identifier_pattern, p.kind, p.catalog, p.created_at, "
                "p.updated_at, (p.image IS NOT NULL) AS has_image, "
                "(SELECT max(v.version) FROM product_versions v WHERE v.product_id = "
                "p.product_id AND v.status = 'published') AS latest_published, "
                "(SELECT v.version FROM product_versions v WHERE v.product_id = p.product_id "
                "AND v.status = 'draft') AS draft, (SELECT count(*) FROM platform_devices d "
                "WHERE d.product_id = p.product_id) AS devices FROM products p")

    # --- template versions --------------------------------------------------------------------

    async def save_draft(self, orgs: set[str] | None, product_id: UUID,
                         template: ProductTemplate, notes: str | None, user_id: int | None
                         ) -> VersionRecord:
        """The product's draft version: updated if there is one, else the next version."""
        definition = Jsonb(template.model_dump(mode="json", exclude_defaults=True))
        async with self._tx(orgs) as conn:
            await self._lock_product(conn, product_id)
            row = await (await conn.execute(
                "UPDATE product_versions SET definition=%s, notes=%s, updated_at=now() "
                "WHERE product_id=%s AND status='draft' RETURNING version",
                (definition, notes, product_id))).fetchone()
            if row is None:
                row = await (await conn.execute(
                    "INSERT INTO product_versions (product_id, version, definition, notes, "
                    "created_by) SELECT %s, coalesce(max(version), 0) + 1, %s, %s, %s "
                    "FROM product_versions WHERE product_id=%s RETURNING version",
                    (product_id, definition, notes, user_id, product_id))).fetchone()
        return await self.version(orgs, product_id, row["version"])

    async def publish(self, orgs: set[str] | None, product_id: UUID, version: int,
                      user_id: int | None) -> VersionRecord:
        async with self._tx(orgs) as conn:
            await self._lock_product(conn, product_id)
            row = await (await conn.execute(
                "UPDATE product_versions SET status='published', published_at=now(), "
                "published_by=%s, updated_at=now() WHERE product_id=%s AND version=%s AND "
                "status='draft' RETURNING version", (user_id, product_id, version))).fetchone()
        if row is None:
            current = await self.version(orgs, product_id, version)     # 404 if not visible
            raise PlatformError(f"version {version} is {current.status}, not a draft",
                                "not_draft")
        return await self.version(orgs, product_id, version)

    async def _lock_product(self, conn: Any, product_id: UUID) -> None:
        """Only the maker may change versions (RLS); serialize version numbering."""
        row = await (await conn.execute(
            "SELECT product_id FROM products WHERE product_id=%s FOR UPDATE",
            (product_id,))).fetchone()
        if row is None:
            raise NotVisible("product")

    async def versions(self, orgs: set[str] | None, product_id: UUID) -> list[VersionRecord]:
        await self.product(orgs, product_id)
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(
                self._version_sql() + " WHERE v.product_id=%s ORDER BY v.version DESC",
                (product_id,))).fetchall()
        return [VersionRecord.model_validate(r) for r in rows]

    async def version(self, orgs: set[str] | None, product_id: UUID, version: int
                      ) -> VersionRecord:
        async with self._tx(orgs) as conn:
            row = await (await conn.execute(
                self._version_sql() + " WHERE v.product_id=%s AND v.version=%s",
                (product_id, version))).fetchone()
        if row is None:
            raise NotVisible("product version")
        return VersionRecord.model_validate(row)

    @staticmethod
    def _version_sql() -> str:
        return ("SELECT v.*, (SELECT count(*) FROM platform_devices d WHERE d.product_id = "
                "v.product_id AND d.version = v.version) AS devices FROM product_versions v")

    async def template(self, product_id: UUID, version: int) -> ProductTemplate:
        """The (validated, cached) template of a version; service use."""
        key = (product_id, version)
        if key not in self._templates:
            record = await self.version(None, product_id, version)
            template = ProductTemplate.model_validate(record.definition)
            if record.status == "draft":
                return template           # drafts change: never cached
            self._templates[key] = template
        return self._templates[key]

    # --- devices ------------------------------------------------------------------------------

    async def register_devices(self, orgs: set[str] | None, product_id: UUID, version: int, *,
                               protocol: str, entries: Sequence[tuple[str, str | None]],
                               batch: str | None, warranty_months: int | None,
                               connection: dict[str, Any], options: dict[str, Any],
                               enabled: bool, user_id: int | None,
                               parent_device_id: str | None = None,
                               names: Sequence[str] | None = None
                               ) -> list[PlatformDeviceRecord]:
        """Register devices of a published version: (identifier, serial) per device. Each gets
        its own generated device_id and its own tags. All or nothing."""
        identifiers = [i for i, _ in entries]
        serials = [s for _, s in entries if s is not None]
        for label, values in (("identifiers", identifiers), ("serial numbers", serials)):
            repeated = sorted({v for v in values if values.count(v) > 1})
            if repeated:
                raise PlatformError(f"repeated {label} in the request: {', '.join(repeated[:20])}",
                                    "duplicate", {"values": repeated[:20]})
        record = await self.version(orgs, product_id, version)
        if record.status != "published":
            raise PlatformError(f"version {version} is {record.status}: register devices of a "
                                "published version", "not_published")
        product = await self.product(orgs, product_id)
        template = await self.template(product_id, version)
        try:
            tags = (build_tags(template, protocol) + calc_rows(template)
                    if protocol in template.bindings else None)
        except ValueError as exc:
            raise PlatformError(str(exc)) from None
        if tags is None:
            raise PlatformError(f"the product has no {protocol} binding "
                                f"(it has {', '.join(sorted(template.bindings))})", "no_binding")
        ids = [new_device_id() for _ in entries]
        names = list(names) if names else [f"{product.name} {ident}" for ident in identifiers]
        if enabled:            # a running device needs a complete, valid configuration now
            for device_id, (ident, serial), name in zip(ids, entries, names, strict=True):
                self._build(template, device_id, name, protocol, connection, options, ident,
                            serial or "", enabled=True)
        await self._check_free(identifiers, product_id, serials)
        try:
            async with self._tx(orgs) as conn:
                await conn.execute(
                    "INSERT INTO platform_devices (device_id, product_id, version, maker_org_id, "
                    "identifier, serial_number, batch, warranty_months, options, registered_by, "
                    "parent_device_id) SELECT e.device_id, %s, %s, %s, e.identifier, e.serial, "
                    "%s, %s, %s, %s, %s FROM unnest(%s::text[], %s::text[], %s::text[]) AS "
                    "e(device_id, identifier, serial)",
                    (product_id, version, product.org_id, batch, warranty_months, Jsonb(options),
                     user_id, parent_device_id, ids, identifiers, [s for _, s in entries]))
                await _as_service(conn)
                await self._insert_runtime_rows(conn, ids, names, protocol, enabled, connection,
                                                tags)
        except psycopg.errors.UniqueViolation:     # a concurrent registration took one
            await self._check_free(identifiers, product_id, serials)
            raise
        except psycopg.errors.InsufficientPrivilege:
            raise NotVisible("product") from None
        log.info("registered %d devices of %s v%d", len(ids), product.code, version)
        return await self.devices_by_id(orgs, ids)

    async def _check_free(self, identifiers: list[str], product_id: UUID,
                          serials: list[str]) -> None:
        async with self._tx(None) as conn:
            taken = [r["identifier"] for r in await (await conn.execute(
                "SELECT identifier FROM platform_devices WHERE upper(identifier) = "
                "ANY(%s) ORDER BY identifier LIMIT 20", ([i.upper() for i in identifiers],)
            )).fetchall()]
            used = [r["serial_number"] for r in await (await conn.execute(
                "SELECT serial_number FROM platform_devices WHERE product_id=%s AND "
                "serial_number = ANY(%s) ORDER BY serial_number LIMIT 20",
                (product_id, serials))).fetchall()]
        if taken:
            raise PlatformError(f"identifiers already registered: {', '.join(taken)}",
                                "duplicate", {"values": taken})
        if used:
            raise PlatformError(f"serial numbers already registered: {', '.join(used)}",
                                "duplicate", {"values": used})

    @staticmethod
    async def _insert_runtime_rows(conn: Any, ids: list[str], names: list[str], protocol: str,
                                   enabled: bool, connection: dict[str, Any],
                                   tags: list[TagBase]) -> None:
        from hub.storage.postgres import _address

        await conn.execute(
            "INSERT INTO devices (device_id, name, site, area, protocol, enabled, connection, "
            "source) SELECT e.device_id, e.name, %s, %s, %s, %s, %s, 'platform' "
            "FROM unnest(%s::text[], %s::text[]) AS e(device_id, name)",
            (PLATFORM_SITE, PLATFORM_AREA, protocol, enabled, Jsonb(connection), ids, names))
        n = len(ids)
        columns: list[list[Any]] = [[] for _ in range(12)]
        for t in tags:
            values = (t.name, t.description, str(t.data_type), t.unit, t.scale, t.offset,
                      str(t.access), t.min, t.max, Jsonb(_address(t)),
                      t.write_class and str(t.write_class))
            columns[0].extend(ids)
            for i, v in enumerate(values, start=1):
                columns[i].extend([v] * n)
        await conn.execute(
            "INSERT INTO tags (device_id, name, description, data_type, unit, scale, \"offset\", "
            "access, min_value, max_value, address, write_class) SELECT * FROM unnest("
            "%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::float8[], "
            "%s::float8[], %s::text[], %s::float8[], %s::float8[], %s::jsonb[], %s::text[])",
            tuple(columns))
        await conn.execute("INSERT INTO device_status (device_id) SELECT unnest(%s::text[])",
                           (ids,))

    async def devices(self, orgs: set[str] | None, *, product_id: UUID | None = None,
                      lifecycle: str | None = None, search: str | None = None,
                      batch: str | None = None, limit: int = 100, offset: int = 0
                      ) -> tuple[list[PlatformDeviceRecord], int]:
        where, params = ["pd.retired_at IS NULL"], []
        if product_id is not None:
            where.append("pd.product_id = %s")
            params.append(product_id)
        if lifecycle == "stock":                 # not with a buyer yet: registered or ready
            where.append("pd.lifecycle IN ('registered', 'ready')")
        elif lifecycle is not None:
            where.append("pd.lifecycle = %s")
            params.append(lifecycle)
        if batch is not None:
            where.append("pd.batch = %s")
            params.append(batch)
        if search:
            where.append("(upper(pd.identifier) LIKE %s OR pd.serial_number LIKE %s)")
            like = search.upper().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            params += [f"%{like}%", f"%{search}%"]
        clause = " WHERE " + " AND ".join(where)      # constant conditions, values as params
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(
                _PD_SELECT + clause + " ORDER BY pd.identifier LIMIT %s OFFSET %s",
                (*params, limit, offset))).fetchall()
            count_sql = "SELECT count(*) AS n FROM platform_devices pd" + clause  # nosec B608
            total = (await (await conn.execute(count_sql, params)).fetchone())["n"]
        return await self._with_runtime(rows), total

    async def devices_by_id(self, orgs: set[str] | None, ids: Sequence[str]
                            ) -> list[PlatformDeviceRecord]:
        async with self._tx(orgs) as conn:
            rows = await (await conn.execute(
                _PD_SELECT + "WHERE pd.device_id = ANY(%s) ORDER BY pd.identifier",
                (list(ids),))).fetchall()
        return await self._with_runtime(rows)

    async def _with_runtime(self, rows: list[dict[str, Any]]) -> list[PlatformDeviceRecord]:
        """Complete visible platform rows (in their order) with name, protocol, state, ..."""
        if not rows:
            return []
        async with self._tx(None) as conn:
            runtime = {r["device_id"]: r for r in await (await conn.execute(
                _RUNTIME_SELECT, ([r["device_id"] for r in rows],))).fetchall()}
        return [PlatformDeviceRecord.model_validate(runtime[r["device_id"]] | r) for r in rows
                if r["device_id"] in runtime]

    async def device(self, orgs: set[str] | None, device_id: str) -> PlatformDeviceRecord:
        found = await self.devices_by_id(orgs, [device_id])
        if not found:
            raise NotVisible("device")
        return found[0]

    async def update_device(self, orgs: set[str] | None, device_id: str, *,
                            name: str | None = None, connection: dict[str, Any] | None = None,
                            options: dict[str, Any] | None = None, enabled: bool | None = None,
                            lifecycle: str | None = None, warranty_months: int | None = None
                            ) -> tuple[PlatformDeviceRecord, AnyDeviceModel | None]:
        """Change a device (maker only); returns it and its device model when enabled."""
        current = await self.device(orgs, device_id)
        if lifecycle is not None and lifecycle not in MAKER_LIFECYCLE:
            raise PlatformError(f"lifecycle {lifecycle} cannot be set here "
                                f"({', '.join(MAKER_LIFECYCLE)})", "invalid_lifecycle")
        name = current.name if name is None else name
        connection = current.connection if connection is None else connection
        options = current.options if options is None else options
        enabled = current.enabled if enabled is None else enabled
        model = None
        if enabled:
            template = await self.template(current.product_id, current.version)
            model = self._build(template, device_id, name, current.protocol, connection, options,
                                current.identifier, current.serial_number or "", enabled=True)
        try:
            async with self._tx(orgs) as conn:
                row = await (await conn.execute(
                    "UPDATE platform_devices SET options=%s, lifecycle=coalesce(%s, lifecycle), "
                    "warranty_months=coalesce(%s, warranty_months), updated_at=now() "
                    "WHERE device_id=%s RETURNING device_id",
                    (Jsonb(options), lifecycle, warranty_months, device_id))).fetchone()
                if row is None:
                    raise NotVisible("device")
                await _as_service(conn)
                await conn.execute(
                    "UPDATE devices SET name=%s, connection=%s, enabled=%s, updated_at=now() "
                    "WHERE device_id=%s AND source='platform'",
                    (name, Jsonb(connection), enabled, device_id))
        except psycopg.errors.InsufficientPrivilege:
            raise NotVisible("device") from None
        return await self.device(orgs, device_id), model

    async def set_lifecycle(self, orgs: set[str] | None, product_id: UUID, lifecycle: str, *,
                            batch: str | None = None, device_ids: Sequence[str] | None = None
                            ) -> int:
        """Move a batch (or these devices) of a product to a maker stage; devices a buyer has
        activated are left alone (their stage comes from the claim). Returns how many moved."""
        if lifecycle not in MAKER_LIFECYCLE:
            raise PlatformError(f"lifecycle {lifecycle} cannot be set here", "invalid_lifecycle")
        if batch is None and not device_ids:
            raise PlatformError("give a batch or devices", "invalid")
        where, params = ["product_id = %s", "lifecycle <> 'active'", "retired_at IS NULL"], \
            [product_id]
        if batch is not None:
            where.append("batch = %s")
            params.append(batch)
        if device_ids:
            where.append("device_id = ANY(%s)")
            params.append(list(device_ids))
        try:
            async with self._tx(orgs) as conn:
                await self._lock_product(conn, product_id)
                cur = await conn.execute(
                    "UPDATE platform_devices SET lifecycle=%s, updated_at=now() WHERE "  # nosec B608
                    + " AND ".join(where), (lifecycle, *params))
                return cur.rowcount
        except psycopg.errors.InsufficientPrivilege:
            raise NotVisible("product") from None

    async def calc_device(self, device_id: str) -> Any | None:
        """The device's calculated tags (from its template version) as a hub calc device with
        the device's own id; None if its template has none (service use)."""
        found = await self.devices_by_id(None, [device_id])
        if not found:
            return None
        record = found[0]
        template = await self.template(record.product_id, record.version)
        return calc_device(template, device_id, record.name)

    async def device_models(self, device_ids: Sequence[str] | None = None
                            ) -> list[AnyDeviceModel]:
        """Enabled platform devices (all, or these) as device models (for the hub; service
        use). A device whose configuration no longer validates is skipped with a log error."""
        sql, params = (_PD_SELECT + "JOIN devices d USING (device_id) WHERE d.enabled AND "
                       "pd.retired_at IS NULL AND d.retired_at IS NULL"), []
        if device_ids is not None:
            sql += " AND pd.device_id = ANY(%s)"
            params.append(list(device_ids))
        async with self._tx(None) as conn:
            rows = await (await conn.execute(sql + " ORDER BY pd.device_id", params)).fetchall()
        out: list[AnyDeviceModel] = []
        for rec in await self._with_runtime(rows):
            try:
                template = await self.template(rec.product_id, rec.version)
                out.append(self._build(template, rec.device_id, rec.name, rec.protocol,
                                       rec.connection, rec.options, rec.identifier,
                                       rec.serial_number or "", enabled=True))
            except PlatformError as exc:
                log.error("platform device %s (%s) not started: %s", rec.identifier,
                          rec.device_id, exc)
        return out

    @staticmethod
    def _build(template: ProductTemplate, device_id: str, name: str, protocol: str,
               connection: dict[str, Any], options: dict[str, Any], identifier: str,
               serial: str, *, enabled: bool) -> AnyDeviceModel:
        try:
            return build_device(device_id=device_id, name=name, template=template,
                                protocol=protocol, connection=connection, options=options,
                                identifier=identifier, serial=serial, enabled=enabled)
        except ValidationError as exc:
            err = exc.errors()[0]
            where = ".".join(str(p) for p in err.get("loc", ()))
            raise PlatformError(f"device configuration: {where}: {err.get('msg')}",
                                "invalid_device") from None
        except ValueError as exc:
            raise PlatformError(str(exc), "invalid_device") from None


async def _as_tenant(conn: Any, orgs: set[str]) -> None:
    await conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
    await conn.execute("SELECT set_config('app.org_ids', %s, true)", (",".join(sorted(orgs)),))


async def _as_service(conn: Any) -> None:
    """Back to the application role for the runtime rows of the same transaction."""
    await conn.execute("RESET ROLE")
    await conn.execute("SELECT set_config('app.org_ids', '', true)")
