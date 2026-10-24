"""Sign-in with Active Directory / LDAP, against ldap3's offline mock directory."""

from __future__ import annotations

from typing import Any

import pytest
from ldap3 import MOCK_SYNC, MODIFY_REPLACE, Connection, Server
from ldap3.core.exceptions import LDAPSocketOpenError
from pydantic import ValidationError

from backend.directory import Directory, DirectoryRejected, normalize_dn
from hub.models import LdapFile
from tests.api.conftest import PASSWORD, Backend

pytestmark = [pytest.mark.postgres, pytest.mark.integration]

BASE = "DC=kish,DC=local"
SVC = f"CN=svc-hub,OU=Service,{BASE}"
OPERATORS = f"CN=Hub-Operators,OU=Groups,{BASE}"
ENGINEERS = f"CN=Hub-Engineers,OU=Groups,{BASE}"
ALI = f"CN=Ali Rezaei,OU=Staff,{BASE}"
AD_PASSWORD = "Ad-Pass-2026!"


def config(**overrides: Any) -> LdapFile:
    return LdapFile.model_validate({
        "url": "ldaps://dc1.kish.local", "bind_dn": SVC,
        "bind_password_env": "HUB_LDAP_BIND_PASSWORD", "user_base": f"OU=Staff,{BASE}",
        "nested_groups": False,
        "mappings": [{"group": OPERATORS, "role": "operator", "site": "kish", "area": "gate"},
                     {"group": ENGINEERS, "role": "engineer"}],
    } | overrides)


class MockDirectory:
    """One shared mock DIT; ``down`` makes every connection fail like an unreachable DC."""

    def __init__(self) -> None:
        self.server = Server("dc1.kish.local")
        self.down = False
        self.binds: list[str] = []
        admin = self.connect(SVC, "svc-secret")
        add = admin.strategy.add_entry
        add(SVC, {"objectClass": "user", "sAMAccountName": "svc-hub",
                  "userPassword": "svc-secret"})
        add(ALI, {"objectClass": "user", "sAMAccountName": "ali.rezaei",
                  "displayName": "Ali Rezaei", "userPassword": AD_PASSWORD,
                  "memberOf": [OPERATORS]})
        add(f"CN=No Role,OU=Staff,{BASE}", {
            "objectClass": "user", "sAMAccountName": "no.role", "displayName": "No Role",
            "userPassword": AD_PASSWORD, "memberOf": [f"CN=Canteen,OU=Groups,{BASE}"]})
        add(f"CN=Twin A,OU=Staff,{BASE}", {"objectClass": "user", "sAMAccountName": "twin",
                                            "userPassword": "x" * 12})
        add(f"CN=Twin B,OU=Staff,{BASE}", {"objectClass": "user", "sAMAccountName": "twin",
                                            "userPassword": "x" * 12})
        self.admin = admin

    def connect(self, user: str, password: str) -> Any:
        if self.down:
            raise LDAPSocketOpenError("socket connection error while opening: [WinError 10061]")
        self.binds.append(user)
        return Connection(self.server, user=user, password=password, client_strategy=MOCK_SYNC)

    def set_groups(self, dn: str, groups: list[str]) -> None:
        self.admin.bind()
        self.admin.modify(dn, {"memberOf": [(MODIFY_REPLACE, groups)]})


@pytest.fixture
def ad() -> MockDirectory:
    return MockDirectory()


@pytest.fixture
def directory(ad: MockDirectory) -> Directory:
    return Directory(config(), env={"HUB_LDAP_BIND_PASSWORD": "svc-secret"},
                     connection_factory=ad.connect)


async def login(b: Backend, username: str, password: str) -> Any:
    return await b.client.post("/api/auth/login",
                               json={"username": username, "password": password})


# --- the directory module ----------------------------------------------------------------------


def test_config_refuses_clear_text_and_bad_mappings() -> None:
    with pytest.raises(ValidationError, match="clear text"):
        config(url="ldap://dc1.kish.local")
    assert config(url="ldap://dc1.kish.local", start_tls=True).start_tls
    with pytest.raises(ValidationError, match="distinguished name"):
        config(mappings=[{"group": "Operators", "role": "operator"}])
    with pytest.raises(ValidationError, match="area needs a site"):
        config(mappings=[{"group": OPERATORS, "role": "operator", "area": "gate"}])
    with pytest.raises(ValidationError):
        config(mappings=[{"group": OPERATORS, "role": "superuser"}])
    expected = "cn=hub-operators,ou=groups,dc=kish"
    assert normalize_dn("CN=Hub-Operators, OU=Groups ,DC=kish") == expected


def test_authenticate(directory: Directory, ad: MockDirectory) -> None:
    user = directory.authenticate_sync("ali.rezaei", AD_PASSWORD)
    assert user.dn == ALI and user.display_name == "Ali Rezaei"
    assert directory.roles_for(user.groups) == [("operator", "kish", "gate")]
    assert ad.binds[-1] == ALI                           # the password was checked as the user
    with pytest.raises(DirectoryRejected, match="wrong password"):
        directory.authenticate_sync("ali.rezaei", "wrong")
    with pytest.raises(DirectoryRejected, match="unknown"):
        directory.authenticate_sync("nobody", AD_PASSWORD)
    with pytest.raises(DirectoryRejected, match="more than one"):
        directory.authenticate_sync("twin", "x" * 12)


def test_empty_password_never_reaches_the_directory(directory: Directory,
                                                    ad: MockDirectory) -> None:
    before = len(ad.binds)
    with pytest.raises(DirectoryRejected, match="empty"):
        directory.authenticate_sync("ali.rezaei", "")
    assert len(ad.binds) == before                       # no anonymous bind was attempted


@pytest.mark.parametrize("username", ["*", "ali*", "ali.rezaei)(objectClass=*",
                                      "*)(sAMAccountName=ali.rezaei", "\\2a"])
def test_filter_injection_finds_nobody(directory: Directory, username: str) -> None:
    with pytest.raises(DirectoryRejected, match="unknown"):
        directory.authenticate_sync(username, AD_PASSWORD)


# --- sign-in through the API -------------------------------------------------------------------


@pytest.fixture
def with_directory(backend: Backend, directory: Directory) -> Backend:
    backend.services.auth.directory = directory
    return backend


async def test_first_sign_in_creates_the_user_with_mapped_roles(with_directory: Backend,
                                                               ad: MockDirectory) -> None:
    b = with_directory
    r = await login(b, "Ali.Rezaei", AD_PASSWORD)
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await b.client.get("/api/auth/me", headers=h)).json()
    assert me["auth_source"] == "ldap" and me["display_name"] == "Ali Rezaei"
    assert me["roles"] == [{"role": "operator", "site": "kish", "area": "gate"}]
    assert me["must_change_password"] is False
    user = await b.store.get_user_by_username("ali.rezaei")
    assert user is not None and user.external_dn == ALI
    events = [e.event for e in await b.store.list_auth_audit(limit=10)]
    assert {"user_created", "roles_synced", "login_ok"} <= set(events)

    # the directory decides: new group -> new role at the next sign-in
    ad.set_groups(ALI, [ENGINEERS])
    r = await login(b, "ali.rezaei", AD_PASSWORD)
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await b.client.get("/api/auth/me", headers=h)).json()
    assert me["roles"] == [{"role": "engineer", "site": None, "area": None}]


async def test_wrong_passwords_lock_the_account(with_directory: Backend) -> None:
    b = with_directory
    assert (await login(b, "ali.rezaei", AD_PASSWORD)).status_code == 200
    for _ in range(3):                                   # login_max_failures = 3 in the tests
        r = await login(b, "ali.rezaei", "Wrong-Pass-1")
        assert r.status_code == 401
    r = await login(b, "ali.rezaei", AD_PASSWORD)
    assert r.status_code == 401 and r.json()["error"] == "account_locked"


async def test_no_mapped_group_no_access(with_directory: Backend) -> None:
    b = with_directory
    r = await login(b, "no.role", AD_PASSWORD)
    assert r.status_code == 401 and r.json()["error"] == "no_role"
    assert await b.store.get_user_by_username("no.role") is None


async def test_directory_down_local_accounts_still_work(with_directory: Backend,
                                                        ad: MockDirectory) -> None:
    b = with_directory
    await b.user("local.admin", ("admin", None, None))
    ad.down = True
    r = await login(b, "ali.rezaei", AD_PASSWORD)
    assert r.status_code == 503 and r.json()["error"] == "directory_unavailable"
    assert (await login(b, "local.admin", PASSWORD)).status_code == 200


async def test_directory_accounts_are_managed_in_the_directory(with_directory: Backend) -> None:
    b = with_directory
    r = await login(b, "ali.rezaei", AD_PASSWORD)
    h_ali = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await b.client.post("/api/auth/change-password", headers=h_ali,
                            json={"current_password": AD_PASSWORD,
                                  "new_password": "Another-Pass-77"})
    assert r.status_code == 409 and r.json()["error"] == "directory_account"
    await b.user("boss", ("admin", None, None))
    h = await b.headers("boss")
    ali = await b.store.get_user_by_username("ali.rezaei")
    assert ali is not None
    for method, path, body in [
        ("POST", f"/api/users/{ali.user_id}/password", {"new_password": "Reset-Pass-123"}),
        ("POST", f"/api/users/{ali.user_id}/roles", {"role": "admin"}),
    ]:
        r = await b.client.request(method, path, headers=h, json=body)
        assert r.status_code == 409, (path, r.text)
    users = (await b.client.get("/api/users", headers=h)).json()
    assert next(u for u in users if u["username"] == "ali.rezaei")["auth_source"] == "ldap"
    # disabled in the hub: refused even with the right directory password
    r = await b.client.patch(f"/api/users/{ali.user_id}", headers=h, json={"is_active": False})
    assert r.status_code == 200
    assert (await login(b, "ali.rezaei", AD_PASSWORD)).status_code == 401


async def test_local_users_ignore_the_directory(with_directory: Backend,
                                               ad: MockDirectory) -> None:
    b = with_directory
    await b.user("op.local", ("operator", None, None))
    before = len(ad.binds)
    assert (await login(b, "op.local", PASSWORD)).status_code == 200
    assert len(ad.binds) == before


def test_config_file_is_loaded_and_checked(tmp_path: Any) -> None:
    import yaml

    from hub.config_loader import load_config

    (tmp_path / "ldap.yaml").write_text(yaml.safe_dump(config().model_dump(mode="json")),
                                        encoding="utf-8")
    report = load_config(tmp_path, env={})
    assert report.ldap is not None and report.ldap.url == "ldaps://dc1.kish.local"
    assert any("HUB_LDAP_BIND_PASSWORD" in i.message for i in report.issues)
    (tmp_path / "ldap.yaml").write_text("url: ldap://dc1\nbind_dn: CN=a,DC=b\n"
                                        "bind_password_env: X_Y\nuser_base: DC=b\n"
                                        "mappings: [{group: 'CN=g,DC=b', role: viewer}]\n",
                                        encoding="utf-8")
    assert load_config(tmp_path, env={}).has_errors            # clear text refused


async def test_password_recheck_uses_the_directory(with_directory: Backend) -> None:
    from backend.errors import Unauthorized

    b = with_directory
    assert (await login(b, "ali.rezaei", AD_PASSWORD)).status_code == 200
    ali = await b.store.get_user_by_username("ali.rezaei")
    assert ali is not None
    await b.services.auth._check_own_password(ali, AD_PASSWORD)   # noqa: SLF001
    with pytest.raises(Unauthorized):
        await b.services.auth._check_own_password(ali, "Wrong-Pass-1")   # noqa: SLF001
