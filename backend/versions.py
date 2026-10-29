"""Template version upgrades and firmware (platform phase 7).

Upgrading: devices move to another published version of their product. Tags with the same key
keep their tag id and history; new keys are added; vanished keys are retired (history stays).
A *breaking* move (a tag removed, its data type changed, no longer writable, the protocol's
binding gone) needs ``allow_breaking``; the preview lists what breaks and the automations and
scenes that use the devices. Each enabled device is rebuilt and validated first: a device whose
configuration would not be valid stays on its version (reported, the others go on).

Firmware: the manufacturer uploads an image per product and version (SHA-256 kept), rolls it
out to devices (one job each), the hub offers it over the device's transport (TCP / MQTT
``d/<id>/ota`` / the HTTP queue), the device downloads it with its credential, checks the
SHA-256 (and its own signature) and reports downloading / installed / failed with its version.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any
from uuid import UUID

from backend.errors import Conflict, Invalid, NotFound
from backend.rbac import Principal
from hub.platform.template import ProductTemplate, build_tags, calc_rows, diff_templates
from hub.storage.platform import PlatformError

log = logging.getLogger(__name__)
MAX_IMAGE = 16 * 1024 * 1024


class VersionService:
    def __init__(self, svc: Any) -> None:
        self.svc = svc

    @property
    def store(self) -> Any:
        return self.svc.store

    async def _product(self, p: Principal, product_id: UUID) -> Any:
        try:
            product = await self.store.platform.product(p.orgs_with("product.manage"),
                                                        product_id)
        except PlatformError:
            raise NotFound("product not found") from None
        if not p.can_in_org("product.manage", product.org_id):
            raise NotFound("product not found")
        return product

    async def _published(self, product_id: UUID, version: int) -> ProductTemplate:
        record = await self.store.platform.version(None, product_id, version)
        if record.status != "published":
            raise Conflict(f"version {version} is {record.status}", code="not_published")
        return ProductTemplate.model_validate(record.definition)

    # --- upgrades -----------------------------------------------------------------------------

    async def preview(self, p: Principal, product_id: UUID, to_version: int,
                      from_version: int | None) -> dict[str, Any]:
        await self._product(p, product_id)
        new = await self._published(product_id, to_version)
        devices = [d for d in await self.store.versions.devices_of(product_id, from_version)
                   if d["version"] != to_version]
        by_version: dict[tuple[int, str], list[str]] = {}
        for d in devices:
            by_version.setdefault((d["version"], d["protocol"]), []).append(d["device_id"])
        groups = []
        for (version, protocol), ids in sorted(by_version.items()):
            old = await self.store.platform.template(product_id, version)
            diff = diff_templates(old, new, protocol)
            groups.append({"from_version": version, "protocol": protocol, "devices": len(ids)}
                          | diff)
        removed = {k for g in groups for k in g["removed"]}
        users = await self.store.versions.automations_using([d["device_id"] for d in devices])
        affected = [{"kind": u["kind"], "id": u["id"], "name": u["name"]} for u in users
                    if any(f'"tag": "{k}"' in str(u["definition"]).replace("'", '"')
                           for k in removed)]
        return {"to_version": to_version, "devices": len(devices), "groups": groups,
                "breaking": any(g["breaking"] for g in groups),
                "automations_affected": affected}

    async def upgrade(self, p: Principal, product_id: UUID, to_version: int, *,
                      from_version: int | None, device_ids: list[str] | None,
                      allow_breaking: bool, client_addr: str | None) -> dict[str, Any]:
        await self._product(p, product_id)
        new = await self._published(product_id, to_version)
        devices = [d for d in await self.store.versions.devices_of(product_id, from_version,
                                                                   device_ids)
                   if d["version"] != to_version]
        moved, failed = [], []
        for d in devices:
            old = await self.store.platform.template(product_id, d["version"])
            diff = diff_templates(old, new, d["protocol"])
            if diff["breaking"] and not allow_breaking:
                failed.append({"device_id": d["device_id"], "error": "breaking change: "
                               + "; ".join(diff["breaking_changes"])})
                continue
            if d["protocol"] not in new.bindings:
                failed.append({"device_id": d["device_id"],
                               "error": f"version {to_version} has no {d['protocol']} binding"})
                continue
            model = None
            record = await self.store.platform.device(None, d["device_id"])
            if d["enabled"]:
                try:
                    model = self.store.platform._build(
                        new, d["device_id"], record.name, d["protocol"], record.connection,
                        record.options, record.identifier, record.serial_number or "",
                        enabled=True)
                except PlatformError as exc:
                    failed.append({"device_id": d["device_id"], "error": str(exc)})
                    continue
            await self.store.versions.move(d["device_id"], to_version,
                                           build_tags(new, d["protocol"]) + calc_rows(new))
            if model is not None:
                await self.svc.hub.apply_platform_device(d["device_id"], model)
            moved.append(d["device_id"])
        await self.store.add_auth_audit(
            "devices_upgraded", p.username, user_id=p.user_id, client_addr=client_addr,
            detail={"product_id": str(product_id), "to_version": to_version,
                    "moved": len(moved), "failed": len(failed),
                    "allow_breaking": allow_breaking})
        return {"moved": moved, "failed": failed}

    # --- firmware -----------------------------------------------------------------------------

    async def add_release(self, p: Principal, product_id: UUID, *, version: str,
                          notes: str | None, image: bytes, sha256: str | None,
                          min_template_version: int | None, client_addr: str | None) -> Any:
        await self._product(p, product_id)
        if not image or len(image) > MAX_IMAGE:
            raise Invalid("the image must be 1 byte to 16 MB", code="invalid_image")
        digest = hashlib.sha256(image).hexdigest()
        if sha256 is not None and sha256.lower() != digest:
            raise Invalid("the image does not match the given SHA-256 (damaged upload?)",
                          code="checksum_mismatch")
        try:
            release = await self.store.versions.add_release(
                product_id=product_id, version=version, notes=notes, sha256=digest,
                image=image, min_template_version=min_template_version, user_id=p.user_id)
        except PlatformError as exc:
            raise Conflict(str(exc), code=exc.code) from None
        await self.store.add_auth_audit(
            "firmware_released", p.username, user_id=p.user_id, client_addr=client_addr,
            detail={"product_id": str(product_id), "version": version, "sha256": digest,
                    "size": len(image)})
        return release

    async def _release(self, p: Principal, release_id: int) -> Any:
        release = await self.store.versions.release(release_id)
        if release is None:
            raise NotFound("firmware release not found")
        await self._product(p, release.product_id)
        return release

    async def withdraw(self, p: Principal, release_id: int, client_addr: str | None) -> None:
        await self._release(p, release_id)
        await self.store.versions.withdraw(release_id)
        await self.store.add_auth_audit("firmware_withdrawn", p.username, user_id=p.user_id,
                                        client_addr=client_addr,
                                        detail={"release_id": release_id})

    async def rollout(self, p: Principal, release_id: int, device_ids: list[str] | None,
                      client_addr: str | None) -> dict[str, Any]:
        release = await self._release(p, release_id)
        if release.status != "released":
            raise Conflict("the release was withdrawn", code="withdrawn")
        devices = await self.store.versions.devices_of(release.product_id, None, device_ids)
        skipped = []
        eligible = []
        for d in devices:
            if release.min_template_version and d["version"] < release.min_template_version:
                skipped.append({"device_id": d["device_id"], "reason":
                                f"template version {d['version']} < "
                                f"{release.min_template_version}"})
            elif d["protocol"] != "native":
                skipped.append({"device_id": d["device_id"],
                                "reason": "only native devices take firmware from the hub"})
            else:
                eligible.append(d["device_id"])
        jobs = {d: uuid.uuid4() for d in eligible}
        made = await self.store.versions.create_jobs(release_id, jobs, p.user_id)
        skipped += [{"device_id": d, "reason": "an update is already running"}
                    for d in eligible if d not in made]
        for device_id in made:
            await self.offer(device_id, jobs[device_id], release)
        await self.store.add_auth_audit(
            "firmware_rollout", p.username, user_id=p.user_id, client_addr=client_addr,
            detail={"release_id": release_id, "jobs": len(made), "skipped": len(skipped)})
        return {"jobs": len(made), "skipped": skipped}

    async def offer(self, device_id: str, job_id: UUID, release: Any) -> None:
        """Tell the device (it may be offline: the HTTP queue keeps it)."""
        message = {"type": "ota", "job_id": str(job_id), "release_id": release.release_id,
                   "version": release.version, "sha256": release.sha256,
                   "size": release.size_bytes,
                   "url": f"/ingest/v1/devices/{device_id}/firmware/{release.release_id}"}
        await self.svc.ingest.send_ota(device_id, message)
