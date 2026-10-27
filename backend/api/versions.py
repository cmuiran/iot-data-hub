"""/api/platform: moving devices to another template version, firmware releases and rollouts
(product.manage of the product's manufacturer); /ingest/v1: the device fetches an image and
reports on its job (with its credential)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from backend.api.ingest import _device, _invalid
from backend.deps import ServicesDep, client_addr, require
from backend.errors import Invalid, NotFound
from backend.rbac import Principal
from hub.storage.versions import JobRecord, ReleaseRecord

router = APIRouter(tags=["versions"])
Manager = Annotated[Principal, Depends(require("product.manage"))]


@router.get("/api/platform/products/{product_id}/versions/{version}/upgrade-preview")
async def upgrade_preview(product_id: UUID, version: int, svc: ServicesDep, p: Manager,
                          from_version: Annotated[int | None, Query(alias="from", ge=1)] = None
                          ) -> dict[str, Any]:
    """What moving the devices (of one version, or all) to ``version`` changes and breaks."""
    return await svc.versions.preview(p, product_id, version, from_version)


class UpgradeIn(BaseModel):
    from_version: int | None = Field(default=None, ge=1)
    device_ids: list[str] | None = Field(default=None, min_length=1, max_length=10_000)
    allow_breaking: bool = False


@router.post("/api/platform/products/{product_id}/versions/{version}/upgrade")
async def upgrade(product_id: UUID, version: int, body: UpgradeIn, svc: ServicesDep,
                  p: Manager, request: Request) -> dict[str, Any]:
    return await svc.versions.upgrade(
        p, product_id, version, from_version=body.from_version, device_ids=body.device_ids,
        allow_breaking=body.allow_breaking, client_addr=client_addr(request))


@router.post("/api/platform/products/{product_id}/firmware", status_code=201)
async def add_firmware(product_id: UUID, request: Request, svc: ServicesDep, p: Manager,
                       version: Annotated[str, Query(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,39}$")],
                       notes: Annotated[str | None, Query(max_length=2000)] = None,
                       sha256: Annotated[str | None, Query(pattern=r"^[0-9a-fA-F]{64}$")] = None,
                       min_template_version: Annotated[int | None, Query(ge=1)] = None,
                       ) -> ReleaseRecord:
    """Body: the image itself (application/octet-stream, at most 16 MB)."""
    length = int(request.headers.get("content-length") or 0)
    if length > 16 * 1024 * 1024:
        raise Invalid("the image must be at most 16 MB", code="invalid_image")
    image = await request.body()
    return await svc.versions.add_release(p, product_id, version=version, notes=notes,
                                          image=image, sha256=sha256,
                                          min_template_version=min_template_version,
                                          client_addr=client_addr(request))


@router.get("/api/platform/products/{product_id}/firmware")
async def list_firmware(product_id: UUID, svc: ServicesDep, p: Manager) -> list[ReleaseRecord]:
    await svc.versions._product(p, product_id)
    return await svc.store.versions.releases(product_id)


@router.post("/api/platform/firmware/{release_id}/withdraw", status_code=204)
async def withdraw(release_id: int, svc: ServicesDep, p: Manager, request: Request) -> None:
    await svc.versions.withdraw(p, release_id, client_addr(request))


class RolloutIn(BaseModel):
    device_ids: list[str] | None = Field(default=None, min_length=1, max_length=10_000)


@router.post("/api/platform/firmware/{release_id}/rollout")
async def rollout(release_id: int, body: RolloutIn, svc: ServicesDep, p: Manager,
                  request: Request) -> dict[str, Any]:
    return await svc.versions.rollout(p, release_id, body.device_ids, client_addr(request))


@router.get("/api/platform/firmware/{release_id}/jobs")
async def jobs(release_id: int, svc: ServicesDep, p: Manager) -> list[JobRecord]:
    await svc.versions._release(p, release_id)
    return await svc.store.versions.jobs(release_id=release_id)


# --- the device --------------------------------------------------------------------------------


@router.get("/ingest/v1/devices/{device_id}/firmware/{release_id}")
async def download(device_id: str, release_id: int, svc: ServicesDep, request: Request,
                   authorization: Annotated[str | None, Header()] = None) -> Response:
    """The image of a release the device has an open job for."""
    await _device(svc, device_id, authorization, request)
    if not any(j.release_id == release_id for j in await svc.store.versions.jobs(
            device_id=device_id, open_only=True)):
        raise NotFound("no firmware job for this release")
    release = await svc.store.versions.release(release_id)
    image = await svc.store.versions.image(release_id)
    if release is None or image is None:
        raise NotFound("firmware release not found")
    return Response(image, media_type="application/octet-stream", headers={
        "X-Firmware-Version": release.version, "X-Firmware-SHA256": release.sha256})


@router.post("/ingest/v1/devices/{device_id}/ota")
async def ota(device_id: str, payload: dict[str, Any], svc: ServicesDep, request: Request,
              authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """``{"job_id": ..., "status": "downloading" | "installed" | "failed", "version": ...}``"""
    driver = await _device(svc, device_id, authorization, request)
    try:
        return {"status": await svc.ingest.ota(driver, payload)}
    except ValueError as exc:
        raise _invalid(exc) from None
