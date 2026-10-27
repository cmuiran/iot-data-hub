"""0011_claims: up/down/up, rollback refusal, codes and credentials invisible to hub_tenant,
catalog products readable by every tenant, ownership moving with an edge hub's children."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from hub.platform.devices import ProductIn
from hub.platform.template import ProductTemplate
from hub.storage.migrate import migrate, rollback
from tests.db.test_platform_store import _orgs, _published, _register, _sql
from tests.platform.test_template import relay_template

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.postgres


def test_up_down_up(database_url: str) -> None:
    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    rollback(database_url, "0012_commands_ingest")
    rollback(database_url, "0011_claims")
    with psycopg.connect(database_url) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert not {"device_claim_codes", "device_credentials"} & tables
    assert migrate(database_url) == ["0011_claims",
                                     "0012_commands_ingest",
                                     "0013_automations",
                                     "0014_versions_firmware"]


async def test_codes_are_service_only(store: Any, database_url: str) -> None:
    await _orgs(store)
    (device,) = await _register(store, await _published(store), 1, 1)
    await store.claims.issue_codes({device.device_id: "hmac-sha256$x"}, "activation", None)
    await store.claims.issue_credentials({device.device_id: "a" * 64}, None)
    for table in ("device_claim_codes", "device_credentials"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _sql(database_url, {"maker_a"}, f"SELECT 1 FROM {table}")  # noqa: S608
    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    rollback(database_url, "0012_commands_ingest")
    with pytest.raises(psycopg.Error, match="claim codes or device credentials exist"):
        rollback(database_url, "0011_claims")
    assert await store.claims.credential_valid(device.device_id, "a" * 64)
    await store.claims.issue_credentials({device.device_id: "b" * 64}, None)   # rotation
    assert not await store.claims.credential_valid(device.device_id, "a" * 64)


async def test_catalog_visible_to_all_tenants(store: Any, database_url: str) -> None:
    await _orgs(store)
    p = store.platform
    product = await p.create_product({"maker_b"}, "maker_b", ProductIn(
        code="CAT-1", name="Meter", identifier_pattern="CAT-{00}", catalog=True), None)
    await p.save_draft({"maker_b"}, product.product_id,
                       ProductTemplate.model_validate(relay_template()), None, None)
    q = "SELECT count(*) FROM product_versions"
    assert _sql(database_url, {"home_c"}, q)[0][0] == 0          # a draft is not shown
    await p.publish({"maker_b"}, product.product_id, 1, None)
    assert _sql(database_url, {"home_c"}, "SELECT code FROM products") == [("CAT-1",)]
    assert _sql(database_url, {"home_c"}, q)[0][0] == 1
    assert _sql(database_url, {"maker_a"}, "SELECT count(*) FROM products")[0][0] == 1


async def test_ownership_moves_with_children(store: Any) -> None:
    await _orgs(store)
    product = await _published(store)
    hub, child = await _register(store, product, 1, 2)
    async with store._conn() as conn:
        await conn.execute("UPDATE platform_devices SET parent_device_id=%s WHERE device_id=%s",
                           (hub.device_id, child.device_id))
    await store.claims.issue_codes({hub.device_id: "hmac-sha256$x"}, "activation", None)
    assert await store.claims.complete_claim(hub.device_id, "home_c", None, datetime.now(UTC))
    assert not await store.claims.complete_claim(hub.device_id, "maker_b", None,
                                                 datetime.now(UTC))        # used
    levels = await store.accounts.access_levels({"home_c"}, [hub.device_id, child.device_id])
    assert levels == {hub.device_id: "owner", child.device_id: "owner"}
    ids = await store.claims.release(hub.device_id, "sold", datetime.now(UTC))
    assert set(ids) == {hub.device_id, child.device_id}
    assert await store.accounts.access_levels({"home_c"}, ids) == {}
