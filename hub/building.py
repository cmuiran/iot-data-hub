"""Building information model: lookups over config/building.yaml.

``BuildingModel`` indexes a validated :class:`hub.models.BuildingFile` (locations as a tree,
systems, equipment and their points) for the calculation engine (aggregates over a room, floor
or building), the API and RBAC (the site a location belongs to). Cross-object checks (unknown
parents, cycles, unknown tags) are done by ``hub.config_loader``; the methods here assume a
configuration that passed them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from hub.models import BuildingFile, Equipment, Location, LocationKind


@dataclass
class BuildingModel:
    file: BuildingFile = field(default_factory=BuildingFile)

    def __post_init__(self) -> None:
        self.locations: dict[str, Location] = {loc.id: loc for loc in self.file.locations}
        self.children: dict[str | None, list[str]] = defaultdict(list)
        for loc in self.file.locations:
            self.children[loc.parent].append(loc.id)
        self.systems = {s.id: s for s in self.file.systems}
        self.equipment: dict[str, Equipment] = {e.id: e for e in self.file.equipment}
        self.site_orgs: dict[str, str] = {}
        for org in self.file.organizations:
            for site in org.sites:
                self.site_orgs.setdefault(site, org.id)
        self.point_of_tag: dict[str, tuple[str, str]] = {}
        for e in self.file.equipment:
            for p in e.points:
                self.point_of_tag.setdefault(p.tag, (e.id, p.role))

    @property
    def empty(self) -> bool:
        return not (self.file.locations or self.file.equipment or self.file.organizations)

    # --- tree -----------------------------------------------------------------------------------

    def ancestors(self, location_id: str) -> list[Location]:
        """The location and its parents, nearest first (stops at a cycle or unknown parent)."""
        out: list[Location] = []
        seen: set[str] = set()
        current: str | None = location_id
        while current is not None and current not in seen and current in self.locations:
            seen.add(current)
            loc = self.locations[current]
            out.append(loc)
            current = loc.parent
        return out

    def subtree(self, location_id: str) -> list[str]:
        """The location and every location below it."""
        out: list[str] = []
        stack = [location_id]
        while stack:
            current = stack.pop()
            if current in out:
                continue
            out.append(current)
            stack.extend(reversed(self.children.get(current, [])))
        return out

    def site_of(self, location_id: str) -> str | None:
        for loc in self.ancestors(location_id):
            if loc.site is not None:
                return loc.site
        return None

    def building_of(self, location_id: str) -> str | None:
        for loc in self.ancestors(location_id):
            if loc.kind is LocationKind.BUILDING:
                return loc.id
        return None

    def organization_of_site(self, site: str) -> str | None:
        return self.site_orgs.get(site)

    # --- equipment ------------------------------------------------------------------------------

    def equipment_in(self, location_id: str) -> list[Equipment]:
        inside = set(self.subtree(location_id))
        return [e for e in self.file.equipment if e.location in inside]

    def points_in(self, location_id: str, role: str) -> list[str]:
        """Tag paths of every point with ``role`` in the location or below it (retired
        equipment excluded), in configuration order."""
        return [p.tag for e in self.equipment_in(location_id) if e.status != "retired"
                for p in e.points if p.role == role]
