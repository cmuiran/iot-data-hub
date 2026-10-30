"""The consumer app beyond phase 5: homes (create, rename), deleting the account, a home's
place and electricity tariff, device groups, countdown timers, energy and cost, the SMS
fallback, live updates (server-sent events), support tickets and warranty claims, commands
with several parameters, and the rules a device runs on its own while it is cut off.

Every route checks the caller's organizations first (as the rest of /api/platform/my);
other homes' groups, timers, tickets or SMS settings answer like unknown ones (404).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.platform import TEHRAN, OrgQuery, _mine, _my_level, _my_orgs, _number
from backend.deps import ServicesDep, client_addr, require
from backend.errors import Conflict, Forbidden, Invalid, NotFound
from backend.rbac import Principal
from backend.security import verify_password
from hub import jalali
from hub.models import SEGMENT_PATTERN
from hub.storage.home import GroupRecord, MessageRecord, TicketRecord

log = logging.getLogger(__name__)
router = APIRouter(tags=["home"])
Signed = Annotated[Principal, Depends(require("self"))]
Viewer = Annotated[Principal, Depends(require("device.view"))]
ICON = r"^[a-z][a-z0-9_-]{0,39}$"
STREAM_S = 300


def _member(p: Principal, org_id: str, permission: str = "device.view") -> None:
    if not p.can_in_org(permission, org_id):
        raise NotFound("home not found")


# --- homes and the account ----------------------------------------------------------------------


class HomeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/api/account/homes", status_code=201)
async def create_home(body: HomeIn, svc: ServicesDep, p: Signed, request: Request
                      ) -> dict[str, Any]:
    """Another home of the user (a villa, the office): the user owns it and invites others."""
    homes = [m for m in await svc.accounts.memberships(p) if m.role_id == "customer_owner"]
    if len(homes) >= 20:
        raise Invalid("at most 20 homes per user", code="too_many_homes")
    org_id = await svc.store.home.create_home(p.user_id, body.name.strip())
    await svc.store.add_auth_audit("home_created", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request), detail={"org_id": org_id})
    return {"org_id": org_id, "name": body.name.strip()}


@router.patch("/api/account/homes/{org_id}")
async def rename_home(org_id: str, body: HomeIn, svc: ServicesDep, p: Signed,
                      request: Request) -> dict[str, Any]:
    _member(p, org_id, "org.members")
    org = await svc.store.accounts.organization(org_id)
    if org is None or org["kind"] != "customer":
        raise NotFound("home not found")
    await svc.store.home.rename_home(org_id, body.name.strip())
    await svc.store.add_auth_audit("home_updated", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request), detail={"org_id": org_id})
    return {"org_id": org_id, "name": body.name.strip()}


class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    confirm: Literal["DELETE"]


@router.post("/api/account/delete", status_code=204)
async def delete_account(body: DeleteAccountIn, svc: ServicesDep, p: Signed,
                         request: Request) -> None:
    """The user deletes the account: the devices of the personal home are released (their
    maker sees them as sold, a new owner can claim them after a reset), every session and
    role ends, name / e-mail / password are erased. Shared homes stay with their owners."""
    if p.user.auth_source != "local" or not verify_password(p.user.password_hash, body.password):
        raise Forbidden("the password is not right", code="invalid_credentials")
    personal = await svc.store.accounts.personal_org(p.user_id)
    released = 0
    if personal is not None:
        rows, _ = await svc.store.platform.devices({personal}, limit=500)
        levels = await svc.store.accounts.access_levels({personal}, [r.device_id for r in rows])
        for r in rows:
            if levels.get(r.device_id) == "owner" and r.parent_device_id is None:
                ids = await svc.store.claims.release(r.device_id, "sold", svc.hub.clock())
                await svc.claims._stop(ids)
                released += 1
    await svc.store.add_auth_audit("account_deleted", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"devices_released": released})
    await svc.store.revoke_user_sessions(p.user_id)
    await svc.store.home.anonymize_user(p.user_id)


# --- a home's place and tariff -------------------------------------------------------------------


class Tier(BaseModel):
    up_to_kwh: float | None = Field(default=None, gt=0)    # None: the last tier
    price: float = Field(ge=0)                              # per kWh


class Tariff(BaseModel):
    tiers: list[Tier] = Field(default_factory=list, max_length=10)
    peak_from: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    peak_to: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    peak_factor: float = Field(default=1.0, ge=0.1, le=10)
    fixed: float = Field(default=0, ge=0)                   # per month
    tax_percent: float = Field(default=0, ge=0, le=100)
    currency: str = Field(default="ریال", max_length=12)


class SettingsIn(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_m: int | None = Field(default=None, ge=30, le=5000)
    tariff: Tariff | None = None


@router.get("/api/platform/organizations/{org_id}/settings")
async def home_settings(org_id: str, svc: ServicesDep, p: Viewer) -> dict[str, Any]:
    _member(p, org_id)
    return await svc.store.home.settings(org_id)


@router.put("/api/platform/organizations/{org_id}/settings")
async def save_home_settings(org_id: str, body: SettingsIn, svc: ServicesDep, p: Viewer
                             ) -> dict[str, Any]:
    _member(p, org_id, "org.members")
    values = body.model_dump(exclude_unset=True)
    if "tariff" in values and values["tariff"] is not None:
        values["tariff"] = body.tariff.model_dump() if body.tariff else {}
    return await svc.store.home.save_settings(org_id, values)


# --- groups --------------------------------------------------------------------------------------


class OutputRef(BaseModel):
    device_id: str = Field(pattern=f"^{SEGMENT_PATTERN}$")
    key: str = Field(pattern=f"^{SEGMENT_PATTERN}$")


class SectionIn(BaseModel):
    """A named part of a group: "chandelier" = relay 1 and 2 of the kitchen controller."""
    name: str = Field(min_length=1, max_length=40)
    icon: str | None = Field(default=None, pattern=ICON)
    outputs: list[OutputRef] = Field(min_length=1, max_length=32)


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    icon: str | None = Field(default=None, pattern=ICON)
    device_ids: list[str] = Field(min_length=1, max_length=50)
    sections: list[SectionIn] = Field(default=[], max_length=20)
    position: int = Field(default=0, ge=0, le=1000)

    def parts(self) -> list[dict[str, Any]]:
        stray = {o.device_id for s in self.sections for o in s.outputs} - set(self.device_ids)
        if stray:
            raise Invalid("a part names a device that is not in the group",
                          code="unknown_device")
        return [s.model_dump(exclude_none=True) for s in self.sections]


@router.get("/api/platform/organizations/{org_id}/groups")
async def groups(org_id: str, svc: ServicesDep, p: Viewer) -> list[GroupRecord]:
    _member(p, org_id)
    return await svc.store.home.groups([org_id])


async def _check_devices(svc: ServicesDep, org_id: str, device_ids: list[str]) -> list[str]:
    ids = list(dict.fromkeys(device_ids))
    levels = await svc.store.accounts.access_levels({org_id}, ids)
    missing = [d for d in ids if d not in levels]
    if missing:
        raise Invalid(f"not devices of this home: {', '.join(missing[:5])}", code="unknown_device")
    return ids


@router.post("/api/platform/organizations/{org_id}/groups", status_code=201)
async def create_group(org_id: str, body: GroupIn, svc: ServicesDep, p: Viewer) -> GroupRecord:
    _member(p, org_id)
    ids = await _check_devices(svc, org_id, body.device_ids)
    return await svc.store.home.save_group(org_id, None, name=body.name, icon=body.icon,
                                           device_ids=ids, position=body.position,
                                           user_id=p.user_id, sections=body.parts())


@router.put("/api/platform/organizations/{org_id}/groups/{group_id}")
async def update_group(org_id: str, group_id: int, body: GroupIn, svc: ServicesDep,
                       p: Viewer) -> GroupRecord:
    _member(p, org_id)
    current = await svc.store.home.group(group_id)
    if current is None or current.org_id != org_id:
        raise NotFound("group not found")
    ids = await _check_devices(svc, org_id, body.device_ids)
    return await svc.store.home.save_group(org_id, group_id, name=body.name, icon=body.icon,
                                           device_ids=ids, position=body.position,
                                           user_id=p.user_id, sections=body.parts())


@router.delete("/api/platform/organizations/{org_id}/groups/{group_id}", status_code=204)
async def delete_group(org_id: str, group_id: int, svc: ServicesDep, p: Viewer) -> None:
    _member(p, org_id)
    if not await svc.store.home.delete_group(org_id, group_id):
        raise NotFound("group not found")


# --- the home's names for outputs ---------------------------------------------------------------


class LabelIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    icon: str | None = Field(default=None, pattern=ICON)
    favorite: bool = False
    # toggle: one key; dual: separate on and off keys; momentary: a push (on, then off)
    mode: Literal["toggle", "dual", "momentary"] = "toggle"
    pulse_s: float | None = Field(default=None, gt=0, le=600)


@router.put("/api/platform/my/devices/{device_id}/labels/{key}")
async def save_label(device_id: str, key: str, body: LabelIn, svc: ServicesDep,
                     p: Viewer) -> dict[str, Any]:
    """The owner or an operator names an output or reading of the device for the home
    ("relay 1" -> "chandelier"), picks its icon and marks it a favourite for the home screen;
    no name, icon or mark = the maker's own name again."""
    if await _my_level(svc, p, device_id, "device.operate") not in ("owner", "operator"):
        raise NotFound("device not found")
    owner = await svc.store.accounts.owner_org(None, device_id)
    record = await svc.store.platform.device(None, device_id)
    template = await svc.store.platform.template(record.product_id, record.version)
    if owner is None or key not in {t.key for t in template.tags}:
        raise NotFound("output not found")
    name = body.name.strip() if body.name and body.name.strip() else None
    pulse = (body.pulse_s or 1.0) if body.mode == "momentary" else None
    await svc.store.home.save_label(owner, device_id, key, name=name, icon=body.icon,
                                    favorite=body.favorite, mode=body.mode, pulse_s=pulse)
    return {"device_id": device_id, "key": key, "name": name, "icon": body.icon,
            "favorite": body.favorite, "mode": body.mode, "pulse_s": pulse}


class PulseIn(BaseModel):
    tag: str = Field(pattern=f"^{SEGMENT_PATTERN}$")
    seconds: float = Field(default=1.0, gt=0, le=600)


_PULSES: set[asyncio.Task[Any]] = set()


@router.post("/api/platform/my/devices/{device_id}/pulse")
async def pulse(device_id: str, body: PulseIn, svc: ServicesDep, p: Viewer) -> dict[str, Any]:
    """A momentary key (a door opener, a bell): on now and off after ``seconds``, both
    through the control-safety layer with the caller's rights. The hub turns it off even if
    the app is closed meanwhile."""
    if await _my_level(svc, p, device_id, "device.operate") not in ("owner", "operator"):
        raise NotFound("device not found")
    path = f"platform.devices.{device_id}.{body.tag}"
    result = await svc.control.request_write(p, path, True, "app: momentary", None)
    if result.result != "applied":
        return {"result": result.result, "message": result.message}

    async def release() -> None:
        await asyncio.sleep(body.seconds)
        # the safety layer keeps a minimum time between two writes of one output: a push
        # shorter than that is released as soon as it allows (never left on)
        for _ in range(30):
            try:
                r = await svc.control.request_write(p, path, False, "app: momentary release",
                                                    None)
            except Exception:  # noqa: BLE001 - logged; the device's own state shows it
                log.exception("momentary release of %s failed", path)
                return
            if r.result != "rejected_rate":
                if r.result != "applied":
                    log.warning("momentary release of %s: %s", path, r.result)
                return
            await asyncio.sleep(0.5)

    task = asyncio.create_task(release(), name=f"pulse:{path}")
    _PULSES.add(task)
    task.add_done_callback(_PULSES.discard)
    return {"result": "applied", "seconds": body.seconds}


# --- the home's SMS / call contacts, its SMS PIN, SMS commands -----------------------------------

PHONE = r"^\+?[0-9]{6,15}$"


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    phone: str = Field(pattern=PHONE)
    sms: bool = True                    # alerts by SMS
    call: bool = False                  # alerts by a voice call
    command: bool = False               # may run scenes by an SMS keyword (with the PIN)


@router.get("/api/platform/organizations/{org_id}/contacts")
async def contacts(org_id: str, svc: ServicesDep, p: Viewer) -> dict[str, Any]:
    """Who the home alerts by SMS or call, and whether SMS commands are set up."""
    _member(p, org_id, "org.members")
    return {"contacts": [c.model_dump() for c in await svc.store.home.contacts(org_id)],
            "pin_set": bool(await svc.store.home.sms_pin_hash(org_id)),
            "gateway": bool(getattr(svc.sms, "enabled", False))}


async def _save_contact(svc: ServicesDep, p: Principal, org_id: str, contact_id: int | None,
                        body: ContactIn, request: Request) -> dict[str, Any]:
    _member(p, org_id, "org.members")
    from backend.sms import normalize_phone

    if contact_id is None and len(await svc.store.home.contacts(org_id)) >= 10:
        raise Invalid("a home has 10 contacts at most", code="too_many")
    try:
        record = await svc.store.home.save_contact(
            org_id, contact_id, name=body.name.strip(), phone=normalize_phone(body.phone),
            sms=body.sms, call=body.call, command=body.command, user_id=p.user_id)
    except Exception as exc:  # noqa: BLE001 - the unique (home, phone)
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise Conflict("this number is already a contact of the home",
                           code="duplicate_phone") from None
        raise
    if record is None:
        raise NotFound("contact not found")
    await svc.store.add_auth_audit("contact_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "contact_id": record.contact_id})
    return record.model_dump()


@router.post("/api/platform/organizations/{org_id}/contacts", status_code=201)
async def create_contact(org_id: str, body: ContactIn, svc: ServicesDep, p: Viewer,
                         request: Request) -> dict[str, Any]:
    return await _save_contact(svc, p, org_id, None, body, request)


@router.put("/api/platform/organizations/{org_id}/contacts/{contact_id}")
async def update_contact(org_id: str, contact_id: int, body: ContactIn, svc: ServicesDep,
                         p: Viewer, request: Request) -> dict[str, Any]:
    return await _save_contact(svc, p, org_id, contact_id, body, request)


@router.delete("/api/platform/organizations/{org_id}/contacts/{contact_id}", status_code=204)
async def delete_contact(org_id: str, contact_id: int, svc: ServicesDep, p: Viewer,
                         request: Request) -> None:
    _member(p, org_id, "org.members")
    if not await svc.store.home.delete_contact(org_id, contact_id):
        raise NotFound("contact not found")
    await svc.store.add_auth_audit("contact_deleted", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "contact_id": contact_id})


class PinIn(BaseModel):
    pin: str | None = Field(default=None, pattern=r"^[0-9]{4,8}$")   # None: SMS commands off


@router.put("/api/platform/organizations/{org_id}/sms-pin", status_code=204)
async def set_sms_pin(org_id: str, body: PinIn, svc: ServicesDep, p: Viewer,
                      request: Request) -> None:
    """The PIN an SMS command must carry (``شب 4821``); kept only as a hash."""
    _member(p, org_id, "org.members")
    from backend.security import hash_password

    await svc.store.home.set_sms_pin_hash(org_id, hash_password(body.pin) if body.pin else None)
    await svc.store.add_auth_audit("sms_pin_set", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"org_id": org_id, "on": body.pin is not None})


_SMS_TRIES: dict[str, list[float]] = {}


def _sms_throttled(phone: str, now: float) -> bool:
    """Five SMS commands per number in ten minutes (a guessed PIN costs time and money)."""
    tries = [t for t in _SMS_TRIES.get(phone, []) if now - t < 600]
    tries.append(now)
    _SMS_TRIES[phone] = tries[-20:]
    return len(tries) > 5


@router.post("/api/sms/inbound")
async def sms_inbound(request: Request, svc: ServicesDep,
                      token: Annotated[str | None, Query(max_length=200)] = None
                      ) -> dict[str, Any]:
    """The SMS provider forwards each received SMS here (``?token=HUB_SMS_INBOUND_TOKEN``;
    form or JSON with ``from`` and ``message``). A contact of a home marked for commands runs
    one of the home's scenes by its keyword and the home's PIN, in either order:
    ``شب 4821``. The sender is answered by SMS; anything else is answered by nothing."""
    import time as _time

    from backend.security import verify_password
    from backend.sms import normalize_phone

    expected = svc.settings.sms_inbound_token
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise Forbidden("bad token", code="forbidden")
    raw = (await request.body())[:4096]
    if "json" in request.headers.get("content-type", ""):
        try:
            data = json.loads(raw)
        except ValueError:
            return {"result": "ignored"}
        if not isinstance(data, dict):
            return {"result": "ignored"}
    else:                                        # form-encoded, or the query string (GET-style)
        from urllib.parse import parse_qsl

        data = dict(parse_qsl(raw.decode("utf-8", "replace"))) | {
            k: v for k, v in request.query_params.items() if k != "token"}
    sender = normalize_phone(str(data.get("from") or data.get("sender") or ""))
    words = str(data.get("message") or data.get("text") or "").split()
    if not sender or len(words) != 2 or _sms_throttled(sender, _time.monotonic()):
        return {"result": "ignored"}
    pin, keyword = (words[0], words[1]) if words[0].isdigit() else (words[1], words[0])
    for contact in await svc.store.home.contacts_by_phone([sender]):
        if not contact.command:
            continue
        pin_hash = await svc.store.home.sms_pin_hash(contact.org_id)
        if not pin_hash or not verify_password(pin_hash, pin):
            await svc.store.add_auth_audit("sms_command_refused", f"sms:{contact.contact_id}",
                                           detail={"org_id": contact.org_id})
            continue
        scenes = await svc.store.automations.scenes(None, contact.org_id)
        scene = next((s for s in scenes if s.sms_keyword and
                      s.sms_keyword.casefold() == keyword.casefold()), None)
        if scene is None:
            await svc.sms.send(sender, f"«{keyword}»: ?")
            return {"result": "unknown_keyword"}
        owner = await svc.automations._creator(scene.created_by) if scene.created_by else None
        if owner is None:
            return {"result": "failed"}
        detail = await svc.automations.run_scene(owner, scene)
        ok = all(d.get("result") in ("applied", "ok") for d in detail)
        await svc.store.add_auth_audit("sms_command", f"sms:{contact.contact_id}",
                                       detail={"org_id": contact.org_id,
                                               "scene_id": scene.scene_id, "ok": ok})
        await svc.sms.send(sender, f"{scene.name}: {'✓' if ok else '✗'}")
        return {"result": "ok" if ok else "partial", "scene_id": scene.scene_id}
    return {"result": "ignored"}


# --- countdown timers ----------------------------------------------------------------------------


class TimerIn(BaseModel):
    """``once``: set ``value`` in ``minutes``. ``cycle``: starting in ``minutes`` (0 = now),
    on for ``on_s`` and off for ``off_s``, ``count`` times (0 = until cancelled)."""

    tag: str = Field(pattern=f"^{SEGMENT_PATTERN}$")
    mode: Literal["once", "cycle"] = "once"
    value: bool | int | float = False
    minutes: int = Field(default=1, ge=0, le=24 * 60)
    on_s: float = Field(default=60, ge=1, le=86_400)
    off_s: float = Field(default=60, ge=1, le=86_400)
    count: int = Field(default=0, ge=0, le=10_000)


async def _operating_org(svc: ServicesDep, p: Principal, device_id: str) -> str:
    """One of the caller's homes that may run automations on the device."""
    for m in await svc.accounts.memberships(p):
        if p.can_in_org("automation.manage", m.org_id):
            level = (await svc.store.accounts.access_levels({m.org_id}, [device_id])).get(device_id)
            if level in ("owner", "operator"):
                return m.org_id
    raise NotFound("device not found")


@router.post("/api/platform/my/devices/{device_id}/timers", status_code=201)
async def create_timer(device_id: str, body: TimerIn, svc: ServicesDep, p: Viewer
                       ) -> dict[str, Any]:
    """"Turn relay 1 off in 30 minutes": a one-time automation that removes itself."""
    from backend.api.automations import _check_automation
    from backend.automations import AutomationIn

    org_id = await _operating_org(svc, p, device_id)
    if body.mode == "once" and body.minutes < 1:
        raise Invalid("a timer runs in 1 minute at the soonest", code="too_soon")
    at = (svc.hub.clock() + timedelta(minutes=body.minutes)).astimezone(TEHRAN)
    jy, jm, jd = jalali.to_jalali(at.date())
    action: dict[str, Any] = {"type": "write", "device_id": device_id, "tag": body.tag,
                              "value": body.value}
    if body.mode == "cycle":
        action = {"type": "cycle", "device_id": device_id, "tag": body.tag, "on_s": body.on_s,
                  "off_s": body.off_s, "count": body.count}
    spec = AutomationIn.model_validate({
        "name": f"timer {body.tag}", "trigger": {"type": "once", "date": jalali.fmt(jy, jm, jd),
                                                 "at": at.strftime("%H:%M")},
        "actions": [action], "missed": "catch_up", "catch_up_min": 60,
        "respect_manual_min": 0, "timer": True})
    await _check_automation(svc, org_id, spec)
    record = await svc.store.automations.save_automation(
        None, org_id, None, name=spec.name, enabled=True, definition=spec.definition(),
        user_id=p.user_id)
    if body.minutes == 0:                        # a cycle that starts now
        svc.automations.launch(record, spec, f"timer:{record.automation_id}")
    return {"automation_id": record.automation_id, "at": at.isoformat(), "tag": body.tag,
            "mode": body.mode, "value": body.value}


@router.get("/api/platform/my/devices/{device_id}/timers")
async def timers(device_id: str, svc: ServicesDep, p: Viewer) -> list[dict[str, Any]]:
    if await _my_level(svc, p, device_id, "device.view") is None:
        raise NotFound("device not found")
    out = []
    for m in await svc.accounts.memberships(p):
        if not p.can_in_org("device.view", m.org_id):
            continue
        for a in await svc.store.automations.automations(None, m.org_id):
            d = a.definition
            actions = d.get("actions", [])
            if d.get("timer") and actions and actions[0].get("device_id") == device_id:
                tr = d["trigger"]
                at = datetime.combine(jalali.to_gregorian(*jalali.parse(tr["date"])),
                                      datetime.strptime(tr["at"], "%H:%M").time(), TEHRAN)
                first = actions[0]
                item = {"automation_id": a.automation_id, "org_id": a.org_id,
                        "tag": first["tag"], "value": first.get("value"), "at": at.isoformat(),
                        "mode": "cycle" if first["type"] == "cycle" else "once",
                        "running": a.automation_id in svc.automations._running}
                if first["type"] == "cycle":
                    item |= {k: first[k] for k in ("on_s", "off_s", "count")}
                out.append(item)
    return sorted(out, key=lambda x: x["at"])


@router.delete("/api/platform/my/devices/{device_id}/timers/{automation_id}", status_code=204)
async def cancel_timer(device_id: str, automation_id: int, svc: ServicesDep, p: Viewer) -> None:
    for t in await timers(device_id, svc, p):
        if t["automation_id"] == automation_id:
            _member(p, t["org_id"], "automation.manage")
            await svc.store.automations.delete_automation(None, t["org_id"], automation_id)
            svc.automations.cancel(automation_id)       # a running cycle stops (output off)
            return
    raise NotFound("timer not found")


# --- energy and cost -----------------------------------------------------------------------------


def _tier_cost(kwh: float, tariff: dict[str, Any]) -> float:
    cost, left, floor = 0.0, kwh, 0.0
    for tier in tariff.get("tiers") or []:
        top = tier.get("up_to_kwh")
        size = left if top is None else max(min(left, top - floor), 0)
        cost += size * float(tier.get("price", 0))
        left -= size
        if top is not None:
            floor = top
        if left <= 0:
            break
    return cost


def _in_window(t: datetime, start: str | None, end: str | None) -> bool:
    if not start or not end:
        return False
    hm = t.strftime("%H:%M")
    return start <= hm < end if start <= end else (hm >= start or hm < end)


@router.get("/api/platform/my/energy")
async def energy(svc: ServicesDep, p: Viewer, org: OrgQuery = None,
                 days: Annotated[int, Query(ge=1, le=92)] = 31) -> dict[str, Any]:
    """kWh per day (Tehran days) and per device for the last ``days`` days, from kWh counters
    (daily rise) or else power readings (hourly averages); the current Jalali month's total
    and its cost with the home's tariff (tiers, peak hours, fixed charge, tax)."""
    orgs = await _my_orgs(svc, p, org)
    now = datetime.now(UTC)
    local_today = now.astimezone(TEHRAN).date()
    first_day = local_today - timedelta(days=days - 1)
    start = datetime.combine(first_day, datetime.min.time(), TEHRAN)
    jy, jm, _ = jalali.to_jalali(local_today)
    month_start = datetime.combine(jalali.to_gregorian(jy, jm, 1), datetime.min.time(), TEHRAN)
    start = min(start, month_start)
    per_day: dict[date, float] = {}
    peak_kwh = 0.0
    devices: list[dict[str, Any]] = []
    tariff = {}
    if org is not None:
        tariff = (await svc.store.home.settings(org)).get("tariff") or {}
    rows, _ = await svc.store.platform.devices(orgs, limit=200) if orgs else ([], 0)
    for d in await _mine(svc, orgs, rows) if orgs else []:
        template = await svc.store.platform.template(d.product_id, d.version)
        counters = [t for t in template.tags if (t.unit or "") == "kWh"]
        powers = [t for t in template.tags if (t.unit or "") in ("kW", "W")]
        total = 0.0
        if counters:
            path = f"platform.devices.{d.device_id}.{counters[0].key}"
            for r in await svc.store.history(path, start, now, max_points=days + 40):
                lo, hi = (r.min, r.max) if r.avg is not None else (None, None)
                if lo is None or hi is None:
                    continue
                day = r.ts.astimezone(TEHRAN).date()
                per_day[day] = per_day.get(day, 0.0) + max(hi - lo, 0.0)
                total += max(hi - lo, 0.0)
        elif powers:
            tag = powers[0]
            path = f"platform.devices.{d.device_id}.{tag.key}"
            hours = int((now - start).total_seconds() // 3600) + 1
            for r in await svc.store.history(path, start, now, max_points=max(hours, 10)):
                value = _number(r.avg if r.avg is not None else r.value)
                if value is None:
                    continue
                kwh = value / (1000 if tag.unit == "W" else 1)     # an hour's average kW
                local = r.ts.astimezone(TEHRAN)
                per_day[local.date()] = per_day.get(local.date(), 0.0) + kwh
                total += kwh
                if local >= month_start and _in_window(local, tariff.get("peak_from"),
                                                       tariff.get("peak_to")):
                    peak_kwh += kwh
        if counters or powers:
            devices.append({"device_id": d.device_id, "name": d.name, "kwh": round(total, 2)})
    month_kwh = sum(v for k, v in per_day.items() if k >= month_start.date())
    cost = None
    if tariff.get("tiers"):
        base = _tier_cost(month_kwh, tariff)
        avg_price = base / month_kwh if month_kwh else 0
        base += peak_kwh * avg_price * (float(tariff.get("peak_factor", 1)) - 1)
        base += float(tariff.get("fixed", 0))
        cost = round(base * (1 + float(tariff.get("tax_percent", 0)) / 100))
    daily = [{"day": (first_day + timedelta(days=i)).isoformat(),
              "jalali": jalali.fmt(*jalali.to_jalali(first_day + timedelta(days=i))),
              "kwh": round(per_day.get(first_day + timedelta(days=i), 0.0), 2)}
             for i in range(days)]
    return {"days": daily, "devices": sorted(devices, key=lambda x: -x["kwh"]),
            "month": {"jalali": f"{jy}/{jm:02d}", "kwh": round(month_kwh, 2),
                      "peak_kwh": round(peak_kwh, 2), "cost": cost,
                      "currency": tariff.get("currency", "ریال")}}


# --- SMS fallback --------------------------------------------------------------------------------


class SmsIn(BaseModel):
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{6,15}$")
    new_key: bool = False             # make a new key (after a phone with the old one was lost)


def sms_signature(key: str, device_id: str, counter: int, items: list[str]) -> str:
    """The signature an SMS command ends with: the first 8 bytes of
    HMAC-SHA256(key, "<device_id> <counter> <item> <item> ...") in hex (the app's and the
    device's shared rule; ``key`` is the base64url text)."""
    raw = base64.urlsafe_b64decode(key + "=")
    text = " ".join([device_id, str(counter), *items]).encode()
    return hmac.new(raw, text, hashlib.sha256).hexdigest()[:16]


async def _owner_home(svc: ServicesDep, p: Principal, device_id: str) -> str:
    orgs = p.orgs_with("device.share")
    owner = await svc.store.accounts.owner_org(orgs, device_id) if orgs != set() else None
    if owner is None:
        raise NotFound("device not found")
    return owner


@router.get("/api/platform/my/devices/{device_id}/sms")
async def sms_settings(device_id: str, svc: ServicesDep, p: Viewer) -> dict[str, Any]:
    """The SMS fallback (owner and operators): the SIM number and the signing key, which the
    app keeps on the phone to sign messages while there is no internet."""
    if await _my_level(svc, p, device_id, "device.operate") not in ("owner", "operator"):
        raise NotFound("device not found")
    row = await svc.store.home.sms(device_id)
    return {"enabled": row is not None, "phone": row["phone"] if row else None,
            "key": row["key"] if row else None}


@router.put("/api/platform/my/devices/{device_id}/sms")
async def save_sms(device_id: str, body: SmsIn, svc: ServicesDep, p: Viewer,
                   request: Request) -> dict[str, Any]:
    """The owner switches the SMS fallback on: the hub makes the device's signing key (kept
    unless ``new_key``), the device gets it now over its own connection
    (``{"type": "config", "sms": {...}}``) and accepts only messages signed with it."""
    org = await _owner_home(svc, p, device_id)
    row = await svc.store.home.sms(device_id)
    key = row["key"] if row and not body.new_key else \
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    await svc.store.home.save_sms(device_id, org, body.phone, key, p.user_id)
    sent = await svc.ingest.send_message(device_id, {"type": "config", "sms": {
        "enabled": True, "key": key, "device_id": device_id}})
    await svc.store.add_auth_audit("sms_settings_saved", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"device_id": device_id, "sent": sent,
                                           "new_key": body.new_key or row is None})
    return {"enabled": True, "phone": body.phone, "key": key, "sent_to_device": sent}


@router.delete("/api/platform/my/devices/{device_id}/sms", status_code=204)
async def delete_sms(device_id: str, svc: ServicesDep, p: Viewer) -> None:
    await _owner_home(svc, p, device_id)
    await svc.store.home.delete_sms(device_id)
    await svc.ingest.send_message(device_id, {"type": "config", "sms": {"enabled": False}})


# --- live updates --------------------------------------------------------------------------------


async def _events(svc: ServicesDep, p: Principal, org: str | None) -> AsyncIterator[str]:
    async def allowed() -> set[str]:
        orgs = await _my_orgs(svc, p, org)
        if not orgs:
            return set()
        rows, _ = await svc.store.platform.devices(orgs, limit=500)
        return set(await svc.store.accounts.access_levels(orgs, [r.device_id for r in rows]))

    devices = await allowed()
    queue = svc.hub.bus.subscribe()
    loop = asyncio.get_running_loop()
    recheck = loop.time() + 60
    ends = loop.time() + STREAM_S          # then the app reconnects with a fresh token
    try:
        yield "retry: 3000\n\n"
        while loop.time() < ends:
            try:
                event = await asyncio.wait_for(queue.get(), 15)
            except TimeoutError:
                yield ": keep-alive\n\n"
                event = {}
            if loop.time() > recheck:              # devices added or shares ended meanwhile
                devices = await allowed()
                recheck = loop.time() + 60
            if event is None:
                return
            kind = event.get("type")
            if kind == "samples":
                mine = [s for s in event["samples"]
                        if s["tag"].startswith("platform.devices.") and
                        s["tag"].split(".")[2] in devices]
                if mine:
                    yield f"event: samples\ndata: {json.dumps(mine, ensure_ascii=False)}\n\n"
            elif kind == "status" and event.get("device_id") in devices:
                yield f"event: status\ndata: {json.dumps(event)}\n\n"
    finally:
        svc.hub.bus.unsubscribe(queue)


@router.get("/api/platform/my/stream")
async def stream(svc: ServicesDep, p: Viewer, org: OrgQuery = None) -> StreamingResponse:
    """Server-sent events for the caller's devices: ``samples`` (tag, value, ts, quality) and
    ``status`` (online / offline). Read with fetch() and the bearer token (EventSource cannot
    send headers); a keep-alive every 15 s; the stream ends after five minutes so the app
    reconnects with a current token (a signed-out session gets no new one)."""
    return StreamingResponse(_events(svc, p, org), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# --- tickets (buyer side) ------------------------------------------------------------------------


class TicketIn(BaseModel):
    kind: Literal["support", "warranty"]
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class TicketOut(BaseModel):
    ticket: TicketRecord
    messages: list[MessageRecord]


async def ticket_mail(svc: ServicesDep, ticket: TicketRecord, template: str, text: str,
                      to_maker: bool) -> int:
    """E-mail the other side of a ticket: the maker (its support address, else its product
    managers and support staff) about a new ticket or a buyer's reply, the buyer about the
    maker's answer. A mail that cannot be sent never fails the request; returns how many."""
    from backend.mailer import label, render

    base = (svc.settings.public_url or "").rstrip("/")
    origin = base[:-4] if base.endswith("/app") else base
    if to_maker:
        brand = await svc.store.home.branding(ticket.maker_org_id)
        if brand.get("support_email"):
            people = [(brand["support_email"], "fa")]
        else:
            people = [(m.email, "fa") for m in await svc.store.accounts.members(ticket.maker_org_id)
                      if m.email and m.role_id in ("manufacturer_admin", "manufacturer_support")]
        link = f"{origin}/maker/#/support/tickets/{ticket.ticket_id}"
    else:
        user = await svc.store.get_user(await _opener(svc, ticket))
        people = [(user.email, user.language)] if user and user.email and user.is_active else []
        link = f"{base}/#/ticket/{ticket.ticket_id}"
    sent = 0
    for email, language in people[:10]:
        subject, body = render(template, language, subject=ticket.subject,
                               identifier=ticket.identifier, product=ticket.product_name,
                               name=ticket.opened_by_name, text=text[:2000], link=link,
                               kind=label("ticket_kinds", ticket.kind, language),
                               status=label("ticket_statuses", ticket.status, language))
        try:
            await svc.accounts.mailer.send(email, subject, body)
            sent += 1
        except Exception:  # noqa: BLE001 - the ticket is saved; the mail is a courtesy
            log.warning("ticket %s: mail to %s not sent", ticket.ticket_id, email[:3] + "***")
    return sent


async def _opener(svc: ServicesDep, ticket: TicketRecord) -> int:
    async with svc.store._conn() as conn:
        row = await (await conn.execute("SELECT opened_by FROM tickets WHERE ticket_id=%s",
                                        (ticket.ticket_id,))).fetchone()
    return int(row["opened_by"])


@router.post("/api/platform/my/devices/{device_id}/tickets", status_code=201)
async def open_ticket(device_id: str, body: TicketIn, svc: ServicesDep, p: Viewer,
                      request: Request) -> TicketOut:
    """A support question or a warranty claim to the device's maker. The maker sees the
    device, the text and the sender's name (the buyer chose to write) — nothing else."""
    org = await _owner_home(svc, p, device_id)
    record = await svc.store.platform.device(None, device_id)
    if body.kind == "warranty" and not (record.warranty_until and
                                        record.warranty_until > svc.hub.clock()):
        raise Invalid("the device is not under warranty", code="no_warranty")
    ticket_id = await svc.store.home.open_ticket(
        device_id=device_id, maker_org_id=record.maker_org_id, owner_org_id=org,
        user_id=p.user_id, author_name=p.user.display_name, kind=body.kind,
        subject=body.subject, body=body.body)
    await svc.store.add_auth_audit("ticket_opened", p.username, user_id=p.user_id,
                                   client_addr=client_addr(request),
                                   detail={"ticket_id": ticket_id, "device_id": device_id})
    found = await svc.store.home.ticket(ticket_id)
    assert found is not None
    await ticket_mail(svc, found[0], "ticket_new", body.body, to_maker=True)
    return TicketOut(ticket=found[0], messages=found[1])


async def _buyer_ticket(svc: ServicesDep, p: Principal, ticket_id: int
                        ) -> tuple[TicketRecord, list[MessageRecord]]:
    found = await svc.store.home.ticket(ticket_id)
    if found is None or not p.can_in_org("device.share", found[0].owner_org_id):
        raise NotFound("ticket not found")
    return found


@router.get("/api/platform/my/tickets")
async def my_tickets(svc: ServicesDep, p: Viewer, device: str | None = None
                     ) -> list[TicketRecord]:
    orgs = [m.org_id for m in await svc.accounts.memberships(p)
            if p.can_in_org("device.share", m.org_id)]
    return await svc.store.home.tickets(owner_orgs=orgs, device_id=device) if orgs else []


@router.get("/api/platform/my/tickets/{ticket_id}")
async def my_ticket(ticket_id: int, svc: ServicesDep, p: Viewer) -> TicketOut:
    ticket, messages = await _buyer_ticket(svc, p, ticket_id)
    return TicketOut(ticket=ticket, messages=messages)


@router.post("/api/platform/my/tickets/{ticket_id}/messages")
async def reply_ticket(ticket_id: int, body: MessageIn, svc: ServicesDep, p: Viewer
                       ) -> TicketOut:
    ticket, _ = await _buyer_ticket(svc, p, ticket_id)
    if ticket.status == "closed":
        raise Invalid("the ticket is closed", code="ticket_closed")
    await svc.store.home.add_message(ticket_id, "buyer", p.user_id, p.user.display_name,
                                     body.body, "open")
    found = await svc.store.home.ticket(ticket_id)
    assert found is not None
    await ticket_mail(svc, found[0], "ticket_reply", body.body, to_maker=True)
    return TicketOut(ticket=found[0], messages=found[1])


# --- commands with several parameters ----------------------------------------------------------


class RunCommandIn(BaseModel):
    values: dict[str, bool | int | float] = Field(default_factory=dict, max_length=50)


@router.post("/api/platform/my/devices/{device_id}/actions/{key}")
async def run_command(device_id: str, key: str, body: RunCommandIn, svc: ServicesDep,
                      p: Viewer, request: Request) -> dict[str, Any]:
    """A command of the product template that sets several parameters (fixed values, or the
    ones the user entered): each goes through the control-safety layer in order; it stops at
    the first refused one."""
    if await _my_level(svc, p, device_id, "device.operate") not in ("owner", "operator"):
        raise NotFound("device not found")
    record = await svc.store.platform.device(None, device_id)
    template = await svc.store.platform.template(record.product_id, record.version)
    command = next((c for c in template.commands if c.key == key), None)
    if command is None:
        raise NotFound("no such command", code="unknown_command")
    plan = [(prm.tag, prm.value if prm.value is not None else body.values.get(prm.tag))
            for prm in command.params]
    missing = [tag for tag, value in plan if value is None]
    if missing:                                # all values first: nothing half-sent
        raise Invalid(f"a value for {', '.join(missing)} is needed", code="missing_value")
    results = []
    for tag, value in plan:
        result = await svc.control.request_write(
            p, f"platform.devices.{device_id}.{tag}", value, f"command:{key}",
            client_addr(request))
        results.append({"tag": tag, "value": value, "result": result.result})
        if result.result != "applied":
            break
    ok = all(r["result"] == "applied" for r in results) and len(results) == len(command.params)
    return {"command": key, "ok": ok, "results": results}


# --- rules the device runs by itself while cut off ---------------------------------------------


LOCAL_TRIGGERS = ("time", "interval", "value")


async def local_rules(svc: ServicesDep, device_id: str) -> dict[str, Any]:
    """The owner home's automations marked ``local`` that only read and write this device,
    in the form the device runs them: tags by key, Jalali weekdays (0 = Saturday), the
    hub's UTC offset and the coming holidays."""
    from backend.automations import AutomationIn

    owner = await svc.store.accounts.owner_org(None, device_id)
    rules: list[dict[str, Any]] = []
    if owner is not None:
        for a in await svc.store.automations.automations(None, owner):
            if not a.enabled:
                continue
            try:
                spec = AutomationIn.from_record(a)
            except ValueError:
                continue
            d = spec.definition()
            if not spec.local or d["trigger"]["type"] not in LOCAL_TRIGGERS:
                continue
            refs = [c["device_id"] for c in d["conditions"]]
            refs += [x["device_id"] for x in d["actions"] if x["type"] == "write"]
            if d["trigger"]["type"] == "value":
                refs.append(d["trigger"]["device_id"])
            if any(r != device_id for r in refs) or \
                    any(x["type"] not in ("write", "delay") for x in d["actions"]):
                continue                         # needs another device or the hub

            def strip(x: dict[str, Any]) -> dict[str, Any]:
                return {k: v for k, v in x.items() if k != "device_id"}
            rules.append({"id": a.automation_id, "name": a.name, "trigger": strip(d["trigger"]),
                          "conditions": [strip(c) for c in d["conditions"]],
                          "window": d.get("window"),
                          "actions": [strip(x) for x in d["actions"]]})
    eng = svc.automations
    today = eng.clock().astimezone(eng.tz).date()
    holidays = sorted(h.isoformat() for h in await eng.holidays(owner or "", today,
                                                                 today + timedelta(days=60))) \
        if owner else []
    holidays += [(today + timedelta(days=i)).isoformat() for i in range(60)
                 if jalali.fixed_holiday(today + timedelta(days=i)) is not None]
    offset = eng.clock().astimezone(eng.tz).utcoffset() or timedelta()
    body = {"rules": rules, "utc_offset_min": int(offset.total_seconds() // 60),
            "holidays": sorted(set(holidays)), "offline_after_s": 60}
    version = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    # not part of the version: the device sets its clock from it when it has no NTP
    now = eng.clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {"type": "rules", "version": version} | body | {"server_time": now}


async def push_local_rules(svc: ServicesDep, org_id: str) -> None:
    """After a home's automations changed: every running device the home owns gets its rules
    again (the device keeps the last set it received)."""
    with contextlib.suppress(Exception):
        rows, _ = await svc.store.platform.devices({org_id}, limit=500)
        levels = await svc.store.accounts.access_levels({org_id}, [r.device_id for r in rows])
        for r in rows:
            if levels.get(r.device_id) == "owner" and r.protocol == "native" and \
                    svc.ingest.driver(r.device_id) is not None:
                await svc.ingest.send_message(r.device_id, await local_rules(svc, r.device_id))


@router.get("/api/platform/my/devices/{device_id}/local-rules")
async def my_local_rules(device_id: str, svc: ServicesDep, p: Viewer) -> dict[str, Any]:
    """What the device runs by itself while cut off (for the app to show)."""
    if await _my_level(svc, p, device_id, "device.view") is None:
        raise NotFound("device not found")
    return await local_rules(svc, device_id)


@router.get("/ingest/v1/devices/{device_id}/rules")
async def device_rules(device_id: str, svc: ServicesDep, request: Request,
                       authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """The device fetches its rules (with its credential) after it starts or reconnects."""
    from backend.api.ingest import _device

    await _device(svc, device_id, authorization, request)
    return await local_rules(svc, device_id)
