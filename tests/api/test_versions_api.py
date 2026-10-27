"""Platform phase 7: moving devices to a newer template version (preview, breaking changes,
tag ids and history kept, automations affected) and firmware (upload with checksum, rollout,
download with the device credential, the device's reports, withdrawal)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from typing import Any

import pytest

from backend.settings import Settings
from simulators.native_device import NativeDeviceSim
from tests.api.conftest import Backend, make_settings
from tests.api.test_accounts_api import personal_org
from tests.api.test_ingest_api import native_template, owned_device, running

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def settings() -> Settings:
    return make_settings(signup_enabled=True, public_url="https://hub.test",
                         tcp_ingest_port=0, tcp_ingest_insecure=True,
                         tcp_ingest_host="127.0.0.1", broker_hook_token="h" * 32)


async def product_id(b: Backend, h: dict[str, str]) -> str:
    r = await b.client.get("/api/platform/products", headers=h)
    return next(p["product_id"] for p in r.json() if p["code"] == "NAT-8R")


async def publish(b: Backend, h: dict[str, str], pid: str, template: dict[str, Any]) -> int:
    r = await b.client.put(f"/api/platform/products/{pid}/draft", headers=h,
                           json={"definition": template})
    assert r.status_code == 200, r.text
    version = r.json()["version"]
    r = await b.client.post(f"/api/platform/products/{pid}/versions/{version}/publish",
                            headers=h)
    assert r.status_code == 200, r.text
    return version


async def tag_ids(b: Backend, device_id: str) -> dict[str, int]:
    tags = await b.store.tags()
    return {p.rsplit(".", 1)[1]: r.tag_id for p, r in tags.items()
            if p.startswith(f"platform.devices.{device_id}.")}


async def test_template_upgrade(backend: Backend) -> None:
    b = backend
    h, sara, _, device_id, _ = await owned_device(b)
    pid = await product_id(b, h["alice"])
    org = await personal_org(b, sara)
    r = await b.client.post(f"/api/platform/organizations/{org}/automations", headers=sara,
                            json={"name": "uses relay 8", "trigger": {"type": "time",
                                                                      "at": "09:00"},
                                  "actions": [{"type": "write", "device_id": device_id,
                                               "tag": "relay_8", "value": True}]})
    assert r.status_code == 201, r.text
    before = await tag_ids(b, device_id)

    v2 = native_template()
    v2["tags"] = [t for t in v2["tags"] if t["key"] != "relay_8"]
    v2["tags"][0] = v2["tags"][0] | {"display_name": {"fa": "چراغ"}}
    v2["tags"].append({"key": "humidity", "kind": "analog_input", "display_name": {"en": "RH"},
                       "data_type": "float32", "unit": "%"})
    v2["bindings"]["native"]["tags"] = {t["key"]: {} for t in v2["tags"]}
    version = await publish(b, h["alice"], pid, v2)
    assert version == 2

    r = await b.client.get(f"/api/platform/products/{pid}/versions/2/upgrade-preview",
                           headers=h["alice"])
    assert r.status_code == 200, r.text
    preview = r.json()
    (group,) = preview["groups"]
    assert (group["devices"], group["added"], group["removed"]) == (2, ["humidity"],
                                                                    ["relay_8"])
    assert preview["breaking"] and "relay_8: removed" in group["breaking_changes"]
    assert {"key": "relay_1", "fields": ["display_name"]} in group["changed"]
    assert [a["name"] for a in preview["automations_affected"]] == ["uses relay 8"]
    r = await b.client.get(f"/api/platform/products/{pid}/versions/2/upgrade-preview",
                           headers=h["bob"])
    assert r.status_code == 404                                   # another manufacturer

    url = f"/api/platform/products/{pid}/versions/2/upgrade"
    r = await b.client.post(url, headers=h["alice"], json={})
    assert r.json()["moved"] == [] and len(r.json()["failed"]) == 2
    assert "breaking" in r.json()["failed"][0]["error"]
    r = await b.client.post(url, headers=h["alice"], json={"allow_breaking": True})
    assert len(r.json()["moved"]) == 2, r.text
    after = await tag_ids(b, device_id)
    assert after["relay_1"] == before["relay_1"]                  # same tag, same history
    assert "relay_8" not in after and "humidity" in after
    record = await b.store.platform.device(None, device_id)
    assert record.version == 2
    assert f"platform.devices.{device_id}.humidity" in b.services.hub.tags

    v3 = copy.deepcopy(v2)
    v3["tags"][1] = v3["tags"][1] | {"display_name": {"fa": "پنکه"}}
    await publish(b, h["alice"], pid, v3)
    r = await b.client.post(f"/api/platform/products/{pid}/versions/3/upgrade",
                            headers=h["alice"], json={"device_ids": [device_id]})
    assert r.json()["moved"] == [device_id], r.text             # not breaking: no flag needed
    events = {a.event for a in await b.store.list_auth_audit()}
    assert "devices_upgraded" in events


async def test_firmware(backend: Backend) -> None:
    b = backend
    h, sara, _, device_id, secret = await owned_device(b)
    pid = await product_id(b, h["alice"])
    image = b"\x7fFIRMWARE" * 1000
    base = f"/api/platform/products/{pid}/firmware"
    r = await b.client.post(base, headers=h["alice"], content=image,
                            params={"version": "1.1.0", "sha256": "0" * 64})
    assert r.status_code == 422 and r.json()["error"] == "checksum_mismatch"
    r = await b.client.post(base, headers=h["alice"], content=image, params={
        "version": "1.1.0", "sha256": hashlib.sha256(image).hexdigest(), "notes": "fix"})
    assert r.status_code == 201, r.text
    release = r.json()
    assert release["size_bytes"] == len(image)
    r = await b.client.post(base, headers=h["alice"], content=image,
                            params={"version": "1.1.0"})
    assert r.status_code == 409
    r = await b.client.post(base, headers=h["bob"], content=image, params={"version": "9"})
    assert r.status_code == 404

    other = b.second["device_id"]                                 # type: ignore[attr-defined]
    auth = {"Authorization": f"Bearer {secret}"}
    r = await b.client.get(f"/ingest/v1/devices/{device_id}/firmware/{release['release_id']}",
                           headers=auth)
    assert r.status_code == 404                                   # no job yet
    r = await b.client.post(f"/api/platform/firmware/{release['release_id']}/rollout",
                            headers=h["alice"], json={})
    assert r.status_code == 200 and r.json()["jobs"] == 2, r.text
    r = await b.client.post(f"/api/platform/firmware/{release['release_id']}/rollout",
                            headers=h["alice"], json={})
    assert r.json()["jobs"] == 0                                  # one open job per device

    sim = NativeDeviceSim(device_id, secret, {"relay_1": False})
    stop = asyncio.Event()
    async with running(sim.run_http(b.client, 0.05, stop)):
        async def done() -> bool:
            jobs = await b.store.versions.jobs(device_id=device_id)
            return bool(jobs) and jobs[0].status == "installed"

        for _ in range(100):
            if await done():
                break
            await asyncio.sleep(0.1)
        stop.set()
    assert sim.firmware == "1.1.0"
    (job,) = await b.store.versions.jobs(device_id=device_id)
    assert (job.status, job.reported_version) == ("installed", "1.1.0")
    assert (await b.store.platform.device(None, device_id)).firmware_version == "1.1.0"
    r = await b.client.get(f"/ingest/v1/devices/{other}/firmware/{release['release_id']}",
                           headers=auth)
    assert r.status_code == 401                                   # someone else's credential

    r = await b.client.post(f"/api/platform/firmware/{release['release_id']}/withdraw",
                            headers=h["alice"])
    assert r.status_code == 204
    jobs = {j.device_id: j.status for j in await b.store.versions.jobs(
        release_id=release["release_id"])}
    assert jobs == {device_id: "installed", other: "cancelled"}
    r = await b.client.post(f"/api/platform/firmware/{release['release_id']}/rollout",
                            headers=h["alice"], json={})
    assert r.status_code == 409

    # a device reporting on someone else's job changes nothing
    r = await b.client.post(f"/ingest/v1/devices/{device_id}/ota", headers=auth,
                            json={"job_id": str(job.job_id), "status": "failed"})
    assert r.json()["status"] == "ignored"
    # the version a device reports in its state message is kept
    await b.client.post(f"/ingest/v1/devices/{device_id}/state", headers=auth,
                        json={"state": "online", "firmware": "1.2.0-beta"})
    assert (await b.store.platform.device(None, device_id)).firmware_version == "1.2.0-beta"
    sara_view = await b.client.get(f"/api/platform/my/devices/{device_id}/view", headers=sara)
    assert sara_view.json()["device"]["firmware_version"] == "1.2.0-beta"
