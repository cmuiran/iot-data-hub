"""Platform store (0009): products, versions, 1000 devices, uniqueness, tenant isolation.

Isolation is checked twice: through the store methods (another organization's rows answer like
unknown ones) and directly in SQL as the role hub_tenant (row-level security, no privilege on
the other tables), so a forgotten WHERE in the store would still not leak.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from hub.config_loader import load_config
from hub.platform.devices import ProductIn, identifiers, is_platform_device_id
from hub.platform.template import ProductTemplate
from hub.storage.migrate import RollbackError, rollback
from hub.storage.platform import NotVisible, PlatformError
from tests.platform.test_template import relay_template

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.postgres
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
A, B, C = {"maker_a"}, {"maker_b"}, {"home_c"}
TAGS = 9                                    # relay_template(): 8 relays + power


async def _orgs(store: Any) -> None:
    await store.platform.create_organization("maker_a", "Maker A", "manufacturer")
    await store.platform.create_organization("maker_b", "Maker B", "manufacturer")
    await store.platform.create_organization("home_c", "Home C", "customer")


async def _published(store: Any, orgs: set[str] = A, code: str = "CTRL-8R") -> Any:
    org = next(iter(orgs))
    product = await store.platform.create_product(
        orgs, org, ProductIn(code=code, name="Smart 8-relay",
                             identifier_pattern=f"{code}-{{0000}}"), None)
    template = ProductTemplate.model_validate(relay_template())
    draft = await store.platform.save_draft(orgs, product.product_id, template, None, None)
    assert (draft.version, draft.status) == (1, "draft")
    await store.platform.publish(orgs, product.product_id, 1, None)
    return product


async def _register(store: Any, product: Any, start: int, count: int,
                    orgs: set[str] = A) -> list[Any]:
    entries = [(i, None) for i in identifiers(product.identifier_pattern, start, count)]
    return await store.platform.register_devices(
        orgs, product.product_id, 1, protocol="modbus_tcp", entries=entries, batch="B1",
        warranty_months=18, connection={}, options={}, enabled=False, user_id=None)


def _sql(url: str, orgs: set[str], query: str) -> Any:
    """One query as hub_tenant for ``orgs`` (what the store does for tenant calls)."""
    with psycopg.connect(url) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE hub_tenant")
        conn.execute("SELECT set_config('app.org_ids', %s, true)", (",".join(sorted(orgs)),))
        return conn.execute(query).fetchall()


async def test_product_versions_and_immutability(store: Any, database_url: str) -> None:
    await _orgs(store)
    product = await _published(store)
    assert product.org_id == "maker_a"
    versions = await store.platform.versions(A, product.product_id)
    assert [(v.version, v.status) for v in versions] == [(1, "published")]
    with pytest.raises(PlatformError, match="not a draft"):
        await store.platform.publish(A, product.product_id, 1, None)

    with psycopg.connect(database_url) as conn:     # even the owner cannot change it in SQL
        for sql in ("UPDATE product_versions SET definition = '{}'::jsonb",
                    "UPDATE product_versions SET status = 'draft', published_at = NULL",
                    "DELETE FROM product_versions"):
            with pytest.raises(psycopg.Error, match="published"):
                conn.execute(sql)
            conn.rollback()

    # a change is a new version; devices of version 1 keep version 1
    devices = await _register(store, product, 1, 2)
    changed = relay_template()
    changed["tags"][0] = changed["tags"][0] | {"display_name": {"fa": "چراغ ورودی"}}
    v2 = await store.platform.save_draft(A, product.product_id,
                                         ProductTemplate.model_validate(changed), "rename", None)
    assert (v2.version, v2.status) == (2, "draft")
    again = await store.platform.save_draft(A, product.product_id,
                                            ProductTemplate.model_validate(changed), "x", None)
    assert again.version == 2                        # one draft at a time, updated in place
    await store.platform.publish(A, product.product_id, 2, None)
    after = await store.platform.devices_by_id(A, [d.device_id for d in devices])
    assert {d.version for d in after} == {1}
    v1 = await store.platform.version(A, product.product_id, 1)
    assert v1.definition["tags"][0]["display_name"] == {"fa": "رله 1"}


async def test_thousand_devices(store: Any) -> None:
    await _orgs(store)
    product = await _published(store)
    t0 = time.perf_counter()
    devices = await _register(store, product, 1, 1000)
    elapsed = time.perf_counter() - t0
    assert len(devices) == 1000
    assert elapsed < 30, elapsed
    ids = [d.device_id for d in devices]
    assert len(set(ids)) == 1000 and all(is_platform_device_id(i) for i in ids)
    assert {d.identifier for d in devices} == set(identifiers("CTRL-8R-{0000}", 1, 1000))
    assert {(d.product_id, d.version, d.lifecycle, d.enabled) for d in devices} == {
        (product.product_id, 1, "registered", False)}

    tags = await store.tags()                       # every device has its own tags and ids
    paths = [p for p in tags if p.startswith("platform.devices.")]
    assert len(paths) == 1000 * TAGS
    assert len({tags[p].tag_id for p in paths}) == 1000 * TAGS
    assert f"platform.devices.{ids[0]}.relay_1" in tags

    page, total = await store.platform.devices(A, product_id=product.product_id, limit=50)
    assert total == 1000 and len(page) == 50
    found, total = await store.platform.devices(A, search="ctrl-8r-0042")
    assert total == 1 and found[0].identifier == "CTRL-8R-0042"
    rec = await store.platform.product(A, product.product_id)
    assert rec.devices == 1000 and rec.latest_published == 1


async def test_identifiers_are_unique(store: Any, database_url: str) -> None:
    await _orgs(store)
    product = await _published(store)
    await _register(store, product, 1, 10)
    with pytest.raises(PlatformError, match="already registered: CTRL-8R-0009, CTRL-8R-0010"):
        await _register(store, product, 9, 5)
    with pytest.raises(PlatformError, match="repeated identifiers"):
        await store.platform.register_devices(
            A, product.product_id, 1, protocol="modbus_tcp",
            entries=[("X-001", None), ("X-001", None)], batch=None, warranty_months=None,
            connection={}, options={}, enabled=False, user_id=None)
    # another manufacturer cannot reuse an identifier either (buyers type it)
    other = await _published(store, B, "OTHER-1")
    with pytest.raises(PlatformError, match="already registered"):
        await store.platform.register_devices(
            B, other.product_id, 1, protocol="mqtt", entries=[("ctrl-8r-0001".upper(), None)],
            batch=None, warranty_months=None, connection={}, options={}, enabled=False,
            user_id=None)
    page, total = await store.platform.devices(A)
    assert total == 10                                 # nothing half-registered

    with psycopg.connect(database_url) as conn:      # enforced by the database itself
        row = conn.execute("SELECT * FROM platform_devices LIMIT 1").fetchone()
        assert row is not None
        insert = ("INSERT INTO platform_devices (device_id, product_id, version, "
                  "maker_org_id, identifier) SELECT 'dzzzzzzzzzzzzzzzzzzzz', product_id, "
                  "version, maker_org_id, {} FROM platform_devices LIMIT 1")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert.format("identifier"))
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):     # only upper case is stored
            conn.execute(insert.format("lower(identifier)"))


async def test_register_needs_a_published_version_and_binding(store: Any) -> None:
    await _orgs(store)
    product = await store.platform.create_product(
        A, "maker_a", ProductIn(code="P1", name="P", identifier_pattern="P1-{000}"), None)
    await store.platform.save_draft(A, product.product_id,
                                    ProductTemplate.model_validate(relay_template()), None, None)
    with pytest.raises(PlatformError, match="published version"):
        await _register(store, product, 1, 1)
    await store.platform.publish(A, product.product_id, 1, None)
    with pytest.raises(PlatformError, match="no opcua binding"):
        await store.platform.register_devices(
            A, product.product_id, 1, protocol="opcua", entries=[("P1-001", None)],
            batch=None, warranty_months=None, connection={}, options={}, enabled=False,
            user_id=None)
    with pytest.raises(PlatformError, match="device configuration.*host"):
        await store.platform.register_devices(        # enabled needs a complete connection
            A, product.product_id, 1, protocol="modbus_tcp", entries=[("P1-001", None)],
            batch=None, warranty_months=None, connection={}, options={}, enabled=True,
            user_id=None)


async def test_tenant_isolation_store(store: Any) -> None:
    await _orgs(store)
    product = await _published(store)
    devices = await _register(store, product, 1, 5)
    pid, did = product.product_id, devices[0].device_id

    assert await store.platform.products(B) == []
    page, total = await store.platform.devices(B)
    assert (page, total) == ([], 0)
    assert await store.platform.devices_by_id(B, [did]) == []
    for call in (store.platform.product(B, pid), store.platform.versions(B, pid),
                 store.platform.version(B, pid, 1), store.platform.device(B, did),
                 store.platform.publish(B, pid, 1, None),
                 store.platform.save_draft(B, pid, ProductTemplate.model_validate(
                     relay_template()), None, None),
                 store.platform.update_device(B, did, name="stolen"),
                 _register(store, product, 100, 1, orgs=B)):
        with pytest.raises(NotVisible):
            await call
    with pytest.raises(NotVisible):                 # a product in someone else's organization
        await store.platform.create_product(
            B, "maker_a", ProductIn(code="EVIL", name="x", identifier_pattern="E-{00}"), None)
    assert [o.org_id for o in await store.platform.organizations(B)] == ["maker_b"]

    # nothing changed for A; the maker may change its own device
    rec, model = await store.platform.update_device(A, did, name="Bench unit",
                                                    lifecycle="ready")
    assert (rec.name, rec.lifecycle, model) == ("Bench unit", "ready", None)
    with pytest.raises(PlatformError, match="cannot be set here"):
        await store.platform.update_device(A, did, lifecycle="active")


async def test_buyer_sees_only_its_device(store: Any, database_url: str) -> None:
    await _orgs(store)
    product = await _published(store)
    devices = await _register(store, product, 1, 3)
    mine = devices[1].device_id
    with psycopg.connect(database_url) as conn:      # what a claim (phase 3) will write
        conn.execute("INSERT INTO device_access (device_id, org_id, level) VALUES "
                     "(%s, 'home_c', 'owner')", (mine,))

    page, total = await store.platform.devices(C)
    assert total == 1 and page[0].device_id == mine
    assert [p.product_id for p in await store.platform.products(C)] == [product.product_id]
    assert (await store.platform.version(C, product.product_id, 1)).status == "published"
    with pytest.raises(NotVisible):                  # the buyer cannot change the device record
        await store.platform.update_device(C, mine, name="x")
    with pytest.raises(NotVisible):                  # ... nor the product
        await store.platform.save_draft(C, product.product_id, ProductTemplate.model_validate(
            relay_template()), None, None)
    # privacy: the maker sees its device but not who owns it
    assert _sql(database_url, A, "SELECT count(*) FROM device_access")[0][0] == 0
    assert _sql(database_url, C, "SELECT count(*) FROM device_access")[0][0] == 1


async def test_row_level_security_in_sql(store: Any, database_url: str) -> None:
    await _orgs(store)
    await _register(store, await _published(store), 1, 4)
    await _register(store, await _published(store, B, "OTHER-1"), 1, 2, orgs=B)
    count = "SELECT count(*) FROM platform_devices"
    assert _sql(database_url, A, count)[0][0] == 4
    assert _sql(database_url, B, count)[0][0] == 2
    assert _sql(database_url, A | B, count)[0][0] == 6
    assert _sql(database_url, set(), count)[0][0] == 0           # no organization, no rows
    assert _sql(database_url, B, "SELECT count(*) FROM products")[0][0] == 1
    assert _sql(database_url, B, "SELECT count(*) FROM product_versions")[0][0] == 1
    for table in ("users", "samples", "tags", "devices", "auth_audit", "user_roles"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _sql(database_url, A, f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
    with pytest.raises(psycopg.errors.InsufficientPrivilege):      # RLS on insert
        _sql(database_url, B, "INSERT INTO products (org_id, code, name, identifier_pattern) "
                              "VALUES ('maker_a', 'X1', 'x', 'X1-{00}') RETURNING 1")
    with psycopg.connect(database_url) as conn:      # the application role is not restricted
        assert conn.execute(count).fetchone()[0] == 6


async def test_yaml_sync_leaves_platform_alone(store: Any) -> None:
    await _orgs(store)
    devices = await _register(store, await _published(store), 1, 3)
    report = load_config(REPO_CONFIG, env={})
    await store.sync_config([d.device for d in report.devices])
    await store.sync_building(report.building)
    await store.sync_config([])                      # even an empty YAML configuration
    left = await store.platform.devices_by_id(A, [d.device_id for d in devices])
    assert len(left) == 3
    tags = await store.tags()
    assert sum(p.startswith("platform.") for p in tags) == 3 * TAGS
    orgs = {o.org_id for o in await store.platform.organizations(None)}
    assert {"maker_a", "maker_b", "home_c"} <= orgs


async def test_rollback_refused_with_platform_data(store: Any, database_url: str) -> None:
    await _orgs(store)
    rollback(database_url, "0016_home_maker_plus")
    rollback(database_url, "0015_maker_home")
    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    rollback(database_url, "0012_commands_ingest")
    rollback(database_url, "0011_claims")                    # no claims or accounts yet
    rollback(database_url, "0010_accounts_sharing")
    with pytest.raises(psycopg.Error, match="platform organizations exist"):
        rollback(database_url, "0009_platform_core")
    assert len(await store.platform.organizations(None)) == 3      # nothing changed
    with pytest.raises(RollbackError, match="roll back 0009"):
        rollback(database_url, "0008_building_model")
