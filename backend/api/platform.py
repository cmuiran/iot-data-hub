"""/api/platform: organizations, products, template versions and manufactured devices.

Organization permissions (backend.rbac): ``product.read`` to see an organization's products and
devices, ``product.manage`` to define products and publish versions, ``device.register`` to
register and change devices; ``org.manage`` (global) creates organizations. Every query runs
limited to the caller's organizations (hub.storage.platform, row-level security), so another
organization's product or device answers like an unknown one (404).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError, model_validator

from backend.deps import ServicesDep, client_addr, require
from backend.errors import ApiError, Conflict, Forbidden, Invalid, NotFound
from backend.rbac import Principal
from hub.models import SEGMENT_PATTERN
from hub.platform.devices import (
    MAX_BATCH,
    ProductIn,
    identifiers,
    validate_identifier,
)
from hub.platform.template import ProductTemplate
from hub.storage.accounts import InvitationRecord, MemberRecord
from hub.storage.commands import CommandRecord
from hub.storage.platform import (
    NotVisible,
    OrganizationRecord,
    PlatformDeviceRecord,
    PlatformError,
    ProductRecord,
    VersionRecord,
)

router = APIRouter(prefix="/api/platform", tags=["platform"])
OrgAdmin = Annotated[Principal, Depends(require("org.manage"))]
Reader = Annotated[Principal, Depends(require("product.read"))]
Manager = Annotated[Principal, Depends(require("product.manage"))]
Registrar = Annotated[Principal, Depends(require("device.register"))]
Member = Annotated[Principal, Depends(require("org.members"))]
Viewer = Annotated[Principal, Depends(require("device.view"))]
Sharer = Annotated[Principal, Depends(require("device.share"))]
Signed = Annotated[Principal, Depends(require("self"))]


def _orgs(p: Principal, permission: str) -> set[str] | None:
    return p.orgs_with(permission)


def _api_error(exc: PlatformError) -> ApiError:
    if isinstance(exc, NotVisible) or exc.code == "not_found":
        return NotFound(str(exc))
    if exc.code in ("conflict", "duplicate", "not_draft", "not_published"):
        return Conflict(str(exc), code=exc.code, extra=exc.extra)
    return Invalid(str(exc), code=exc.code, extra=exc.extra)


# --- organizations ----------------------------------------------------------------------------


class OrganizationIn(BaseModel):
    org_id: str = Field(pattern=f"^{SEGMENT_PATTERN}$")
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["manufacturer", "customer", "platform"]


@router.post("/organizations", status_code=201)
async def create_organization(body: OrganizationIn, svc: ServicesDep, p: OrgAdmin,
                              request: Request) -> OrganizationRecord:
    try:
        org = await svc.store.platform.create_organization(body.org_id, body.name, body.kind)
    except PlatformError as exc:
        raise _api_error(exc) from None
    await svc.store.add_auth_audit("organization_created", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org.org_id, "kind": org.kind})
    return org


@router.get("/organizations")
async def list_organizations(svc: ServicesDep, p: Reader) -> list[OrganizationRecord]:
    return await svc.store.platform.organizations(_orgs(p, "product.read"))


# --- products ---------------------------------------------------------------------------------


class ProductCreate(ProductIn):
    org_id: str = Field(pattern=f"^{SEGMENT_PATTERN}$")


@router.get("/products")
async def list_products(svc: ServicesDep, p: Reader,
                        org: Annotated[str | None, Query(pattern=f"^{SEGMENT_PATTERN}$")] = None
                        ) -> list[ProductRecord]:
    return await svc.store.platform.products(_orgs(p, "product.read"), org)


@router.post("/products", status_code=201)
async def create_product(body: ProductCreate, svc: ServicesDep, p: Manager,
                         request: Request) -> ProductRecord:
    if not p.can_in_org("product.manage", body.org_id):
        raise Forbidden(f"missing permission product.manage in {body.org_id}")
    product = ProductIn.model_validate(body.model_dump(exclude={"org_id"}))
    try:
        record = await svc.store.platform.create_product(
            _orgs(p, "product.manage"), body.org_id, product, p.user_id)
    except PlatformError as exc:
        raise _api_error(exc) from None
    await svc.store.add_auth_audit("product_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"product_id": str(record.product_id),
                                           "code": record.code, "org_id": record.org_id})
    return record


@router.get("/products/{product_id}")
async def get_product(product_id: UUID, svc: ServicesDep, p: Reader) -> ProductRecord:
    try:
        return await svc.store.platform.product(_orgs(p, "product.read"), product_id)
    except PlatformError as exc:
        raise _api_error(exc) from None


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    kind: Literal["device", "edge_hub"] | None = None
    catalog: bool | None = None
    identifier_pattern: str | None = Field(default=None, min_length=3, max_length=64)


@router.patch("/products/{product_id}")
async def update_product(product_id: UUID, body: ProductPatch, svc: ServicesDep, p: Manager,
                         request: Request) -> ProductRecord:
    """The product's details (the code never changes; the identifier pattern only until the
    first device is registered). Empty category / description clear them."""
    changes = body.model_dump(exclude_unset=True)
    for key in ("name", "kind", "catalog", "identifier_pattern"):
        if key in changes and changes[key] is None:
            del changes[key]
    for key in ("category", "description"):
        if key in changes:
            changes[key] = (changes[key] or "").strip() or None
    if "identifier_pattern" in changes:
        try:                              # the same rule as on creation
            ProductIn.model_validate({"code": "X0", "name": "x",
                                      "identifier_pattern": changes["identifier_pattern"]})
        except ValidationError as exc:
            raise Invalid(exc.errors()[0]["msg"], code="invalid_pattern") from None
    try:
        record = await svc.store.platform.update_product(_orgs(p, "product.manage"),
                                                         product_id, changes)
    except PlatformError as exc:
        raise _api_error(exc) from None
    await svc.store.add_auth_audit("product_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"product_id": str(product_id),
                                           "changed": sorted(changes)})
    return record


IMAGE_MAGIC = {"image/png": b"\x89PNG", "image/jpeg": b"\xff\xd8\xff", "image/webp": b"RIFF"}
MAX_IMAGE = 512 * 1024


@router.put("/products/{product_id}/image", status_code=204)
async def put_product_image(product_id: UUID, request: Request, svc: ServicesDep,
                            p: Manager) -> None:
    """The product picture: the raw PNG / JPEG / WebP bytes (at most 512 KB) as the body."""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    body = await request.body()
    magic = IMAGE_MAGIC.get(content_type)
    webp_ok = content_type != "image/webp" or body[8:12] == b"WEBP"
    if magic is None or not body.startswith(magic) or not webp_ok:
        raise Invalid("send a PNG, JPEG or WebP image", code="invalid_image")
    if len(body) > MAX_IMAGE:
        raise Invalid("the image is larger than 512 KB", code="image_too_large")
    try:
        await svc.store.platform.set_image(_orgs(p, "product.manage"), product_id, body,
                                           content_type)
    except PlatformError as exc:
        raise _api_error(exc) from None


@router.delete("/products/{product_id}/image", status_code=204)
async def delete_product_image(product_id: UUID, svc: ServicesDep, p: Manager) -> None:
    try:
        await svc.store.platform.set_image(_orgs(p, "product.manage"), product_id, None, None)
    except PlatformError as exc:
        raise _api_error(exc) from None


@router.get("/products/{product_id}/image")
async def get_product_image(product_id: UUID, svc: ServicesDep, p: Signed) -> Response:
    """The picture, for the maker and for whoever has a device of the product (RLS)."""
    orgs = p.orgs_with("product.read")
    if orgs is not None:
        orgs = orgs | (p.orgs_with("device.view") or set())
        orgs |= {m.org_id for m in await svc.accounts.memberships(p)}
    try:
        found = await svc.store.platform.image(orgs if orgs is None or orgs else {"-"},
                                               product_id)
    except PlatformError as exc:
        raise _api_error(exc) from None
    if found is None:
        raise NotFound("the product has no picture", code="no_image")
    data, content_type = found
    return Response(data, media_type=content_type,
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/stats")
async def stats(svc: ServicesDep, p: Reader, product: UUID | None = None) -> dict[str, Any]:
    """The maker's dashboard: devices by lifecycle, online now, firmware versions, warranty."""
    return await svc.store.platform.stats(_orgs(p, "product.read"), product)


@router.get("/protocols")
async def protocols(p: Signed) -> dict[str, Any]:
    """Per protocol, the fields a binding gives: connection defaults and per-tag addresses
    (from the protocol's own models, so the forms of the maker panel and of the app follow
    the hub's validation). The same for everyone: no data of any organization."""
    return _protocol_fields()


# --- template versions ------------------------------------------------------------------------


class DraftIn(BaseModel):
    definition: dict[str, Any]
    notes: str | None = Field(default=None, max_length=2000)


@router.get("/products/{product_id}/versions")
async def list_versions(product_id: UUID, svc: ServicesDep, p: Reader) -> list[VersionRecord]:
    try:
        return await svc.store.platform.versions(_orgs(p, "product.read"), product_id)
    except PlatformError as exc:
        raise _api_error(exc) from None


@router.get("/products/{product_id}/versions/{version}")
async def get_version(product_id: UUID, version: int, svc: ServicesDep, p: Reader
                      ) -> VersionRecord:
    try:
        return await svc.store.platform.version(_orgs(p, "product.read"), product_id, version)
    except PlatformError as exc:
        raise _api_error(exc) from None


@router.put("/products/{product_id}/draft")
async def save_draft(product_id: UUID, body: DraftIn, svc: ServicesDep, p: Manager,
                     request: Request) -> VersionRecord:
    """Create or replace the product's draft version (validated like device YAML)."""
    try:
        template = ProductTemplate.model_validate(body.definition)
    except ValidationError as exc:
        raise Invalid("invalid product template", code="invalid_template",
                      extra={"details": [{"loc": list(e["loc"]), "msg": e["msg"]}
                                         for e in exc.errors()[:20]]}) from None
    try:
        record = await svc.store.platform.save_draft(
            _orgs(p, "product.manage"), product_id, template, body.notes, p.user_id)
    except PlatformError as exc:
        raise _api_error(exc) from None
    await svc.store.add_auth_audit("product_version_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"product_id": str(product_id),
                                           "version": record.version})
    return record


@router.post("/products/{product_id}/versions/{version}/publish")
async def publish_version(product_id: UUID, version: int, svc: ServicesDep, p: Manager,
                          request: Request) -> VersionRecord:
    try:
        record = await svc.store.platform.publish(_orgs(p, "product.manage"), product_id,
                                                  version, p.user_id)
    except PlatformError as exc:
        raise _api_error(exc) from None
    await svc.store.add_auth_audit("product_version_published", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"product_id": str(product_id), "version": version})
    return record


# --- devices ----------------------------------------------------------------------------------


class DeviceEntry(BaseModel):
    identifier: str
    serial_number: str | None = Field(default=None, min_length=1, max_length=64)


def _parse_csv(text: str) -> list[DeviceEntry]:
    import csv as csv_module

    rows = [r for r in csv_module.reader(text.splitlines()) if r and any(c.strip() for c in r)]
    if rows and rows[0][0].strip().lower() == "identifier":
        rows = rows[1:]
    if not 1 <= len(rows) <= MAX_BATCH:
        raise ValueError(f"the CSV must have 1-{MAX_BATCH} devices")
    return [DeviceEntry(identifier=r[0].strip(),
                        serial_number=(r[1].strip() or None) if len(r) > 1 else None)
            for r in rows]


class RegisterIn(BaseModel):
    """Either a range from the product's identifier pattern (start, count) or a list."""

    version: int = Field(ge=1)
    protocol: str = Field(pattern=f"^{SEGMENT_PATTERN}$")
    start: int | None = Field(default=None, ge=0)
    count: int | None = Field(default=None, ge=1, le=MAX_BATCH)
    devices: list[DeviceEntry] | None = Field(default=None, min_length=1, max_length=MAX_BATCH)
    csv: str | None = Field(default=None, min_length=1, max_length=2_000_000,
                            description="identifier[,serial_number] per line, header optional")
    batch: str | None = Field(default=None, min_length=1, max_length=64)
    warranty_months: int | None = Field(default=None, ge=0, le=240)
    connection: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False

    @model_validator(mode="after")
    def _one_way(self) -> RegisterIn:
        if self.csv is not None:
            if self.devices is not None:
                raise ValueError("give devices or csv, not both")
            object.__setattr__(self, "devices", _parse_csv(self.csv))
            object.__setattr__(self, "csv", None)
        ranged = self.start is not None or self.count is not None
        if ranged == (self.devices is not None):
            raise ValueError("give either start + count, devices or csv")
        if ranged and (self.start is None or self.count is None):
            raise ValueError("start and count go together")
        return self


class RegisterOut(BaseModel):
    registered: int
    first: str
    last: str
    devices: list[PlatformDeviceRecord]


@router.post("/products/{product_id}/devices", status_code=201)
async def register_devices(product_id: UUID, body: RegisterIn, svc: ServicesDep, p: Registrar,
                           request: Request) -> RegisterOut:
    orgs = _orgs(p, "device.register")
    try:
        product = await svc.store.platform.product(orgs, product_id)
        if body.devices is not None:
            entries = [(validate_identifier(d.identifier), d.serial_number)
                       for d in body.devices]
        else:                               # start and count are both set (RegisterIn)
            entries = [(i, None) for i in identifiers(product.identifier_pattern,
                                                      body.start or 0, body.count or 0)]
    except PlatformError as exc:
        raise _api_error(exc) from None
    except ValueError as exc:
        raise Invalid(str(exc), code="invalid_identifier") from None
    try:
        records = await svc.store.platform.register_devices(
            orgs, product_id, body.version, protocol=body.protocol, entries=entries,
            batch=body.batch, warranty_months=body.warranty_months, connection=body.connection,
            options=body.options, enabled=body.enabled, user_id=p.user_id)
    except PlatformError as exc:
        raise _api_error(exc) from None
    if body.enabled:
        await _apply(svc, [r.device_id for r in records])
    await svc.store.add_auth_audit(
        "devices_registered", p.username, user_id=p.user_id, client_addr=client_addr(request),
        detail={"product_id": str(product_id), "version": body.version, "count": len(records),
                "first": entries[0][0], "last": entries[-1][0], "protocol": body.protocol})
    return RegisterOut(registered=len(records), first=entries[0][0], last=entries[-1][0],
                       devices=records[:100])


class RangeIn(BaseModel):
    start: int = Field(ge=0)
    count: int = Field(ge=1, le=MAX_BATCH)


@router.post("/products/{product_id}/devices/check")
async def check_range(product_id: UUID, body: RangeIn, svc: ServicesDep, p: Registrar
                      ) -> dict[str, Any]:
    """Before registering a range: its first and last identifier, and the ones already taken
    (by any product: identifiers are unique on the platform)."""
    try:
        product = await svc.store.platform.product(_orgs(p, "device.register"), product_id)
        ids = identifiers(product.identifier_pattern, body.start, body.count)
    except PlatformError as exc:
        raise _api_error(exc) from None
    except ValueError as exc:
        raise Invalid(str(exc), code="invalid_identifier") from None
    try:
        await svc.store.platform._check_free(ids, product_id, [])
    except PlatformError as exc:
        return {"first": ids[0], "last": ids[-1], "count": len(ids),
                "taken": exc.extra.get("values", [])}
    return {"first": ids[0], "last": ids[-1], "count": len(ids), "taken": []}


class DevicePage(BaseModel):
    total: int
    devices: list[PlatformDeviceRecord]


Lifecycle = Literal["registered", "ready", "sold", "active", "disabled", "stock"]


@router.get("/devices")
async def list_devices(svc: ServicesDep, p: Reader, product: UUID | None = None,
                       lifecycle: Lifecycle | None = None,
                       q: Annotated[str | None, Query(max_length=64)] = None,
                       batch: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
                       limit: Annotated[int, Query(ge=1, le=500)] = 100,
                       offset: Annotated[int, Query(ge=0)] = 0) -> DevicePage:
    """``lifecycle=stock``: not with a buyer yet (registered or ready)."""
    rows, total = await svc.store.platform.devices(
        _orgs(p, "product.read"), product_id=product, lifecycle=lifecycle, search=q,
        batch=batch, limit=limit, offset=offset)
    return DevicePage(total=total, devices=rows)


class StageIn(BaseModel):
    lifecycle: Literal["registered", "ready", "sold", "disabled"]
    batch: str | None = Field(default=None, min_length=1, max_length=64)
    device_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_BATCH)


@router.post("/products/{product_id}/devices/lifecycle")
async def set_stage(product_id: UUID, body: StageIn, svc: ServicesDep, p: Registrar,
                    request: Request) -> dict[str, Any]:
    """Move a whole batch (or a list) to a stage, e.g. a shipped batch to "sold"; devices a
    buyer activated keep their stage."""
    try:
        moved = await svc.store.platform.set_lifecycle(
            _orgs(p, "device.register"), product_id, body.lifecycle, batch=body.batch,
            device_ids=body.device_ids)
    except PlatformError as exc:
        raise _api_error(exc) from None
    await svc.store.add_auth_audit(
        "platform_device_updated", p.username, user_id=p.user_id,
        client_addr=client_addr(request),
        detail={"product_id": str(product_id), "lifecycle": body.lifecycle,
                "batch": body.batch, "devices": moved})
    return {"moved": moved}


@router.get("/devices/{device_id}")
async def get_device(device_id: str, svc: ServicesDep, p: Reader) -> PlatformDeviceRecord:
    try:
        return await svc.store.platform.device(_orgs(p, "product.read"), device_id)
    except PlatformError as exc:
        raise _api_error(exc) from None


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    connection: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    enabled: bool | None = None
    lifecycle: Literal["registered", "ready", "sold", "disabled"] | None = None
    warranty_months: int | None = Field(default=None, ge=0, le=240)


@router.patch("/devices/{device_id}")
async def update_device(device_id: str, body: DevicePatch, svc: ServicesDep, p: Registrar,
                        request: Request) -> PlatformDeviceRecord:
    try:
        record, model = await svc.store.platform.update_device(
            _orgs(p, "device.register"), device_id, **body.model_dump(exclude_none=True))
    except PlatformError as exc:
        raise _api_error(exc) from None
    outcome = await svc.hub.apply_platform_device(device_id, model)
    await svc.store.add_auth_audit(
        "platform_device_updated", p.username, user_id=p.user_id,
        client_addr=client_addr(request),
        detail={"device_id": device_id, "identifier": record.identifier, "hub": outcome,
                "changed": sorted(body.model_dump(exclude_none=True))})
    return record


async def _apply(svc: ServicesDep, device_ids: list[str]) -> None:
    models = {d.device_id: d for d in await svc.store.platform.device_models(device_ids)}
    for device_id in device_ids:
        await svc.hub.apply_platform_device(device_id, models.get(device_id))


# --- members and invitations of an organization (platform phase 2) -------------------------


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(pattern=f"^{SEGMENT_PATTERN}$")


@router.get("/organizations/{org_id}/members")
async def list_members(org_id: str, svc: ServicesDep, p: Member) -> list[MemberRecord]:
    return await svc.accounts.members(p, org_id)


@router.delete("/organizations/{org_id}/members/{user_role_id}", status_code=204)
async def remove_member(org_id: str, user_role_id: int, svc: ServicesDep, p: Member,
                        request: Request) -> None:
    await svc.accounts.remove_member(p, org_id, user_role_id, client_addr(request))


@router.get("/organizations/{org_id}/invitations")
async def list_invitations(org_id: str, svc: ServicesDep, p: Member
                           ) -> list[InvitationRecord]:
    return await svc.accounts.invitations(p, org_id)


@router.post("/organizations/{org_id}/invitations", status_code=201)
async def invite(org_id: str, body: InviteIn, svc: ServicesDep, p: Member,
                 request: Request) -> InvitationRecord:
    return await svc.accounts.invite_member(p, org_id, body.email, body.role,
                                            client_addr(request))


@router.delete("/organizations/{org_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(org_id: str, invitation_id: int, svc: ServicesDep, p: Signed,
                            request: Request) -> None:
    await svc.accounts.revoke_invitation(p, org_id, invitation_id, client_addr(request))


# --- the caller's devices and sharing ----------------------------------------------------------


class MyDevice(PlatformDeviceRecord):
    access: str | None = None            # owner / operator / viewer (best of the caller's orgs)
    room: str | None = None              # the room the caller's home put it in
    product_name: str | None = None
    product_has_image: bool = False


async def _my_orgs(svc: ServicesDep, p: Principal, org: str | None = None) -> set[str]:
    orgs = _orgs(p, "device.view")
    if orgs is None:                     # platform administrators use /devices
        orgs = {m.org_id for m in await svc.accounts.memberships(p)}
    if org is not None:
        orgs = orgs & {org}
    return orgs


async def _mine(svc: ServicesDep, orgs: set[str], rows: list[PlatformDeviceRecord]
                ) -> list[MyDevice]:
    ids = [r.device_id for r in rows]
    levels = await svc.store.accounts.access_levels(orgs, ids)
    rooms = await svc.store.accounts.rooms(orgs, ids)
    products = {pr.product_id: pr for pr in await svc.store.platform.products(orgs)}
    out = []
    for r in rows:
        if r.device_id not in levels:     # a maker's own stock is not "mine"
            continue
        product = products.get(r.product_id)
        live = svc.hub.drivers.get(r.device_id)    # the running driver knows better than the
        state = str(live.status.state) if live is not None else r.state     # stored status
        out.append(MyDevice.model_validate(r.model_dump() | {"state": state} | {
            "access": levels[r.device_id], "room": rooms.get(r.device_id),
            "product_name": product.name if product else None,
            "product_has_image": bool(product and product.has_image)}))
    return out


OrgQuery = Annotated[str | None, Query(pattern=f"^{SEGMENT_PATTERN}$")]


@router.get("/my/devices")
async def my_devices(svc: ServicesDep, p: Viewer, org: OrgQuery = None,
                     limit: Annotated[int, Query(ge=1, le=500)] = 200,
                     offset: Annotated[int, Query(ge=0)] = 0) -> list[MyDevice]:
    """Devices the caller's organizations own or received (device.view); ``org`` limits it
    to one home."""
    orgs = await _my_orgs(svc, p, org)
    if not orgs:
        return []
    rows, _ = await svc.store.platform.devices(orgs, limit=limit, offset=offset)
    return await _mine(svc, orgs, rows)


class ShareIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    level: Literal["operator", "viewer"]


@router.get("/devices/{device_id}/shares")
async def list_shares(device_id: str, svc: ServicesDep, p: Sharer) -> dict[str, Any]:
    return await svc.accounts.shares(p, device_id)


@router.post("/devices/{device_id}/shares", status_code=201)
async def share_device(device_id: str, body: ShareIn, svc: ServicesDep, p: Sharer,
                       request: Request) -> InvitationRecord:
    return await svc.accounts.share_device(p, device_id, body.email, body.level,
                                           client_addr(request))


@router.delete("/devices/{device_id}/shares/{access_id}", status_code=204)
async def revoke_share(device_id: str, access_id: int, svc: ServicesDep, p: Signed,
                       request: Request) -> None:
    await svc.accounts.revoke_share(p, device_id, access_id, client_addr(request))


# --- labels, claims, transfer, reset, edge hubs (platform phase 3) -------------------------


class LabelsIn(BaseModel):
    device_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_BATCH)
    batch: str | None = Field(default=None, min_length=1, max_length=64)
    credentials: bool = False          # also a device secret per device (for production)


@router.post("/products/{product_id}/labels")
async def labels(product_id: UUID, body: LabelsIn, svc: ServicesDep, p: Registrar,
                 request: Request) -> Response:
    """CSV for printing labels: identifier, serial, claim code, QR text (and optionally the
    device credential). Shown once: issuing again replaces the earlier codes."""
    text = await svc.claims.labels(p, product_id, device_ids=body.device_ids, batch=body.batch,
                                   credentials=body.credentials,
                                   client_addr=client_addr(request))
    return Response(text, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="labels-{product_id}.csv"',
        "Cache-Control": "no-store"})


@router.post("/devices/{device_id}/reset")
async def reset_device(device_id: str, svc: ServicesDep, p: Registrar,
                       request: Request) -> dict[str, Any]:
    return await svc.claims.reset(p, device_id, client_addr(request))


class ClaimIn(BaseModel):
    identifier: str = Field(min_length=3, max_length=64)
    code: str = Field(min_length=8, max_length=40)
    organization: str | None = Field(default=None, pattern=f"^{SEGMENT_PATTERN}$")
    name: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/claim")
async def claim(body: ClaimIn, svc: ServicesDep, p: Signed, request: Request
                ) -> dict[str, Any]:
    """Add a purchased device to the caller's account (default: the personal organization)."""
    return await svc.claims.claim(p, body.identifier, body.code, body.organization,
                                  body.name, client_addr(request))


class CatalogProduct(ProductRecord):
    protocols: list[str] = []          # of the latest published version


@router.get("/catalog")
async def catalog(svc: ServicesDep, p: Signed) -> list[CatalogProduct]:
    """Products any owner may add behind an edge hub, with the protocols they speak."""
    out = []
    for r in await svc.store.platform.products(None):
        if r.catalog and r.latest_published is not None:
            template = await svc.store.platform.template(r.product_id, r.latest_published)
            out.append(CatalogProduct.model_validate(r.model_dump() | {
                "protocols": sorted(template.bindings)}))
    return out


class OwnerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    connection: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    enabled: bool | None = None


@router.patch("/my/devices/{device_id}")
async def configure_my_device(device_id: str, body: OwnerPatch, svc: ServicesDep, p: Sharer,
                              request: Request) -> PlatformDeviceRecord:
    return await svc.claims.configure(p, device_id, body.model_dump(exclude_none=True),
                                      client_addr(request))


class RoomIn(BaseModel):
    room: str | None = Field(default=None, max_length=40)


@router.put("/my/devices/{device_id}/room")
async def set_room(device_id: str, body: RoomIn, svc: ServicesDep, p: Viewer) -> dict[str, Any]:
    """The room of the device in the caller's homes (each home has its own rooms); an empty
    room takes it out of every room."""
    room = (body.room or "").strip() or None
    if not await svc.store.accounts.set_room(await _my_orgs(svc, p), device_id, room):
        raise NotFound("device not found")
    return {"device_id": device_id, "room": room}


@router.delete("/my/devices/{device_id}", status_code=204)
async def release_my_device(device_id: str, svc: ServicesDep, p: Sharer,
                            request: Request) -> None:
    await svc.claims.release(p, device_id, client_addr(request))


@router.post("/my/devices/{device_id}/transfer")
async def transfer_my_device(device_id: str, svc: ServicesDep, p: Sharer,
                             request: Request) -> dict[str, Any]:
    return await svc.claims.transfer(p, device_id, client_addr(request))


class ChildIn(BaseModel):
    product_id: UUID
    version: int = Field(ge=1)
    protocol: str = Field(pattern=f"^{SEGMENT_PATTERN}$")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    connection: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False


@router.post("/my/devices/{hub_id}/children", status_code=201)
async def add_child(hub_id: str, body: ChildIn, svc: ServicesDep, p: Sharer,
                    request: Request) -> PlatformDeviceRecord:
    return await svc.claims.add_child(
        p, hub_id, product_id=body.product_id, version=body.version, protocol=body.protocol,
        name=body.name, connection=body.connection, options=body.options,
        enabled=body.enabled, client_addr=client_addr(request))


@router.delete("/my/devices/{hub_id}/children/{child_id}", status_code=204)
async def remove_child(hub_id: str, child_id: str, svc: ServicesDep, p: Sharer,
                       request: Request) -> None:
    await svc.claims.remove_child(p, hub_id, child_id, client_addr(request))


@router.get("/my/devices/{device_id}/commands")
async def my_device_commands(device_id: str, svc: ServicesDep, p: Viewer,
                             limit: Annotated[int, Query(ge=1, le=500)] = 50
                             ) -> list[CommandRecord]:
    """The commands of a device the caller's organizations own or received, newest first:
    created / sent / delivered / executed / confirmed, or failed / expired, with the times."""
    orgs = {g.org_id for g in p.grants if g.org_id and "device.view" in
            p.role_permissions.get(g.role_id, frozenset())}
    if not orgs or device_id not in await svc.store.accounts.access_levels(orgs, [device_id]):
        raise NotFound("device not found")
    return await svc.store.commands.for_device(device_id, limit)


class Point(BaseModel):
    ts: datetime
    value: float | None = None


@router.get("/my/devices/{device_id}/history")
async def my_device_history(device_id: str, svc: ServicesDep, p: Viewer,
                            tag: Annotated[str, Query(pattern=f"^{SEGMENT_PATTERN}$")],
                            hours: Annotated[int, Query(ge=1, le=24 * 31)] = 24,
                            points: Annotated[int, Query(ge=10, le=500)] = 96) -> list[Point]:
    """A tag's values over the last hours, for the app's chart (only tags the template shows
    on a chart)."""
    if await _my_level(svc, p, device_id, "device.view") is None:
        raise NotFound("device not found")
    record = await svc.store.platform.device(None, device_id)
    template = await svc.store.platform.template(record.product_id, record.version)
    if not any(t.key == tag and t.ui.visible and t.ui.chart for t in template.tags):
        raise NotFound("no chart for this tag", code="no_chart")
    end = datetime.now(UTC)
    rows = await svc.store.history(f"platform.devices.{device_id}.{tag}",
                                   end - timedelta(hours=hours), end, max_points=points)
    return [Point(ts=r.ts, value=_number(r.avg if r.avg is not None else r.value))
            for r in rows]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return float(value) if isinstance(value, int | float) else None


TEHRAN = ZoneInfo("Asia/Tehran")
TEMPERATURE_UNITS = ("°C", "C", "degC")


@router.get("/my/summary")
async def my_summary(svc: ServicesDep, p: Viewer, org: OrgQuery = None) -> dict[str, Any]:
    """The home screen's tiles, from the devices' own tags: indoor temperature (average of
    the temperature readings), power now and energy since midnight (Tehran time, from kWh
    counters or else the power readings), alerts (offline devices, bad values)."""
    orgs = await _my_orgs(svc, p, org)
    out: dict[str, Any] = {"devices": 0, "online": 0, "temperature": None, "power_kw": None,
                           "energy_kwh": None, "alerts": 0}
    if not orgs:
        return out
    rows, _ = await svc.store.platform.devices(orgs, limit=200)
    mine = await _mine(svc, orgs, rows)
    temps: list[float] = []
    power = energy = 0.0
    have_power = have_energy = False
    alerts = 0
    now = datetime.now(UTC)
    midnight = now.astimezone(TEHRAN).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in mine:
        if d.state != "online":
            alerts += 1
            continue
        template = await svc.store.platform.template(d.product_id, d.version)
        counter = any((x.unit or "") == "kWh" for x in template.tags)
        for t in template.tags:
            path = f"platform.devices.{d.device_id}.{t.key}"
            sample = svc.hub.latest.get(path)
            if sample is None:
                continue
            if str(sample.quality) == "bad":
                alerts += 1
                continue
            value = _number(sample.value) if not isinstance(sample.value, bool) else None
            unit = (t.unit or "").strip()
            if value is None:
                continue
            if unit in TEMPERATURE_UNITS:
                temps.append(value)
            elif unit in ("kW", "W"):
                power += value / (1000 if unit == "W" else 1)
                have_power = True
                if not counter:
                    energy += await _energy(svc, path, midnight, now, unit)
                    have_energy = True
            elif unit == "kWh":
                first = next((v for x in await svc.store.history(path, midnight, now, 2)
                              if (v := _number(x.value if x.avg is None else x.min))
                              is not None), None)
                if first is not None:
                    energy += max(value - first, 0.0)
                    have_energy = True
    out.update(devices=len(mine), online=sum(d.state == "online" for d in mine),
               temperature=round(sum(temps) / len(temps), 1) if temps else None,
               power_kw=round(power, 3) if have_power else None,
               energy_kwh=round(energy, 2) if have_energy else None, alerts=alerts)
    return out


async def _energy(svc: ServicesDep, path: str, start: datetime, end: datetime,
                  unit: str) -> float:
    """kWh from power readings (trapezoids over the stored history; gaps over an hour are
    not counted)."""
    kwh, last = 0.0, None
    for r in await svc.store.history(path, start, end, max_points=288):
        value = _number(r.avg if r.avg is not None else r.value)
        if value is None:
            continue
        kw = value / (1000 if unit == "W" else 1)
        if last is not None:
            hours = (r.ts - last[0]).total_seconds() / 3600
            if 0 < hours <= 1:
                kwh += (kw + last[1]) / 2 * hours
        last = (r.ts, kw)
    return kwh


@router.get("/my/notifications")
async def my_notifications(svc: ServicesDep, p: Viewer, org: OrgQuery = None
                           ) -> list[dict[str, Any]]:
    """What the bell shows, newest first: devices offline, commands that failed or got no
    answer, automation runs that failed (the last 48 hours)."""
    orgs = await _my_orgs(svc, p, org)
    if not orgs:
        return []
    rows, _ = await svc.store.platform.devices(orgs, limit=200)
    since = datetime.now(UTC) - timedelta(hours=48)
    out: list[dict[str, Any]] = []
    for d in await _mine(svc, orgs, rows):
        if d.state != "online" and d.enabled:
            out.append({"kind": "offline", "device_id": d.device_id, "device": d.name,
                        "at": d.last_seen or d.registered_at})
        for c in await svc.store.commands.for_device(d.device_id, 20):
            if c.created_at < since:
                break
            if c.status in ("failed", "expired"):
                out.append({"kind": f"command_{c.status}", "device_id": d.device_id,
                            "device": d.name, "tag": c.tag_path.rsplit(".", 1)[-1],
                            "value": c.value, "at": c.finished_at or c.created_at})
    viewable = p.orgs_with("device.view")
    for org_id in sorted(orgs if viewable is None else orgs & viewable):
        for a in await svc.store.automations.automations(orgs, org_id):
            for run in await svc.store.automations.runs(orgs, a.automation_id, 5):
                if run.started_at >= since and run.status in ("failed", "partial"):
                    out.append({"kind": f"automation_{run.status}",
                                "automation_id": a.automation_id, "automation": a.name,
                                "at": run.finished_at or run.started_at})
    out.sort(key=lambda n: n["at"], reverse=True)
    return out[:50]


# --- the consumer app's device view (platform phase 5) -----------------------------------------


class TagView(BaseModel):
    key: str
    kind: str
    display_name: dict[str, str]
    data_type: str
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    states: dict[int, str] | None = None
    writable: bool = False               # read_write in the template and the caller may operate
    ui: dict[str, Any] = {}
    value: Any = None
    ts: Any = None
    quality: str | None = None
    default_name: dict[str, str] | None = None   # the maker's, when the home renamed it
    icon: str | None = None                      # the home's choice
    favorite: bool = False
    mode: str = "toggle"                         # how the home switches it (toggle/dual/momentary)
    pulse_s: float | None = None


class CommandView(BaseModel):
    key: str
    display_name: dict[str, str]
    icon: str | None = None
    params: list[dict[str, Any]]          # tag, value (None: the user enters it)


class DeviceView(BaseModel):
    device: MyDevice
    product: str
    tags: list[TagView]
    commands: list[CommandView] = []      # several parameters at once (if the caller operates)


async def _my_level(svc: ServicesDep, p: Principal, device_id: str, permission: str
                    ) -> str | None:
    orgs = {g.org_id for g in p.grants if g.org_id and permission in
            p.role_permissions.get(g.role_id, frozenset())}
    if not orgs:
        return None
    return (await svc.store.accounts.access_levels(orgs, [device_id])).get(device_id)


@router.get("/my/devices/{device_id}/view")
async def my_device_view(device_id: str, svc: ServicesDep, p: Viewer) -> DeviceView:
    """Everything the app shows for one device: its tags as the product template defines them
    (names fa/en, kinds, units, ranges, UI hints) with the live values; ``writable`` already
    says whether this caller may operate the tag."""
    level = await _my_level(svc, p, device_id, "device.view")
    if level is None:
        raise NotFound("device not found")
    record = await svc.store.platform.device(None, device_id)
    product = await svc.store.platform.product(None, record.product_id)
    template = await svc.store.platform.template(record.product_id, record.version)
    operate = level in ("owner", "operator") and \
        await _my_level(svc, p, device_id, "device.operate") in ("owner", "operator")
    owner = await svc.store.accounts.owner_org(None, device_id)
    labels = await svc.store.home.labels([owner], [device_id]) if owner else {}
    tags = []
    for t in sorted(template.tags, key=lambda t: (t.ui.order, t.key)):
        if not t.ui.visible:
            continue
        sample = svc.hub.latest.get(f"platform.devices.{device_id}.{t.key}")
        label = labels.get((device_id, t.key))
        names = dict(t.display_name)
        tags.append(TagView(
            key=t.key, kind=str(t.kind),
            display_name={"fa": label.name, "en": label.name} if label and label.name else names,
            default_name=names if label and label.name else None,
            icon=label.icon if label else None, favorite=bool(label and label.favorite),
            mode=label.mode if label else "toggle", pulse_s=label.pulse_s if label else None,
            data_type=str(t.data_type), unit=t.unit, min=t.min, max=t.max, states=t.states,
            writable=operate and str(t.access) == "read_write",
            ui=t.ui.model_dump(exclude_none=True),
            value=sample.value if sample else None, ts=sample.timestamp if sample else None,
            quality=str(sample.quality) if sample else None))
    rooms = await svc.store.accounts.rooms(await _my_orgs(svc, p), [device_id])
    live = svc.hub.drivers.get(device_id)
    return DeviceView(device=MyDevice.model_validate(record.model_dump() | {
        "state": str(live.status.state) if live is not None else record.state,
        "access": level, "room": rooms.get(device_id), "product_name": product.name,
        "product_has_image": product.has_image}), product=product.name, tags=tags,
        commands=[CommandView(key=c.key, display_name=dict(c.display_name), icon=c.icon,
                              params=[prm.model_dump() for prm in c.params])
                  for c in template.commands] if operate else [])


# --- protocol forms for the maker panel --------------------------------------------------------


def _schema_fields(model: Any, skip: frozenset[str] | set[str]) -> list[dict[str, Any]]:
    """A flat field list (name, type, enum, default, required, help) from a pydantic model."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))

    def resolve(prop: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in prop:
            return resolve(defs.get(prop["$ref"].rsplit("/", 1)[-1], {}))
        if "anyOf" in prop:
            options = [resolve(o) for o in prop["anyOf"] if o.get("type") != "null"]
            return options[0] if len(options) == 1 else {"type": "json"}
        if "allOf" in prop and len(prop["allOf"]) == 1:
            return resolve(prop["allOf"][0])
        return prop

    out = []
    for name, prop in schema.get("properties", {}).items():
        if name in skip:
            continue
        inner = resolve(prop)
        kind = inner.get("type", "json")
        field: dict[str, Any] = {"name": name, "required": name in required,
                                 "type": kind if kind in ("string", "integer", "number",
                                                          "boolean") else "json"}
        if "enum" in inner:
            field["type"] = "enum"
            field["enum"] = inner["enum"]
        for key in ("default", "minimum", "maximum"):
            if key in prop:
                field[key] = prop[key]
            elif key in inner:
                field[key] = inner[key]
        text = prop.get("description") or inner.get("description")
        if text:
            field["help"] = text[:300]
        out.append(field)
    return out


@cache
def _protocol_fields() -> dict[str, Any]:
    from hub.platform.template import TEMPLATE_TAG_FIELDS, device_classes, tag_model

    out: dict[str, Any] = {}
    for protocol, cls in sorted(device_classes().items()):
        connection = cls.model_fields["connection"].annotation
        out[protocol] = {
            "connection": _schema_fields(connection, set())
            if hasattr(connection, "model_json_schema") else [],
            "tag": _schema_fields(tag_model(protocol), TEMPLATE_TAG_FIELDS),
        }
    return out
