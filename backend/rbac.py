"""Role-based access control with organization/site/area scopes. Default deny.

A user holds role grants; each grant has a scope: everywhere (no site, no organization), one
organization (every site the organization owns in config/building.yaml), one site, or one
site + area. A permission is allowed for a target (site, area) if some grant whose role carries
the permission covers that target. Global permissions (user management, config, audit, model,
calculations) need a grant with no restriction at all.

Platform permissions (products, device registration) are organization permissions: allowed in
an organization by a grant for that organization, or by an unrestricted grant; site grants do
not count. :meth:`Principal.orgs_with` gives the organizations (None = all).

Writes: ``tag.write`` allows every write; ``control.setpoint`` / ``control.command`` allow only
writes to tags of that write class (a tag without write_class counts as a command).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from hub.models import WriteClass
from hub.storage.records import RoleGrant, UserRecord

GLOBAL_PERMISSIONS = frozenset({"user.manage", "config.edit", "audit.read", "model.edit",
                                "calc.publish", "org.manage"})
ORG_PERMISSIONS = frozenset({"product.read", "product.manage", "device.register",
                             "device.view", "device.operate", "device.share", "org.members",
                             "automation.manage"})
SCOPED_PERMISSIONS = frozenset({"data.read", "tag.write", "write.confirm", "alarm.ack",
                                "control.setpoint", "control.command", "maintenance.write"})
ALL_PERMISSIONS = GLOBAL_PERMISSIONS | SCOPED_PERMISSIONS | ORG_PERMISSIONS
WRITE_PERMISSIONS = frozenset({"tag.write", "control.setpoint", "control.command"})


def write_permission(write_class: WriteClass | str | None) -> str:
    """The class-specific permission for a write (unset = command, the stricter one)."""
    return "control.setpoint" if write_class == WriteClass.SETPOINT else "control.command"


@dataclass(frozen=True)
class Scope:
    site: str | None = None
    area: str | None = None
    org: str | None = None

    @property
    def unrestricted(self) -> bool:
        return self.site is None and self.org is None

    def covers(self, site: str | None, area: str | None,
               site_orgs: Mapping[str, str] | None = None) -> bool:
        if self.org is not None:
            return site is not None and (site_orgs or {}).get(site) == self.org
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
    mfa_missing: bool = False        # a role requires MFA but the user has not enrolled yet
    site_orgs: Mapping[str, str] = field(default_factory=dict)   # site -> organization

    @property
    def user_id(self) -> int:
        return self.user.user_id

    @property
    def username(self) -> str:
        return self.user.username

    def _scopes_for(self, permission: str) -> list[Scope]:
        return [Scope(g.site, g.area, g.org_id) for g in self.grants
                if permission in self.role_permissions.get(g.role_id, frozenset())]

    def can(self, permission: str, site: str | None = None, area: str | None = None) -> bool:
        scopes = self._scopes_for(permission)
        if permission in GLOBAL_PERMISSIONS:
            return any(s.unrestricted for s in scopes)
        if permission in ORG_PERMISSIONS:        # "in some organization?"; see orgs_with
            return any(s.unrestricted or s.org for s in scopes)
        if site is None:  # "anywhere at all?" (e.g. may this user see the alarm list)
            return bool(scopes)
        return any(s.covers(site, area, self.site_orgs) for s in scopes)

    def can_write(self, write_class: WriteClass | str | None, site: str | None = None,
                  area: str | None = None) -> bool:
        return self.can("tag.write", site, area) or \
            self.can(write_permission(write_class), site, area)

    def can_write_anything(self) -> bool:
        return any(self.can(p) for p in WRITE_PERMISSIONS)

    def orgs_with(self, permission: str) -> set[str] | None:
        """Organizations where an organization permission is allowed; None = every one."""
        scopes = self._scopes_for(permission)
        if any(s.unrestricted for s in scopes):
            return None
        return {s.org for s in scopes if s.org}

    def can_in_org(self, permission: str, org_id: str) -> bool:
        orgs = self.orgs_with(permission)
        return orgs is None or org_id in orgs

    def organizations(self) -> set[str] | None:
        """Organizations whose data the user may read; None = no restriction."""
        scopes = self._scopes_for("data.read")
        if any(s.unrestricted for s in scopes):
            return None
        orgs = {s.org for s in scopes if s.org}
        orgs |= {self.site_orgs[s.site] for s in scopes if s.site in self.site_orgs}
        return orgs

    def effective(self) -> dict[str, list[dict[str, str | None]]]:
        out: dict[str, list[dict[str, str | None]]] = {}
        for permission in sorted(ALL_PERMISSIONS):
            scopes = self._scopes_for(permission)
            if permission in ORG_PERMISSIONS:
                orgs = self.orgs_with(permission)
                if orgs is None:
                    out[permission] = [{"site": None, "area": None}]
                elif orgs:
                    out[permission] = [{"org": o} for o in sorted(orgs)]
                continue
            if permission in GLOBAL_PERMISSIONS:
                scopes = [s for s in scopes if s.unrestricted]
            # organization scopes are listed as their sites (that is what clients check)
            expanded = {Scope(site, None) for s in scopes if s.org
                        for site, org in self.site_orgs.items() if org == s.org}
            expanded |= {s for s in scopes if not s.org}
            if expanded:
                out[permission] = [{"site": s.site, "area": s.area} for s in sorted(
                    expanded, key=lambda s: (s.site or "", s.area or ""))]
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
