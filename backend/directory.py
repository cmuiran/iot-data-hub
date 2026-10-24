"""Sign-in against Active Directory / LDAP (config/ldap.yaml).

1. Bind with the service account and find the user (``user_filter``, the username escaped so
   it cannot change the filter).
2. Collect the user's groups: ``memberOf`` and, with ``nested_groups`` on Active Directory, the
   groups that contain them (matching rule LDAP_MATCHING_RULE_IN_CHAIN).
3. Check the password with a bind as the user. An empty password is refused before that: an
   LDAP simple bind with an empty password is an anonymous bind and "succeeds".
4. Map groups to hub roles (``mappings``); the caller refuses users with none.

Only encrypted connections (LDAPS or StartTLS, certificate checked). ldap3 is synchronous, so
the work runs in a thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from hub.models import LdapFile

log = logging.getLogger(__name__)
IN_CHAIN = "1.2.840.113556.1.4.1941"            # Active Directory: transitive membership
PASSWORD_MARKER = "!directory"                  # password_hash of directory users: never matches


class DirectoryError(Exception):
    """The directory cannot be used (unreachable, TLS, service account refused)."""


class DirectoryRejected(Exception):
    """Unknown user, ambiguous user or wrong password."""


@dataclass
class DirectoryUser:
    dn: str
    display_name: str
    groups: set[str] = field(default_factory=set)


def normalize_dn(dn: str) -> str:
    """Case and spacing do not matter in DN comparisons (CN=Ops, OU=G == cn=ops,ou=g)."""
    return re.sub(r"\s*([,=])\s*", r"\1", dn.strip()).lower()


class Directory:
    def __init__(self, cfg: LdapFile, env: Mapping[str, str] | None = None,
                 connection_factory: Callable[[str, str], Any] | None = None) -> None:
        self.cfg = cfg
        self._env = os.environ if env is None else env
        self._factory = connection_factory or self._connection
        self._server: Any = None
        self._mappings = [(normalize_dn(m.group), m.role, m.site, m.area) for m in cfg.mappings]

    # --- connections --------------------------------------------------------------------------

    def _make_server(self) -> Any:
        import ldap3

        c = self.cfg
        ca = self._env.get(c.ca_file_env) if c.ca_file_env else None
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=ca,
                        version=ssl.PROTOCOL_TLS_CLIENT)
        return ldap3.Server(c.url, use_ssl=c.url.startswith("ldaps://"), tls=tls,
                            get_info=ldap3.NONE, connect_timeout=c.timeout_s)

    def _connection(self, user: str, password: str) -> Any:
        import ldap3

        if self._server is None:
            self._server = self._make_server()
        return ldap3.Connection(self._server, user=user, password=password,
                                receive_timeout=self.cfg.timeout_s, raise_exceptions=False,
                                read_only=True)

    def _open(self, user: str, password: str) -> tuple[Any, bool]:
        """(connection, bound?) over an encrypted channel."""
        from ldap3.core.exceptions import LDAPException

        try:
            conn = self._factory(user, password)
            conn.open()
            if self.cfg.start_tls:
                conn.start_tls()
            return conn, bool(conn.bind())
        except LDAPException as exc:
            raise DirectoryError(f"{self.cfg.url}: {type(exc).__name__}: {exc}") from exc
        except OSError as exc:
            raise DirectoryError(f"{self.cfg.url}: {exc}") from exc

    # --- sign-in ------------------------------------------------------------------------------

    def authenticate_sync(self, username: str, password: str) -> DirectoryUser:
        from ldap3 import SUBTREE
        from ldap3.utils.conv import escape_filter_chars

        if not password:
            raise DirectoryRejected("empty password")
        c = self.cfg
        secret = self._env.get(c.bind_password_env)
        if not secret:
            raise DirectoryError(f"environment variable {c.bind_password_env} is not set")
        svc, ok = self._open(c.bind_dn, secret)
        try:
            if not ok:
                raise DirectoryError("the service account was refused: check bind_dn and "
                                     f"{c.bind_password_env}")
            query = c.user_filter.replace("{username}", escape_filter_chars(username))
            attribute = c.display_name_attribute
            svc.search(c.user_base, query, SUBTREE, attributes=[attribute, "memberOf"],
                       size_limit=2)
            entries = list(svc.entries)
            if not entries:
                raise DirectoryRejected("unknown user")
            if len(entries) > 1:
                raise DirectoryRejected("more than one directory entry matches the username")
            entry = entries[0]
            dn = entry.entry_dn
            attrs = entry.entry_attributes_as_dict
            groups = {normalize_dn(g) for g in attrs.get("memberOf", [])}
            if c.nested_groups:
                svc.search(c.group_base or c.user_base,
                           f"(member:{IN_CHAIN}:={escape_filter_chars(dn)})", SUBTREE,
                           attributes=[])
                groups |= {normalize_dn(e.entry_dn) for e in svc.entries}
            names = attrs.get(attribute) or [username]
            display = str(names[0] if isinstance(names, list) else names)
        finally:
            svc.unbind()
        user, ok = self._open(dn, password)
        user.unbind()
        if not ok:
            raise DirectoryRejected("wrong password (or the account is disabled / locked)")
        return DirectoryUser(dn, display, groups)

    async def authenticate(self, username: str, password: str) -> DirectoryUser:
        return await asyncio.to_thread(self.authenticate_sync, username, password)

    def roles_for(self, groups: set[str]) -> list[tuple[str, str | None, str | None]]:
        normalized = {normalize_dn(g) for g in groups}
        return sorted({(role, site, area) for group, role, site, area in self._mappings
                       if group in normalized}, key=lambda g: (g[0], g[1] or "", g[2] or ""))
