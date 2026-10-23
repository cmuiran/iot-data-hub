"""Role-based access control with site/area scopes. Default deny.

A user holds role grants; each grant has a scope: (site=None) everywhere, (site) one site, or
(site, area) one area. A permission is allowed for a target (site, area) if some grant whose
role carries the permission covers that target. Global permissions (user management, config,
audit) need a grant with no site restriction.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from hub.storage.records import RoleGrant, UserRecord

GLOBAL_PERMISSIONS = frozenset({"user.manage", "config.edit", "audit.read"})
SCOPED_PERMISSIONS = frozenset({"data.read", "tag.write", "write.confirm", "alarm.ack"})
ALL_PERMISSIONS = GLOBAL_PERMISSIONS | SCOPED_PERMISSIONS


@dataclass(frozen=True)
class Scope:
    site: str | None = None
    area: str | None = None

    def covers(self, site: str | None, area: str | None) -> bool:
        if self.site is None:
            return True
        if site != self.site:
            return False
        return self.area is None or area == self.area


@dataclass(frozen=True)
class Principal:
    user: UserRecord
    session_id: object
    grants: tuple[RoleGrant, ...]
    role_permissions: dict[str, frozenset[str]]

    @property
    def user_id(self) -> int:
        return self.user.user_id

    @property
    def username(self) -> str:
        return self.user.username

    def _scopes_for(self, permission: str) -> list[Scope]:
        return [Scope(g.site, g.area) for g in self.grants
                if permission in self.role_permissions.get(g.role_id, frozenset())]

    def can(self, permission: str, site: str | None = None, area: str | None = None) -> bool:
        scopes = self._scopes_for(permission)
        if permission in GLOBAL_PERMISSIONS:
            return any(s.site is None for s in scopes)
        if site is None:  # "anywhere at all?" (e.g. may this user see the alarm list)
            return bool(scopes)
        return any(s.covers(site, area) for s in scopes)

    def effective(self) -> dict[str, list[dict[str, str | None]]]:
        out: dict[str, list[dict[str, str | None]]] = {}
        for permission in sorted(ALL_PERMISSIONS):
            scopes = self._scopes_for(permission)
            if permission in GLOBAL_PERMISSIONS:
                scopes = [s for s in scopes if s.site is None]
            if scopes:
                out[permission] = [{"site": s.site, "area": s.area} for s in
                                   sorted(set(scopes), key=lambda s: (s.site or "", s.area or ""))]
        return out


def split_path(tag_path: str) -> tuple[str, str, str, str]:
    site, area, device_id, tag = tag_path.split(".", 3)
    return site, area, device_id, tag


def filter_paths(principal: Principal, paths: Iterable[str], permission: str = "data.read"
                 ) -> list[str]:
    out = []
    for path in paths:
        site, area, _, _ = split_path(path)
        if principal.can(permission, site, area):
            out.append(path)
    return out

