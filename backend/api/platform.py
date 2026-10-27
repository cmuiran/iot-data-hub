"""/api/platform: organizations, products, template versions and manufactured devices.

Organization permissions (backend.rbac): ``product.read`` to see an organization's products and
devices, ``product.manage`` to define products and publish versions, ``device.register`` to
register and change devices; ``org.manage`` (global) creates organizations. Every query runs
limited to the caller's organizations (hub.storage.platform, row-level security), so another
organization's product or device answers like an unknown one (404).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

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


class DevicePage(BaseModel):
    total: int
    devices: list[PlatformDeviceRecord]


Lifecycle = Literal["registered", "ready", "sold", "active", "disabled"]


@router.get("/devices")
async def list_devices(svc: ServicesDep, p: Reader, product: UUID | None = None,
                       lifecycle: Lifecycle | None = None,
                       q: Annotated[str | None, Query(max_length=64)] = None,
                       limit: Annotated[int, Query(ge=1, le=500)] = 100,
                       offset: Annotated[int, Query(ge=0)] = 0) -> DevicePage:
    rows, total = await svc.store.platform.devices(
        _orgs(p, "product.read"), product_id=product, lifecycle=lifecycle, search=q,
        limit=limit, offset=offset)
    return DevicePage(total=total, devices=rows)


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


@router.get("/my/devices")
async def my_devices(svc: ServicesDep, p: Viewer,
                     limit: Annotated[int, Query(ge=1, le=500)] = 200,
                     offset: Annotated[int, Query(ge=0)] = 0) -> list[MyDevice]:
    """Devices the caller's organizations own or received (device.view)."""
    orgs = _orgs(p, "device.view")
    if orgs is None:                     # platform administrators use /devices
        orgs = {m.org_id for m in await svc.accounts.memberships(p)}
    if not orgs:
        return []
    rows, _ = await svc.store.platform.devices(orgs, limit=limit, offset=offset)
    levels = await svc.store.accounts.access_levels(orgs, [r.device_id for r in rows])
    return [MyDevice.model_validate(r.model_dump() | {"access": levels[r.device_id]})
            for r in rows if r.device_id in levels]   # a maker's own stock is not "mine"


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


@router.get("/catalog")
async def catalog(svc: ServicesDep, p: Signed) -> list[ProductRecord]:
    """Products any owner may add behind an edge hub."""
    return [r for r in await svc.store.platform.products(None)
            if r.catalog and r.latest_published is not None]


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


class DeviceView(BaseModel):
    device: MyDevice
    product: str
    tags: list[TagView]


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
    tags = []
    for t in sorted(template.tags, key=lambda t: (t.ui.order, t.key)):
        if not t.ui.visible:
            continue
        sample = svc.hub.latest.get(f"platform.devices.{device_id}.{t.key}")
        tags.append(TagView(
            key=t.key, kind=str(t.kind), display_name=dict(t.display_name),
            data_type=str(t.data_type), unit=t.unit, min=t.min, max=t.max, states=t.states,
            writable=operate and str(t.access) == "read_write",
            ui=t.ui.model_dump(exclude_none=True),
            value=sample.value if sample else None, ts=sample.timestamp if sample else None,
            quality=str(sample.quality) if sample else None))
    return DeviceView(device=MyDevice.model_validate(record.model_dump() | {"access": level}),
                      product=product.name, tags=tags)
