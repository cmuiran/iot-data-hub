# Building information model (BMS phase 1)

`config/building.yaml` describes where things are and what they are. It sits **on top of** the
tags: a point refers to an existing tag path (`site.area.device.tag`), and tag names never change
because of the model, so history, alarms, control policies and dashboards are unaffected.

```
Organization ─► Campus ─► Building ─► Floor ─► Zone ─► Room
                              │
              System (hvac, electrical, water, …) ─► Equipment ─► Point ─► Tag
```

Schema: `BuildingFile` in `hub/models.py`. Cross-checks: `_check_building` in
`hub/config_loader.py`. Lookups: `hub/building.py`. Example: `config/building.yaml`.

## Objects

| Object | Fields | Rules |
|---|---|---|
| Organization | `id`, `name`, `sites` | a site belongs to at most one organization; sites of devices without an organization are warned about |
| Location | `id`, `kind` (campus/building/floor/zone/room), `name`, `parent`, `site` (campus only), `level` (floor only), `area_m2`, `description`, `haystack`, `brick_class` | campus is top level; building under campus; floor under building; zone under floor; room under floor or zone (so the tree cannot have cycles) |
| System | `id`, `kind` (hvac, electrical, water, lighting, fire, access, elevator, security, metering, other), `name`, `building` | `building` must be a building; points of a `fire` system must not be `read_write` |
| Equipment | `id`, `name`, `type`, `location`, `system`, `device`, `manufacturer`, `model`, `serial_number`, `status` (in_service, maintenance, out_of_service, retired), `installed_on`, `warranty_until`, `description`, `documents` [`title`, `ref`], `haystack`, `brick_class`, `points` | not directly on a campus; must be in its system's building; `documents.ref` is an `http(s)` link or a relative path without `..` |
| Point | `role`, `tag`, `description`, `haystack`, `brick_class` | role unique per equipment; a tag is a point of at most one equipment; unit, data type, range and access come from the tag |

Ids use the tag segment rule (`[a-z][a-z0-9_]{0,63}`) and are stable: they are the keys in the
database and the API. `haystack` holds Project Haystack markers (camelCase), `brick_class` a Brick
Schema class name; both are only stored and checked for shape (export is a later phase).

Retired equipment stays in the model (and its maintenance history in the database) but is left
out of building aggregates (`sum_of`, …).

## Database (migration 0008)

The hub mirrors the file on start-up and on every configuration change (`sync_building`):
`organizations`, `organization_sites`, `locations`, `building_systems`, `equipment`,
`equipment_points`; objects that disappear get `retired_at`. Maintenance history is data, not
configuration: `maintenance_records` (append-only, corrections are new records).

## API

All read routes need `data.read`; visibility follows the user's scopes (an equipment is visible
when one of its points is; a location when its whole site is, or when it contains visible
equipment). Hidden objects answer 404.

| Route | Purpose |
|---|---|
| `GET /api/model/locations` | visible locations, parents first (`parent` builds the tree), with equipment counts |
| `GET /api/model/locations/{id}` | one location: path, children, equipment |
| `GET /api/model/systems` | systems of visible buildings |
| `GET /api/model/equipment?location=&system=&type=&status=` | equipment list (filter by subtree) |
| `GET /api/model/equipment/{id}` | equipment with its points: live value, source time, receive time, quality and reason, access and write class |
| `GET /api/model/points/{tag}` | which equipment / role a tag belongs to |
| `GET /api/model/equipment/{id}/maintenance` | maintenance history |
| `POST /api/model/equipment/{id}/maintenance` | add a record (`maintenance.write` for the equipment's area); audited as `maintenance_recorded` |
| `GET/PUT /api/config/building` | the file itself (`model.edit`); validated like every configuration change |

Dashboard: **Building** page (tree, location, equipment, points, documents, maintenance).
