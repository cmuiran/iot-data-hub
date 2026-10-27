"""Claiming devices: labels with claim codes, the buyer's claim, transfer, release, the
manufacturer's reset, device credentials, and devices behind an edge hub.

* A **claim code** is 12 characters (60 random bits, Crockford base32, printed as
  ``XXXX-XXXX-XXXX``) and goes on the label next to the identifier, also as a QR code. Only an
  HMAC of it is stored (key: HUB_DATA_KEY, else the JWT secret): a copy of the database alone
  does not allow guessing codes offline. A code works once.
* Claiming needs identifier **and** code. Every failure answers the same ``invalid_claim``
  (unknown identifier, wrong code, used code, locked, disabled or retired device), so the
  answer tells nothing about which devices exist. A device locks for CLAIM_LOCK_MINUTES after
  CLAIM_MAX_ATTEMPTS wrong codes; callers are rate limited per user and per address.
* A device owned by someone can only be claimed with a **transfer** code its owner asked for;
  the claim then moves the device (and the devices behind it) and ends every earlier access.
* A **device credential** is a 256-bit secret for the device itself (flashed in production;
  MQTT / HTTP / TCP sign-in from platform phase 4). Only its SHA-256 is stored.
* An **edge hub** (product kind edge_hub) has devices behind it: its owner adds them from the
  product catalog (or the hub maker's products); they belong to the hub's owner and move with it.

Codes and secrets are shown once (label CSV, transfer answer) and never logged.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import secrets
from typing import Any
from uuid import UUID

from backend.errors import Conflict, Invalid, NotFound
from backend.rbac import Principal
from hub.drivers.base import utcnow
from hub.storage.platform import PlatformError

log = logging.getLogger(__name__)

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"          # Crockford base32
CODE_LENGTH = 12
CLAIM_MAX_ATTEMPTS = 5
CLAIM_LOCK_MINUTES = 15
CLAIMS_PER_USER_HOUR = 10
_LOOKALIKE = str.maketrans({"O": "0", "I": "1", "L": "1"})
INVALID = "identifier or claim code is not valid"


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def normalize_code(value: str) -> str:
    """What a person types: spaces and dashes ignored, O/I/L read as 0/1, any case."""
    return "".join(value.upper().split()).replace("-", "").translate(_LOOKALIKE)


def new_secret() -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    return secret, hashlib.sha256(secret.encode("ascii")).hexdigest()


class ClaimService:
    def __init__(self, svc: Any, clock: Any = utcnow) -> None:
        self.svc = svc
        self.clock = clock
        key = svc.settings.data_key or svc.settings.jwt_secret
        if not svc.settings.data_key:
            log.warning("HUB_DATA_KEY is not set: claim codes are keyed with the JWT secret "
                        "(changing it makes printed codes invalid)")
        self._key = key.encode("utf-8")

    @property
    def store(self) -> Any:
        return self.svc.store

    def code_hash(self, device_id: str, code: str) -> str:
        digest = hmac.new(self._key, f"{device_id}:{normalize_code(code)}".encode(),
                          hashlib.sha256).hexdigest()
        return f"hmac-sha256${digest}"

    def qr(self, identifier: str, code: str) -> str:
        base = self.svc.settings.public_url or ""
        return f"{base}/#/claim/{identifier}/{format_code(code)}"

    # --- labels (manufacturer) ----------------------------------------------------------------

    async def labels(self, principal: Principal, product_id: UUID, *,
                     device_ids: list[str] | None, batch: str | None,
                     credentials: bool, client_addr: str | None) -> str:
        """New claim codes (and optionally device credentials) for devices of a product that
        nobody owns yet, as CSV. Earlier codes of these devices stop working."""
        orgs = principal.orgs_with("device.register")
        try:
            product = await self.store.platform.product(orgs, product_id)
        except PlatformError:
            raise NotFound("product not found") from None
        if not principal.can_in_org("device.register", product.org_id):
            raise NotFound("product not found")
        rows: list[Any] = []
        offset = 0
        while True:                       # every device of the product, page by page
            page, total = await self.store.platform.devices(orgs, product_id=product_id,
                                                            limit=500, offset=offset)
            rows += page
            offset += 500
            if offset >= total:
                break
        if device_ids is not None:
            wanted = set(device_ids)
            rows = [r for r in rows if r.device_id in wanted]
        if batch is not None:
            rows = [r for r in rows if r.batch == batch]
        rows = [r for r in rows if r.lifecycle in ("registered", "ready", "sold")
                and r.parent_device_id is None]
        if not rows:
            raise NotFound("no unclaimed devices match", code="no_devices")
        owned = await self._owned_any([r.device_id for r in rows])
        rows = [r for r in rows if r.device_id not in owned]
        codes = {r.device_id: new_code() for r in rows}
        await self.store.claims.issue_codes(
            {d: self.code_hash(d, c) for d, c in codes.items()}, "activation",
            principal.user_id)
        secrets_ = {}
        if credentials:
            pairs = {r.device_id: new_secret() for r in rows}
            await self.store.claims.issue_credentials(
                {d: h for d, (_, h) in pairs.items()}, principal.user_id)
            secrets_ = {d: s for d, (s, _) in pairs.items()}
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["identifier", "serial_number", "claim_code", "qr"]
                        + (["device_id", "device_secret"] if credentials else []))
        for r in rows:
            code = codes[r.device_id]
            writer.writerow([r.identifier, r.serial_number or "", format_code(code),
                             self.qr(r.identifier, code)]
                            + ([r.device_id, secrets_[r.device_id]] if credentials else []))
        await self.store.add_auth_audit(
            "claim_codes_issued", principal.username, user_id=principal.user_id,
            client_addr=client_addr, detail={"product_id": str(product_id), "count": len(rows),
                                             "credentials": credentials})
        return out.getvalue()

    async def _owned_any(self, device_ids: list[str]) -> set[str]:
        async with self.store._conn() as conn:
            rows = await (await conn.execute(
                "SELECT device_id FROM device_access WHERE device_id = ANY(%s) AND "
                "level='owner' AND revoked_at IS NULL", (device_ids,))).fetchall()
        return {r["device_id"] for r in rows}

    # --- claim (buyer) ------------------------------------------------------------------------

    async def claim(self, principal: Principal, identifier: str, code: str,
                    org_id: str | None, name: str | None, client_addr: str | None
                    ) -> dict[str, Any]:
        accounts = self.svc.accounts
        await accounts._ip_limit("claim", client_addr)
        await accounts._limit("claim_user", str(principal.user_id), CLAIMS_PER_USER_HOUR,
                              client_addr)
        if org_id is None:
            org_id = await self.store.accounts.ensure_personal_org(principal.user,
                                                                   principal.user.display_name)
        elif not principal.can_in_org("org.members", org_id):
            raise NotFound("organization not found")
        now = self.clock()
        target = await self.store.claims.target(identifier.strip())
        ok = (target is not None and not target.retired and target.lifecycle != "disabled"
              and target.parent_device_id is None and target.code_hash is not None
              and target.used_at is None
              and not (target.locked_until and target.locked_until > now)
              and (target.owner_org is None or target.purpose == "transfer"))
        if ok and target is not None:
            ok = hmac.compare_digest(target.code_hash or "",
                                     self.code_hash(target.device_id, code))
            if not ok:
                await self.store.claims.record_failure(target.device_id, now,
                                                       CLAIM_MAX_ATTEMPTS, CLAIM_LOCK_MINUTES)
        if not ok or target is None:
            await self.store.add_auth_audit(
                "claim_failed", principal.username, user_id=principal.user_id,
                client_addr=client_addr,
                detail={"device_id": target.device_id if target else None})
            raise Invalid(INVALID, code="invalid_claim")
        if target.owner_org == org_id:
            raise Conflict("the device already belongs to this organization",
                           code="already_owner")
        if not await self.store.claims.complete_claim(target.device_id, org_id,
                                                      principal.user_id, now):
            raise Invalid(INVALID, code="invalid_claim")
        if name:
            await self.store.platform.update_device(None, target.device_id, name=name)
        await self.store.add_auth_audit(
            "device_claimed", principal.username, user_id=principal.user_id,
            client_addr=client_addr,
            detail={"device_id": target.device_id, "org_id": org_id,
                    "transfer": target.owner_org is not None})
        return {"device_id": target.device_id, "identifier": target.identifier,
                "organization": org_id, "transferred": target.owner_org is not None}

    # --- owner actions ------------------------------------------------------------------------

    async def _owner(self, principal: Principal, device_id: str) -> str:
        orgs = principal.orgs_with("device.share")
        owner = await self.store.accounts.owner_org(orgs, device_id) if orgs != set() else None
        if owner is None:
            raise NotFound("device not found")
        return owner

    async def transfer(self, principal: Principal, device_id: str, client_addr: str | None
                       ) -> dict[str, Any]:
        """A one-time code for the next owner; the device stays the caller's until claimed."""
        await self._owner(principal, device_id)
        target = await self._record(device_id)
        if target.parent_device_id is not None:
            raise Invalid("a device behind an edge hub moves with its hub",
                          code="child_device")
        code = new_code()
        await self.store.claims.issue_codes({device_id: self.code_hash(device_id, code)},
                                            "transfer", principal.user_id)
        await self.store.add_auth_audit("device_transfer_started", principal.username,
                                        user_id=principal.user_id, client_addr=client_addr,
                                        detail={"device_id": device_id})
        return {"identifier": target.identifier, "claim_code": format_code(code),
                "qr": self.qr(target.identifier, code)}

    async def release(self, principal: Principal, device_id: str, client_addr: str | None
                      ) -> None:
        """The owner removes the device from its account (and every share ends)."""
        await self._owner(principal, device_id)
        target = await self._record(device_id)
        if target.parent_device_id is not None:
            raise Invalid("remove it through its edge hub", code="child_device")
        ids = await self.store.claims.release(device_id, "sold", self.clock())
        await self._stop(ids)
        await self.store.add_auth_audit("device_released", principal.username,
                                        user_id=principal.user_id, client_addr=client_addr,
                                        detail={"device_id": device_id, "devices": len(ids)})

    async def reset(self, principal: Principal, device_id: str, client_addr: str | None
                    ) -> dict[str, Any]:
        """The manufacturer's factory reset: every access ends, a new activation code."""
        orgs = principal.orgs_with("device.register")
        try:
            record = await self.store.platform.device(orgs, device_id)
        except PlatformError:
            raise NotFound("device not found") from None
        if not principal.can_in_org("device.register", record.maker_org_id) or \
                record.parent_device_id is not None:
            raise NotFound("device not found")
        ids = await self.store.claims.release(device_id, "ready", self.clock())
        await self._stop(ids)
        code = new_code()
        await self.store.claims.issue_codes({device_id: self.code_hash(device_id, code)},
                                            "activation", principal.user_id)
        await self.store.add_auth_audit("device_reset", principal.username,
                                        user_id=principal.user_id, client_addr=client_addr,
                                        detail={"device_id": device_id, "devices": len(ids)})
        return {"identifier": record.identifier, "claim_code": format_code(code),
                "qr": self.qr(record.identifier, code)}

    async def configure(self, principal: Principal, device_id: str, changes: dict[str, Any],
                        client_addr: str | None) -> Any:
        """The owner sets name, connection, options and enabled (e.g. the Modbus address of
        its device); validated like any device before it runs."""
        await self._owner(principal, device_id)
        try:
            record, model = await self.store.platform.update_device(None, device_id, **changes)
        except PlatformError as exc:
            if exc.code == "not_found":
                raise NotFound(str(exc)) from None
            raise Invalid(str(exc), code=exc.code) from None
        outcome = await self.svc.hub.apply_platform_device(device_id, model)
        await self.store.add_auth_audit(
            "platform_device_updated", principal.username, user_id=principal.user_id,
            client_addr=client_addr,
            detail={"device_id": device_id, "hub": outcome, "changed": sorted(changes),
                    "by": "owner"})
        return record

    async def _record(self, device_id: str) -> Any:
        return await self.store.platform.device(None, device_id)

    async def _stop(self, device_ids: list[str]) -> None:
        await self.store.claims.disable(device_ids)
        for d in device_ids:
            await self.svc.hub.apply_platform_device(d, None)

    # --- edge hubs ----------------------------------------------------------------------------

    async def add_child(self, principal: Principal, hub_id: str, *, product_id: UUID,
                        version: int, protocol: str, name: str | None,
                        connection: dict[str, Any], options: dict[str, Any], enabled: bool,
                        client_addr: str | None) -> Any:
        owner = await self._owner(principal, hub_id)
        hub = await self._record(hub_id)
        if hub.product_kind != "edge_hub":
            raise Invalid("devices can be added behind an edge hub only", code="not_edge_hub")
        product = await self._catalog_product(product_id, hub.maker_org_id)
        identifier = await self.store.claims.next_child_identifier(hub.identifier)
        try:
            (child,) = await self.store.platform.register_devices(
                None, product_id, version, protocol=protocol, entries=[(identifier, None)],
                batch=None, warranty_months=None, connection=connection, options=options,
                enabled=enabled, user_id=principal.user_id, parent_device_id=hub_id,
                names=[name or f"{product.name} ({identifier})"])
        except PlatformError as exc:
            if exc.code == "not_found":
                raise NotFound(str(exc)) from None
            raise Invalid(str(exc), code=exc.code) from None
        await self.store.accounts.set_owner(child.device_id, owner, principal.user_id)
        if enabled:
            (model,) = await self.store.platform.device_models([child.device_id])
            await self.svc.hub.apply_platform_device(child.device_id, model)
        await self.store.add_auth_audit(
            "child_device_added", principal.username, user_id=principal.user_id,
            client_addr=client_addr, detail={"hub": hub_id, "device_id": child.device_id,
                                             "product_id": str(product_id)})
        return await self.store.platform.device(None, child.device_id)

    async def _catalog_product(self, product_id: UUID, hub_maker: str) -> Any:
        try:
            product = await self.store.platform.product(None, product_id)
        except PlatformError:
            raise NotFound("product not found") from None
        if not (product.catalog or product.org_id == hub_maker) or product.kind == "edge_hub":
            raise NotFound("product not found")
        return product

    async def remove_child(self, principal: Principal, hub_id: str, child_id: str,
                           client_addr: str | None) -> None:
        await self._owner(principal, hub_id)
        if child_id not in await self.store.claims.children(hub_id):
            raise NotFound("device not found")
        await self.store.claims.retire(child_id, self.clock())
        await self.svc.hub.apply_platform_device(child_id, None)
        await self.store.add_auth_audit("child_device_removed", principal.username,
                                        user_id=principal.user_id, client_addr=client_addr,
                                        detail={"hub": hub_id, "device_id": child_id})

    # --- device credentials (for the protocol front ends of phase 4) ---------------------------

    async def credential_valid(self, device_id: str, secret: str) -> bool:
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        return await self.store.claims.credential_valid(device_id, digest)
