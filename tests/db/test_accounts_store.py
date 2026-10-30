"""0010_accounts_sharing: up/down/up, rollback refusal, and row-level security of invitations and
device shares in SQL as hub_tenant (a forgotten condition in the code still cannot leak)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.accounts import new_token
from hub.storage.migrate import migrate, rollback
from hub.storage.platform import PlatformError
from tests.db.test_platform_store import _orgs, _published, _register, _sql

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.postgres
SOON = datetime.now(UTC) + timedelta(days=1)


def test_up_down_up(database_url: str) -> None:
    rollback(database_url, "0017_switches_sms")
    rollback(database_url, "0016_home_maker_plus")
    rollback(database_url, "0015_maker_home")
    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    rollback(database_url, "0012_commands_ingest")
    rollback(database_url, "0011_claims")
    rollback(database_url, "0010_accounts_sharing")
    with psycopg.connect(database_url) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='users'")}
    assert not {"invitations", "account_tokens"} & tables and "email" not in cols
    assert migrate(database_url) == ["0010_accounts_sharing", "0011_claims",
                                     "0012_commands_ingest",
                                     "0013_automations",
                                     "0014_versions_firmware",
                                     "0015_maker_home",
                                     "0016_home_maker_plus",
                                     "0017_switches_sms"]


async def test_rollback_refused_with_accounts(store: Any, database_url: str) -> None:
    await store.accounts.create_user(username="user_x", display_name="X", password_hash="h",
                                     email="x@example.com", language="fa", verified_at=None)
    rollback(database_url, "0017_switches_sms")
    rollback(database_url, "0016_home_maker_plus")
    rollback(database_url, "0015_maker_home")
    rollback(database_url, "0014_versions_firmware")
    rollback(database_url, "0013_automations")
    rollback(database_url, "0012_commands_ingest")
    rollback(database_url, "0011_claims")
    with pytest.raises(psycopg.Error, match="e-mail address exist"):
        rollback(database_url, "0010_accounts_sharing")
    with pytest.raises(PlatformError, match="has an account"):        # unique, any case
        await store.accounts.create_user(username="user_y", display_name="Y", password_hash="h",
                                         email="X@EXAMPLE.com", language="fa", verified_at=None)


async def test_tokens_are_single_use(store: Any) -> None:
    user = await store.accounts.create_user(username="user_t", display_name="T", password_hash="h",
                                            email="t@example.com", language="en",
                                            verified_at=None)
    now = datetime.now(UTC)
    _, first = new_token()
    _, second = new_token()
    await store.accounts.add_token(user.user_id, "verify_email", first, SOON)
    await store.accounts.add_token(user.user_id, "verify_email", second, SOON)
    assert await store.accounts.use_token(first, "verify_email", now) is None   # replaced
    assert await store.accounts.use_token(second, "reset_password", now) is None  # purpose
    assert await store.accounts.use_token(second, "verify_email", now) == user.user_id
    assert await store.accounts.use_token(second, "verify_email", now) is None   # once
    _, old = new_token()
    await store.accounts.add_token(user.user_id, "reset_password", old, now - timedelta(1))
    assert await store.accounts.use_token(old, "reset_password", now) is None    # expired


async def test_invitations_and_shares_isolated(store: Any, database_url: str) -> None:
    await _orgs(store)                                       # maker_a, maker_b, home_c
    await store.platform.create_organization("home_d", "Home D", "customer")
    device = (await _register(store, await _published(store), 1, 1))[0].device_id
    await store.accounts.set_owner(device, "home_c", None)
    _, hashed = new_token()
    inv = await store.accounts.create_invitation(
        {"home_c"}, kind="device", org_id="home_c", email="d@example.com", token_hash=hashed,
        expires_at=SOON, invited_by=None, device_id=device, level="viewer")
    accepted = await store.accounts.accept_invitation(inv, (await store.accounts.create_user(
        username="user_d", display_name="D", password_hash="h", email="d@example.com",
        language="fa", verified_at=SOON)).user_id, "home_d", datetime.now(UTC))
    assert accepted

    # another org cannot invite in home_c's name, nor share a device it does not own
    for orgs, org, dev in (({"home_d"}, "home_c", None), ({"home_d"}, "home_d", device),
                           ({"maker_a"}, "maker_a", device)):
        with pytest.raises(PlatformError, match="not found"):
            await store.accounts.create_invitation(
                orgs, kind="device" if dev else "organization", org_id=org,
                email="e@example.com", token_hash=new_token()[1], expires_at=SOON,
                invited_by=None, device_id=dev, level="viewer" if dev else None,
                role_id=None if dev else "customer_member")
    assert _sql(database_url, {"home_d"}, "SELECT count(*) FROM invitations")[0][0] == 0
    assert _sql(database_url, {"home_c"}, "SELECT count(*) FROM invitations")[0][0] == 1
    assert _sql(database_url, {"maker_b"},
                "SELECT count(*) FROM device_access")[0][0] == 0     # makers: none

    share = (await store.accounts.shares({"home_c"}, device))[0]
    assert share.org_id == "home_d" and share.org_name == "Home D"
    assert await store.accounts.revoke_share({"maker_a"}, device, share.access_id) is None
    # the owner row cannot be revoked by anyone through the tenant role
    owner_id = _sql(database_url, {"home_c"}, "SELECT access_id FROM device_access WHERE "
                    "level='owner'")[0][0]
    assert await store.accounts.revoke_share({"home_c"}, device, owner_id) is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege):    # only revoked_at is writable
        _sql(database_url, {"home_c"}, "UPDATE device_access SET level='owner' RETURNING 1")
    # the receiving organization may give the share up itself
    assert (await store.accounts.revoke_share({"home_d"}, device, share.access_id)) is not None
    assert await store.accounts.shares({"home_c"}, device) == []
