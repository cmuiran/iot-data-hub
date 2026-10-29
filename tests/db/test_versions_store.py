"""0014_versions_firmware: up/down/up, and rollback refused while firmware releases exist."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from hub.storage.migrate import migrate, rollback
from tests.db.test_platform_store import _orgs, _published

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.postgres


def test_up_down_up(database_url: str) -> None:
    rollback(database_url, "0016_home_maker_plus")
    rollback(database_url, "0015_maker_home")
    rollback(database_url, "0014_versions_firmware")
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT to_regclass('firmware_releases')").fetchone()[0] is None
    assert migrate(database_url) == ["0014_versions_firmware",
                                     "0015_maker_home",
                                     "0016_home_maker_plus"]


async def test_rollback_refused_with_releases(store: Any, database_url: str) -> None:
    await _orgs(store)
    product = await _published(store)
    image = b"firmware"
    await store.versions.add_release(product_id=product.product_id, version="1.0.0",
                                     notes=None, sha256=hashlib.sha256(image).hexdigest(),
                                     image=image, min_template_version=None, user_id=None)
    with pytest.raises(psycopg.Error, match="firmware releases exist"):
        rollback(database_url, "0016_home_maker_plus")
        rollback(database_url, "0015_maker_home")
        rollback(database_url, "0014_versions_firmware")
