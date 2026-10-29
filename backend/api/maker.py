"""The maker panel beyond devices: its brand (also shown in the buyer's app), support tickets
and warranty claims from buyers, trend figures, the device list as CSV, the platform plan
and monthly invoices. Organization permissions as in /api/platform: ``product.read`` to look,
``product.manage`` to change the brand, ``org.members`` for billing; ``org.manage`` (platform
administration) sets plans and invoice states."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from backend.api.home import MessageIn, TicketOut, ticket_mail
from backend.api.platform import IMAGE_MAGIC, MAX_IMAGE, TEHRAN, Lifecycle
from backend.deps import ServicesDep, client_addr, require
from backend.errors import Invalid, NotFound
from backend.rbac import Principal
from hub import jalali
from hub.models import SEGMENT_PATTERN
from hub.storage.home import InvoiceRecord, PlanRecord, TicketRecord

router = APIRouter(tags=["maker"])
Reader = Annotated[Principal, Depends(require("product.read"))]
Manager = Annotated[Principal, Depends(require("product.manage"))]
Member = Annotated[Principal, Depends(require("org.members"))]
OrgAdmin = Annotated[Principal, Depends(require("org.manage"))]


def _in(p: Principal, permission: str, org_id: str) -> None:
    if not p.can_in_org(permission, org_id):
        raise NotFound("organization not found")


def _maker_orgs(p: Principal) -> list[str] | None:
    orgs = p.orgs_with("product.read")
    return None if orgs is None else sorted(orgs)


# --- branding ----------------------------------------------------------------------------------


class BrandIn(BaseModel):
    brand_name: str | None = Field(default=None, min_length=1, max_length=60)
    accent: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    support_email: str | None = Field(default=None, min_length=3, max_length=254)
    support_phone: str | None = Field(default=None, pattern=r"^\+?[0-9 -]{6,20}$")


@router.get("/api/platform/organizations/{org_id}/branding")
async def branding(org_id: str, svc: ServicesDep, p: Reader) -> dict[str, Any]:
    _in(p, "product.read", org_id)
    return await svc.store.home.branding(org_id)


@router.put("/api/platform/organizations/{org_id}/branding")
async def save_branding(org_id: str, body: BrandIn, svc: ServicesDep, p: Manager,
                        request: Request) -> dict[str, Any]:
    _in(p, "product.manage", org_id)
    await svc.store.home.save_branding(org_id, body.model_dump())
    await svc.store.add_auth_audit("branding_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request), detail={"org_id": org_id})
    return await svc.store.home.branding(org_id)


@router.put("/api/platform/organizations/{org_id}/branding/logo", status_code=204)
async def put_logo(org_id: str, request: Request, svc: ServicesDep, p: Manager) -> None:
    _in(p, "product.manage", org_id)
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    body = await request.body()
    magic = IMAGE_MAGIC.get(content_type)
    if magic is None or not body.startswith(magic) or \
            content_type == "image/webp" and body[8:12] != b"WEBP":
        raise Invalid("send a PNG, JPEG or WebP image", code="invalid_image")
    if len(body) > min(MAX_IMAGE, 256 * 1024):
        raise Invalid("the logo is larger than 256 KB", code="image_too_large")
    await svc.store.home.set_logo(org_id, body, content_type)


@router.delete("/api/platform/organizations/{org_id}/branding/logo", status_code=204)
async def delete_logo(org_id: str, svc: ServicesDep, p: Manager) -> None:
    _in(p, "product.manage", org_id)
    await svc.store.home.set_logo(org_id, None, None)


@router.get("/api/public/brand/{org_id}")
async def public_brand(org_id: str, svc: ServicesDep) -> dict[str, Any]:
    """What a maker shows of itself (no sign-in: the app's start page uses it with
    ``?brand=<org_id>``): name, accent colour, support contact, whether there is a logo."""
    b = await svc.store.home.branding(org_id)
    org = await svc.store.accounts.organization(org_id)
    if not b or org is None or org["kind"] != "manufacturer":
        raise NotFound("brand not found")
    return {k: b.get(k) for k in ("org_id", "display_name", "accent", "support_email",
                                  "support_phone", "has_logo")}


@router.get("/api/public/brand/{org_id}/logo")
async def public_logo(org_id: str, svc: ServicesDep) -> Response:
    org = await svc.store.accounts.organization(org_id)
    found = await svc.store.home.logo(org_id) if org and org["kind"] == "manufacturer" else None
    if found is None:
        raise NotFound("no logo")
    return Response(found[0], media_type=found[1], headers={"Cache-Control": "public, max-age=600"})


# --- tickets (maker side) ------------------------------------------------------------------------


class ReplyIn(MessageIn):
    status: Literal["answered", "in_progress", "approved", "rejected", "closed"] | None = None


@router.get("/api/platform/tickets")
async def tickets(svc: ServicesDep, p: Reader,
                  status: Annotated[str | None, Query(pattern="^[a-z_]{1,20}$")] = None
                  ) -> list[TicketRecord]:
    return await svc.store.home.tickets(maker_orgs=_maker_orgs(p), status=status)


async def _maker_ticket(svc: ServicesDep, p: Principal, ticket_id: int) -> TicketOut:
    found = await svc.store.home.ticket(ticket_id)
    if found is None or not p.can_in_org("product.read", found[0].maker_org_id):
        raise NotFound("ticket not found")
    return TicketOut(ticket=found[0], messages=found[1])


@router.get("/api/platform/tickets/{ticket_id}")
async def ticket(ticket_id: int, svc: ServicesDep, p: Reader) -> TicketOut:
    return await _maker_ticket(svc, p, ticket_id)


@router.post("/api/platform/tickets/{ticket_id}/messages")
async def answer(ticket_id: int, body: ReplyIn, svc: ServicesDep, p: Reader,
                 request: Request) -> TicketOut:
    """The maker answers and/or moves the ticket on (a warranty claim: approved / rejected)."""
    current = await _maker_ticket(svc, p, ticket_id)
    status = body.status or "answered"
    if status in ("approved", "rejected") and current.ticket.kind != "warranty":
        raise Invalid("only warranty claims are approved or rejected", code="not_warranty")
    brand = await svc.store.home.branding(current.ticket.maker_org_id)
    await svc.store.home.add_message(ticket_id, "maker", p.user_id,
                                     f"{p.user.display_name} · {brand.get('display_name', '')}",
                                     body.body, status)
    await svc.store.add_auth_audit("ticket_updated", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"ticket_id": ticket_id, "status": status})
    updated = await _maker_ticket(svc, p, ticket_id)
    await ticket_mail(svc, updated.ticket, "ticket_answer", body.body, to_maker=False)
    return updated


# --- trends and the device list as CSV ------------------------------------------------------------


@router.get("/api/platform/trend")
async def trend(svc: ServicesDep, p: Reader, product: UUID | None = None,
                weeks: Annotated[int, Query(ge=2, le=104)] = 12) -> dict[str, Any]:
    """Per week: devices registered, activated by buyers, tickets opened; and the activated
    devices offline for more than three days now (likely faulty)."""
    orgs = _maker_orgs(p)
    if orgs is None:
        orgs = sorted(await svc.store.platform.organization_ids())
    return await svc.store.home.trend(orgs, product, weeks, datetime.now(UTC))


@router.get("/api/platform/devices.csv")
async def devices_csv(svc: ServicesDep, p: Reader, product: UUID | None = None,
                      lifecycle: Lifecycle | None = None,
                      batch: Annotated[str | None, Query(min_length=1, max_length=64)] = None
                      ) -> Response:
    """The devices (at most 50 000) with their stage, connection, firmware and warranty —
    never their owner."""
    orgs = p.orgs_with("product.read")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["identifier", "serial_number", "product", "version", "batch", "lifecycle",
                     "state", "last_seen", "firmware", "registered_at", "activated_at",
                     "warranty_months", "warranty_until"])
    offset = 0
    while offset < 50_000:
        rows, total = await svc.store.platform.devices(orgs, product_id=product,
                                                       lifecycle=lifecycle, batch=batch,
                                                       limit=500, offset=offset)
        for r in rows:
            writer.writerow([r.identifier, r.serial_number or "", r.product_code, r.version,
                             r.batch or "", r.lifecycle, r.state or "",
                             r.last_seen.isoformat() if r.last_seen else "",
                             r.firmware_version or "", r.registered_at.isoformat(),
                             r.activated_at.isoformat() if r.activated_at else "",
                             r.warranty_months if r.warranty_months is not None else "",
                             r.warranty_until.isoformat() if r.warranty_until else ""])
        offset += 500
        if offset >= total:
            break
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="devices.csv"', "Cache-Control": "no-store"})


# --- plans and invoices ---------------------------------------------------------------------------


def _amount(plan: PlanRecord, devices: int) -> int:
    extra = max(devices - plan.included_devices, 0)
    return plan.monthly_fee + extra * plan.per_device_fee


def _period(now: datetime) -> str:
    jy, jm, _ = jalali.to_jalali(now.astimezone(TEHRAN).date())
    return f"{jy:04d}-{jm:02d}"


@router.get("/api/platform/organizations/{org_id}/billing")
async def billing(org_id: str, svc: ServicesDep, p: Member) -> dict[str, Any]:
    """The plan, an estimate of this month (activated devices now: nothing is stored), the
    issued invoices (one per past Jalali month, made by ``close_month``), every plan to
    compare. Payment itself happens outside the platform (bank transfer / gateway)."""
    _in(p, "org.members", org_id)
    plan = await svc.store.home.org_plan(org_id)
    devices = await svc.store.home.active_devices(org_id)
    current = {"period": _period(svc.hub.clock()), "plan_id": plan.plan_id,
               "active_devices": devices, "amount": _amount(plan, devices),
               "status": "estimate"}
    return {"plan": plan, "active_devices": devices, "current": current,
            "over_limit": plan.max_devices is not None and devices > plan.max_devices,
            "invoices": await svc.store.home.invoices(org_id),
            "plans": await svc.store.home.plans()}


def _previous(period: str) -> str:
    year, month = (int(x) for x in period.split("-"))
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


async def close_month(svc: ServicesDep, now: datetime) -> int:
    """Issue last month's invoice of every manufacturer that has none yet (activated devices
    when the month turned). Run by the automation engine's loop; repeating it changes nothing
    (an issued or paid invoice is kept)."""
    period = _previous(_period(now))
    made = 0
    for org_id in sorted(await svc.store.platform.organization_ids()):
        org = await svc.store.accounts.organization(org_id)
        if org is None or org["kind"] != "manufacturer":
            continue
        if any(i.period == period for i in await svc.store.home.invoices(org_id)):
            continue
        plan = await svc.store.home.org_plan(org_id)
        devices = await svc.store.home.active_devices(org_id)
        await svc.store.home.issue_invoice(org_id, period, plan.plan_id, devices,
                                           _amount(plan, devices))
        made += 1
    return made


class PlanIn(BaseModel):
    plan_id: str = Field(pattern=f"^{SEGMENT_PATTERN}$")


@router.put("/api/platform/organizations/{org_id}/plan")
async def set_plan(org_id: str, body: PlanIn, svc: ServicesDep, p: OrgAdmin,
                   request: Request) -> PlanRecord:
    if body.plan_id not in {x.plan_id for x in await svc.store.home.plans()}:
        raise Invalid("unknown plan", code="unknown_plan")
    org = await svc.store.accounts.organization(org_id)
    if org is None or org["kind"] != "manufacturer":
        raise NotFound("organization not found")
    await svc.store.home.set_plan(org_id, body.plan_id, p.user_id)
    await svc.store.add_auth_audit("plan_changed", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "plan_id": body.plan_id})
    return await svc.store.home.org_plan(org_id)


class InvoiceStatusIn(BaseModel):
    status: Literal["paid", "void", "issued"]


@router.post("/api/platform/invoices/{invoice_id}/status")
async def invoice_status(invoice_id: int, body: InvoiceStatusIn, svc: ServicesDep,
                         p: OrgAdmin) -> InvoiceRecord:
    record = await svc.store.home.set_invoice_status(invoice_id, body.status)
    if record is None:
        raise NotFound("invoice not found")
    return record
