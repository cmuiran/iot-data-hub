# Platform plane: products, template versions, devices (platform phase 1)

Engineering devices still come from `config/devices/*.yaml`. Platform devices are defined in
the database: a manufacturer defines a **product** once, publishes **template versions**, and
registers each manufactured unit as a **device instance** with its own identifiers, tags and
history. Design and decisions: `docs/fa/platform/phase0-audit.md` (ADR-001 .. ADR-010).

Code: `hub/platform/template.py` (template model), `hub/platform/devices.py` (identifiers,
building the device model), `hub/storage/platform.py` (store, isolation),
`backend/api/platform.py` (API), `db/migrations/0009_platform_core.sql`.

## Data model

| Table | Holds |
|---|---|
| `organizations` | tenants; `kind` platform / manufacturer / customer / site_operator; `source` config (building.yaml) or platform |
| `products` | one row per product model of a manufacturer: `code` (unique per organization), name, category, image, `identifier_pattern` |
| `product_versions` | the template (JSON, below) per version; `draft` → `published` (then immutable, enforced by a trigger) → `deprecated` |
| `platform_devices` | one row per physical unit: generated `device_id`, `identifier` (globally unique), `serial_number` (unique per product), product + version, maker, `lifecycle`, batch, warranty, protocol options |
| `device_access` | which organizations use a device (`owner` / `operator` / `viewer`); one owner at a time (written by the claim flow, phase 3) |
| `devices`, `tags`, `device_status`, `samples`, … | unchanged; platform devices have rows here like YAML devices (`devices.source = 'platform'`), so history, alarms and control need no change |

Identifiers: `device_id` = `d` + 20 base32 characters (internal, not guessable);
`identifier` = what the buyer types (`CTRL-8R-0001`, from the pattern `CTRL-8R-{0000}`, upper
case, 3–64 characters `A-Z 0-9 -`); `serial_number`; tag key (stable). Tag path:
`platform.devices.<device_id>.<key>`: independent of owner and location.

## Template

```json
{
  "tags": [
    {"key": "relay_1", "kind": "digital_output", "display_name": {"fa": "رله ۱", "en": "Relay 1"},
     "data_type": "bool", "access": "read_write", "write_class": "command",
     "ui": {"chart": true, "scenario": true, "order": 1, "group": "outputs"}},
    {"key": "power", "kind": "analog_input", "display_name": {"en": "Power"},
     "data_type": "float32", "unit": "kW", "min": 0, "max": 10}
  ],
  "bindings": {
    "modbus_tcp": {"connection": {"port": 502, "unit_id": 1}, "device": {"poll_interval_ms": 1000},
                   "tags": {"relay_1": {"register_type": "coil", "address": 0},
                            "power": {"register_type": "input", "address": 0}}},
    "mqtt": {"connection": {"broker_ref": "main", "telemetry_topic": "d/{device_id}/telemetry"},
             "tags": {"relay_1": {"json_path": "$.relays[0]"}, "power": {"json_path": "$.power"}}}
  }
}
```

* `kind`: digital_input / digital_output (bool), analog_input / analog_output (numeric),
  variable, parameter. Inputs and variables are read-only; outputs and parameters may be
  `read_write` (then min/max are required for numbers, as for any writable tag). Default:
  read-only.
* Other tag fields: `description`, `unit`, `min`/`max`, `states` (enum labels), `default`,
  `ui` (visible, chart, scenario, order, group, icon).
* `bindings`: one per protocol (`modbus_tcp`, `modbus_rtu`, `mqtt`, `opcua`, `s7`, `http`,
  `snmp`, `fins`, `mc`, `enip`, `iec104`). Each binding maps **every** tag to the protocol's
  address fields (plus scale/offset/history options) and is validated with that protocol's tag
  model, i.e. by the same rules as device YAML. `connection` holds defaults merged under each
  device's own connection; strings may use `{device_id}`, `{identifier}`, `{serial}`.
* Any number and type of tags; no code or table per product.

## Isolation

Tenant requests run in a transaction as the database role `hub_tenant` with
`app.org_ids` = the caller's organizations. Row-level security on `organizations`, `products`,
`product_versions`, `platform_devices` and `device_access` limits that role to its rows, and it
has no privilege on any other table (users, samples, tags, …). Runtime details of visible
devices are then read by the application role for exactly those device ids. A manufacturer
sees its products and devices but not who uses them; a buyer organization (`device_access`)
sees only its devices and their products. Another organization's object answers 404.

`hub_tenant` is created by migration 0009 when the migrating role may create roles; otherwise
a superuser runs once: `CREATE ROLE hub_tenant NOLOGIN; GRANT hub_tenant TO hub_app;`.

## Permissions

| Permission | Scope | Roles |
|---|---|---|
| `org.manage` | global | admin |
| `product.read` | organization | manufacturer_admin, production_operator, manufacturer_support, admin |
| `product.manage` | organization | manufacturer_admin, admin |
| `device.register` | organization | manufacturer_admin, production_operator, admin |

Grant with an organization scope (`POST /api/users/{id}/roles` with `organization`). Grants for
the site `platform` are refused: platform device access comes from organizations.

## API (`/api/platform`)

| Method and path | Permission | |
|---|---|---|
| `POST /organizations` | org.manage | `{org_id, name, kind}` |
| `GET /organizations` | product.read | the caller's organizations |
| `GET /products?org=` / `POST /products` | product.read / product.manage | `{org_id, code, name, category, description, image_ref, identifier_pattern}` |
| `GET /products/{id}` | product.read | with latest published version, draft, device count |
| `GET /products/{id}/versions`, `GET …/versions/{v}` | product.read | |
| `PUT /products/{id}/draft` | product.manage | `{definition, notes}`: create/replace the draft; 422 `invalid_template` with details |
| `POST /products/{id}/versions/{v}/publish` | product.manage | |
| `POST /products/{id}/devices` | device.register | `{version, protocol, start + count | devices: [{identifier, serial_number}], batch, warranty_months, connection, options, enabled}` (max 10 000 per call; all or nothing; 409 `duplicate` with the values) |
| `GET /devices?product=&lifecycle=&q=&limit=&offset=` | product.read | |
| `GET /devices/{device_id}` | product.read | |
| `PATCH /devices/{device_id}` | device.register | `{name, connection, options, enabled, lifecycle}`; an enabled device must build into a valid device (422 `invalid_device`); the hub starts/stops it without a reload |

Audit (`auth_audit`): organization_created, product_saved, product_version_saved,
product_version_published, devices_registered, platform_device_updated.

## Hub

On start the hub loads every enabled platform device (`PlatformStore.device_models`) next to the
YAML devices; `Hub.apply_platform_device` starts, restarts or stops one device; a YAML reload
keeps platform devices running; the YAML sync never retires them. The control-safety layer
refuses every write to a platform device (`rejected_disabled`, "platform phase 4"), whoever
asks, until phase 4 adds the command lifecycle and per-device write permissions.

Also visible to unrestricted (global) grants only, as for any site: platform tags in the
engineering API (`/api/data`), northbound outputs and the OPC UA / Modbus servers when those are
configured for all tags. Tenant users reach their devices through `/api/platform` only.

# Accounts, members and sharing (platform phase 2)

Code: `backend/accounts.py` (rules), `backend/mailer.py` + `backend/messages/emails.json`
(e-mail texts fa/en), `hub/storage/accounts.py` (store), `backend/api/account.py`,
`db/migrations/0010_accounts_sharing.sql`.

## Accounts

* **Sign-up** `POST /api/account/signup {email, password, display_name, language,
  invitation_token?}`: only with `HUB_SIGNUP_ENABLED=true`, or with an invitation (its link
  proves the address, so that account is active at once). Answer `202 {"status":
  "check_email"}` whether or not the address has an account; an existing owner gets an e-mail
  instead (no enumeration). The password policy is the hub's (length, variety, history).
* **Verification** `POST /api/account/verify {token}`, `POST /api/account/verify/resend
  {email}`. A verified account gets a **personal organization** (kind customer) and the role
  `customer_owner` in it.
* **Sign-in** with the e-mail address in `username` (`/api/auth/login`); refused with
  `email_not_verified` until verified. Admin-created and directory accounts are unchanged.
* **Password reset** `POST /api/account/password/forgot {email}` (always 202),
  `POST /api/account/password/reset {token, new_password}`: ends every session.
* Tokens: 256-bit random, only the SHA-256 is stored, single use, expiry (`HUB_VERIFY_HOURS`,
  `HUB_RESET_MINUTES`, `HUB_INVITATION_DAYS`); links carry the token after `#`, so it never
  reaches server or proxy logs: `{HUB_PUBLIC_URL}/#/account/verify/<token>`.
* Rate limits: `HUB_ACCOUNT_IP_PER_HOUR` per client address and action (429
  `account_throttled`, audited), `HUB_ACCOUNT_EMAIL_PER_HOUR` e-mails per address. In memory
  per node.
* E-mail: `HUB_SMTP_HOST/PORT/SECURITY`, `HUB_SMTP_USERNAME/PASSWORD`, `HUB_MAIL_FROM`; without
  a host nothing is sent and the log says so (never the message: it holds a one-time link).

## Organizations and members

| Role (organization scope) | Permissions |
|---|---|
| customer_owner | device.view, device.operate, device.share, org.members |
| customer_member | device.view, device.operate |
| customer_viewer | device.view |
| manufacturer_admin (+ phase 1) | org.members |

`device.operate` is granted but every write to a platform device is still refused by the
control-safety layer until phase 4.

| Method and path | Who |
|---|---|
| `GET /api/account/organizations` | the caller's memberships |
| `DELETE /api/account/organizations/{org}` | leave (not the owner of a personal organization) |
| `GET/POST /api/platform/organizations/{org}/invitations` `{email, role}` | org.members there; customer orgs invite customer roles, manufacturers manufacturer roles |
| `DELETE /api/platform/organizations/{org}/invitations/{id}` | org.members there |
| `GET /api/platform/organizations/{org}/members`, `DELETE …/members/{user_role_id}` | org.members there; the last owner stays; removal ends the member's sessions |
| `GET /api/account/invitations`, `POST /api/account/invitations/accept {token, organization?}` | the invitee (verified e-mail = the invitation's address) |

## Sharing a device

The owner organization (`device_access` level owner; the claim of phase 3 sets it) shares a
device with someone by e-mail: `POST /api/platform/devices/{id}/shares {email, level}` with
`level` operator or viewer (device.share). The invitee accepts into its personal organization
(or another organization where it has org.members); that organization gets `device_access` at
that level. `GET …/shares` lists shares and open invitations (owner only);
`DELETE …/shares/{access_id}` revokes (owner) or gives the share up (receiving organization).
A received share cannot be shared on. `GET /api/platform/my/devices` lists the devices the
caller's organizations own or received, with the best level (`access`).

Isolation (row-level security for hub_tenant, as in phase 1): invitations are visible only to
the inviting organization; an organization can invite only in its own name and share only a
device it owns; only `revoked_at` of `device_access` is writable, never the owner row; the
manufacturer sees neither owners nor shares.

Audit: account_signup, email_verified, password_reset_requested, password_reset,
invitation_created, invitation_accepted, invitation_revoked, member_removed, device_shared,
device_share_revoked, account_throttled.

# Claiming devices, labels, edge hubs (platform phase 3)

Code: `backend/claims.py` (rules), `hub/storage/claims.py`, `db/migrations/0011_claims.sql`.

## Production: registration and labels

* Register thousands of units per call (`start` + `count`, a list, or `csv`:
  `identifier[,serial_number]` per line, header optional; at most 10 000, all or nothing).
* `POST /api/platform/products/{id}/labels {device_ids?, batch?, credentials?}`
  (device.register) answers a CSV: `identifier, serial_number, claim_code, qr` and with
  `credentials: true` also `device_id, device_secret`. Only devices nobody owns get codes;
  issuing again replaces the earlier codes. The CSV is the only place codes and secrets ever
  appear (`Cache-Control: no-store`; not in logs or the audit).
* Claim code: 12 characters Crockford base32 (60 random bits) printed `XXXX-XXXX-XXXX`; typed
  loosely (case, spaces, O/I/L) it still matches. Stored as HMAC-SHA256 keyed with
  `HUB_DATA_KEY` (or the JWT secret when that is unset: then changing the JWT secret makes
  printed codes invalid — set `HUB_DATA_KEY` in production). QR text:
  `{HUB_PUBLIC_URL}/#/claim/<identifier>/<code>`.
* Device credential: 256-bit secret, SHA-256 stored, one active per device (issuing again
  rotates it); checked by `ClaimService.credential_valid` — the MQTT / HTTP / TCP front ends of
  phase 4 use it.

## The buyer's claim

`POST /api/platform/claim {identifier, code, organization?, name?}` (signed in; default target:
the caller's personal organization; another organization needs org.members there). Every
refusal is the same 422 `invalid_claim` "identifier or claim code is not valid": unknown
identifier, wrong / used / missing code, locked, disabled, retired or child device, already
owned (without a transfer code). A device locks for 15 minutes after 5 wrong codes; claims are
limited to 10 per user and `HUB_ACCOUNT_IP_PER_HOUR` per address per hour. The code is used
once; the device becomes `active` and the organization its owner.

Owner actions (device.share of the owner organization):

| | |
|---|---|
| `PATCH /api/platform/my/devices/{id} {name, connection, options, enabled}` | e.g. the Modbus address of the device; validated before it runs; the hub starts / stops it live |
| `POST /api/platform/my/devices/{id}/transfer` | a one-time transfer code (shown once); the device stays the owner's until someone claims it with that code, then every earlier access (shares included) ends |
| `DELETE /api/platform/my/devices/{id}` | remove it from the account: every access ends, it stops, lifecycle `sold` |

Manufacturer: `POST /api/platform/devices/{id}/reset` (device.register): factory reset, every
access ends, a fresh activation code (shown once).

## Edge hubs

A product of kind `edge_hub` is a gateway; its owner adds the equipment behind it from the
**catalog** (products with `catalog: true`, any manufacturer) or from the hub maker's own
products: `GET /api/platform/catalog`, `POST /api/platform/my/devices/{hub}/children
{product_id, version, protocol, name, connection, options, enabled}` (e.g. a Modbus meter at an
address on the hub's network), `DELETE …/children/{child}` (retired; history stays). A child gets
the identifier `<hub identifier>-001`, belongs to the hub's owner and moves with the hub on a
transfer or release; it cannot be claimed or transferred on its own.

In this phase the children run on this hub process (the hub is local to the equipment). The
link between an edge hub at a site and a central platform (outbound MQTT/TLS, commands through
the edge's own control layer) is platform phase 4.

# Device connectivity and commands (platform phase 4)

Code: `hub/drivers/native.py`, `backend/ingest.py`, `backend/tcp_ingest.py`,
`backend/mqtt_ingest.py`, `backend/api/ingest.py`, `hub/storage/commands.py`,
`simulators/native_device.py`, `db/migrations/0012_commands_ingest.sql`.

## Two kinds of connection

| | Protocols | Who connects |
|---|---|---|
| Polled (unchanged) | Modbus TCP/RTU, OPC UA, S7, FINS, MC, EtherNet/IP, IEC 104, SNMP, HTTP GET, MQTT/JSON (third-party topics) | the hub reads the device; per-device drivers; device settings from YAML or the platform database |
| **Platform device contract** (`protocol: native`) | MQTT, HTTP, TCP/TLS | the device connects to the hub with its credential (phase 3) |

A product gets the contract with a `native` binding: `{"tags": {"<key>": {}, …},
"connection": {"transports": ["mqtt", "http", "tcp"], "stale_after_s": 300,
"command_timeout_s": 30}}`. No addresses: the device reports values by tag key.

## Messages (the same JSON on every transport)

| | |
|---|---|
| telemetry (device → hub) | `{"ts": "<ISO>" \| <epoch ms>, "values": {"relay_1": true, "power": 1.8}}` — unknown keys ignored, a wrong type gives a bad sample, no `ts` = hub time, unreadable `ts` = uncertain |
| state (device → hub) | `{"state": "online" \| "offline"}` (MQTT: use it as the last will) |
| command (hub → device) | `{"command_id": "<uuid>", "tag": "relay_1", "value": true, "expires_at": "<ISO>"}` — the device must not execute a `command_id` twice and should drop expired ones |
| ack (device → hub) | `{"command_id": "<uuid>", "status": "received" \| "done" \| "failed", "value": <now>, "error": "…"}` |

Offline: no message for `stale_after_s` (or state offline) → device offline, every value bad.

## Transports

* **HTTP**: `Authorization: Bearer <device secret>`; `POST /ingest/v1/devices/{id}/telemetry`,
  `…/state`, `…/ack`, `GET …/commands` (waiting commands; the telemetry answer says how many).
* **TCP** (`HUB_TCP_INGEST_PORT`; TLS with `HUB_TLS_CERT` / `HUB_TLS_KEY`, plain only with
  `HUB_TCP_INGEST_INSECURE=true`): newline-delimited JSON; first line
  `{"device_id": "…", "secret": "…"}` → `{"ok": true}`; then `{"type": "telemetry" | "state" |
  "ack", …}`; the hub pushes `{"type": "command", …}` at once. 64 KiB per line, 10 s to sign
  in, one session per device.
* **MQTT** (`HUB_PLATFORM_BROKER` = a broker of `brokers.yaml`): one subscription for all
  devices to `<prefix>d/+/telemetry | state | ack` (`$share/<group>/` with
  `HUB_PLATFORM_SHARED_GROUP`), commands on `<prefix>d/<id>/cmd`. The broker authenticates each
  device and limits it to its own topics through the hub's hooks (`HUB_BROKER_HOOK_TOKEN`,
  header `X-Hub-Hook-Token`): `POST /ingest/v1/broker/auth {username: <device_id>, password:
  <secret>}` and `POST /ingest/v1/broker/acl {username, topic, action}` → `{"result": "allow" |
  "deny"}` (EMQX HTTP authentication / authorization; mosquitto-go-auth http backend). Without
  these hooks the MQTT transport is not authenticated: use HTTP or TCP.

Device sign-in failures are audited (`device_auth_failed`) and limited per address (20 per
10 minutes); a device that is switched off or removed cannot sign in.

A command goes to the open TCP session, else the platform broker (if the device uses MQTT),
else the HTTP queue.

## Who may command a platform device

`device.operate` through an organization grant, in an organization that owns the device or
received it at level operator. Viewers, other customers and unrestricted (global) grants are
refused (`rejected_rbac`); someone who cannot even see the device gets 404. Everything else of
the control-safety layer applies as before (HUB_CONTROL_ENABLED, ranges, rate, interlocks,
two-person confirmation per control.yaml, audit).

## The life of a command (every protocol)

`commands` (command_id = the write's request_id): `created → sent → delivered → executed →
confirmed`, or `failed` / `expired`, each step with its time; statuses only move forward and a
device can ack only its own commands.

| | sent | delivered | executed | confirmed | failed / expired |
|---|---|---|---|---|---|
| native | handed to TCP / MQTT, or fetched over HTTP | ack received | ack done | the value came back (telemetry or ack) | ack failed / no answer in `command_timeout_s` (write result `timeout`, state unknown) |
| polled | (= delivered) | the protocol write succeeded | — | read-back equals the value | write error, read-back mismatch / timeout |

`GET /api/platform/my/devices/{id}/commands` (device.view with access to the device) lists them.

## Simulator

`simulators/native_device.py` (`NativeDeviceSim`): holds values, sends telemetry, executes
commands and acks them over HTTP, TCP or MQTT; faults `refuse`, `silent`, `ack_only` for tests.
`python -m simulators.native_device --http http://127.0.0.1:8000 --device d… --secret …`.

## Not in this phase

* The link between an edge hub at a site and a central platform (an edge hub as a native
  device toward the centre, its children's values forwarded, commands passed down through the
  edge's own control layer).
* SNMP trap reception (the SNMP driver still polls).
* An MQTT broker itself: the hub uses one (EMQX recommended) and serves its auth/ACL hooks.

# Consumer app (platform phase 5)

`app/` — a PWA served by hub-api at **/app** (`HUB_APP_DIR`, `off` to disable). Plain ES
modules, no framework and no build step (nothing downloaded; it also runs on an edge hub
without internet); the dashboard's CSP applies unchanged (no inline script or style, nothing
from other hosts). Persian (RTL) by default, English in "Me"; dark by default, light on request
or by the system setting; Persian digits and relative times.

| Screen | |
|---|---|
| Sign in / sign up / forgot / reset / verify / invitation | the phase 2 API; the e-mail links (`#/account/verify/<token>`, …) open here |
| Home | greeting, counts (devices, online, offline), filters (all / online / shared), a card per device with an on/off switch when the product has a writable bool tag, else its first value |
| Device | outputs as a grid of buttons, writable parameters with min–max, values, the last 5 commands with their status, and for the owner: sharing by e-mail, transfer code, removal |
| Add | QR scan (BarcodeDetector + camera where the browser has them) or identifier + code; a scanned label link `#/claim/<identifier>/<code>` opens it pre-filled |
| Me / members | homes and roles, open invitations, language, theme, sign-out; owners invite and remove members |

The app reads `GET /api/platform/my/devices/{id}/view`: the product template's tags (names fa/en,
kinds, units, ranges, UI order) with live values and `writable` for this caller. Values refresh
every 5 seconds while the app is visible. The service worker keeps the app shell for offline
use (network first; never the API).

Not yet: push notifications (Web Push needs VAPID keys and a dependency), live updates over
WebSocket for customers, rooms / places inside a home, history charts.

# Scenes and automations, Jalali calendar (platform phase 6)

Code: `hub/jalali.py`, `backend/automations.py` (models, occurrences, engine),
`backend/api/automations.py`, `hub/storage/automations.py`, `app/js/auto.js`,
`db/migrations/0013_automations.sql`.

## Calendar

`hub/jalali.py`: exact Jalali ↔ Gregorian conversion (33-year arithmetic; tested day by day
2000–2040), weekdays from Saturday (0) to Friday (6), month lengths and leap years, the fixed
solar holidays (Nowruz 1–4 Farvardin, 12 and 13 Farvardin, 14 and 15 Khordad, 22 Bahman,
29 Esfand). The lunar (Hijri) holidays move every year and are **not computed**: they are added
as dates — for everyone by a platform administrator, or by a home for itself
(`POST /api/platform/organizations/{org}/holidays {date: "1405/02/10", name}`).
`GET …/holidays?year=1405` lists all three kinds; `GET /api/platform/calendar?day=1405/01/13`
describes a day. Times are the hub's zone (HUB_TIMEZONE, Asia/Tehran: no daylight saving since
1401 — the zone database decides, nothing is hard-coded).

## Automation

```json
{"name": "Office hours", "enabled": true,
 "trigger": {"type": "time", "at": "08:00", "weekdays": [0, 1, 2, 3, 4], "holidays": "skip"},
 "conditions": [{"device_id": "d…", "tag": "power", "op": "lt", "value": 9}],
 "window": {"between": ["06:00", "22:00"]},
 "actions": [{"type": "write", "device_id": "d…", "tag": "relay_1", "value": true},
             {"type": "delay", "seconds": 30}, {"type": "scene", "scene_id": 3},
             {"type": "notify", "text": "The office is on"}],
 "priority": 50, "respect_manual_min": 60, "missed": "skip", "catch_up_min": 30}
```

| Trigger | |
|---|---|
| `time` | `at` HH:MM; optional `weekdays` (0 = Sat), `months` and `month_days` (Jalali), `start` / `end` (Jalali dates), `holidays`: any / skip / only |
| `interval` | every `every_min` minutes from midnight, optionally `between` two times (may cross midnight), same calendar options |
| `once` | a Jalali `date` and `at` |
| `value` | a device tag meets `op` `value` (eq ne lt le gt ge), optionally for `for_s` seconds; fires on the change to true, not again while it stays true, not at start-up |
| `offline` | a device is offline for `for_s` seconds |

Conditions are all required (a value that is missing or bad is not met); `window` limits by
times, days and holidays. Actions: `write` (through the control-safety layer as the creator,
checked at run time: a member who left stops its automations), `scene`, `delay`, `notify`
(e-mail to the creator).

Rules:

* **Once per occurrence**: a run is stored with its occurrence (`time:<ISO>`, value edge) and
  `UNIQUE (automation_id, trigger_key)`: two hub nodes or a restart never run it twice.
* **Missed** occurrences (hub down): recorded as skipped; with `missed: catch_up` run once if at
  most `catch_up_min` late. A changed automation starts afresh.
* **Priorities**: safety (the control layer) first; then a person — a tag a person changed in the
  last `respect_manual_min` minutes is left alone (`skipped_manual`); then automations — of two
  writing one tag within a minute, the higher `priority` wins (`skipped_priority`).
* **Test**: `POST …/automations/{id}/test` evaluates now and lists what would be written
  (`would_write`); nothing is sent or stored.

## API (`/api/platform/organizations/{org}`)

Reading: device.view in the organization; changing: automation.manage (customer_owner,
customer_member, admin). Every device must be one the organization owns or received; writes need
owner / operator access, a writable tag and a value of its type and range.

`GET/POST /scenes`, `PUT/DELETE /scenes/{id}`, `POST /scenes/{id}/run` (with the caller's
rights), `GET/POST /automations` (answers carry the next 3 runs), `PUT/DELETE /automations/{id}`,
`POST /automations/{id}/test`, `GET /automations/{id}/runs`, `GET/POST /holidays`.

## App

"هوشمندسازی" tab: scene cards that run with one tap, automations as "if … then …" sentences with
their next run in the Persian calendar, an on/off switch, delete, and a builder (a time on chosen
weekdays, or a device value above / below / equal to a number → switch an output on or off).

Not yet: sunrise / sunset triggers, priority conflicts across hub nodes (the priority memory is
per node), editing an automation in the app (delete and create again), conditions and several
actions in the app's builder (the API has them), the lunar holidays of each year (to be added
as dates).

# Template upgrades and firmware (platform phase 7)

Code: `hub/platform/template.py` (`diff_templates`), `backend/versions.py`,
`backend/api/versions.py`, `hub/storage/versions.py`, `db/migrations/0014_versions_firmware.sql`.

**Upgrades** (product.manage of the maker):
`GET /api/platform/products/{id}/versions/{v}/upgrade-preview?from=` lists, per current version
and protocol, the tags added, removed and changed, whether it is *breaking* (a tag removed, its
data type changed, no longer writable, the protocol's binding gone) and the automations / scenes
that use a removed tag. `POST …/versions/{v}/upgrade {from_version?, device_ids?,
allow_breaking}` moves devices: same key = same tag id and history, new keys added, vanished
keys retired (history kept, never deleted). Breaking moves need `allow_breaking`; an enabled
device whose configuration would not validate stays where it is (reported per device).

**Firmware**: `POST /api/platform/products/{id}/firmware?version=&sha256=&notes=&min_template_version=`
with the image as the body (≤ 16 MB; a given SHA-256 must match), `GET …/firmware`,
`POST /api/platform/firmware/{id}/rollout {device_ids?}` (one open job per device; native
devices only; `min_template_version` respected), `GET …/jobs`, `POST …/withdraw` (open jobs are
cancelled). The device gets an offer — TCP `{"type": "ota", …}`, MQTT `d/<id>/ota`, or the HTTP
command queue — with `job_id, version, sha256, size, url`; it downloads
`GET /ingest/v1/devices/{id}/firmware/{release}` with its credential (only for its own open job),
checks the SHA-256 (and its own signature, e.g. ESP32 secure boot) and reports
`POST /ingest/v1/devices/{id}/ota {job_id, status: downloading | installed | failed, version}`
(TCP `{"type": "ota", …}`, MQTT `d/<id>/ota/status`). The version a device reports
(`installed`, or `{"state": "online", "firmware": "1.4.2"}`) is kept on the device.

Not yet: images signed by the hub (integrity is the SHA-256 over TLS; authenticity is the
device's own secure boot), staged rollouts by percentage, the owner postponing an update.

# Deployment, load and penetration tests (platform phase 8)

* **Cloud**: `deploy/` — Dockerfile (non-root, health check), docker-compose with
  TimescaleDB, EMQX (TLS 8883 only, device auth / ACL through the hub's hooks), Caddy (HTTPS,
  the broker hooks not exposed), the database on an internal network;
  `docs/fa/platform/deploy-cloud.md`. Not run here (no Docker on the development machine).
* **Request size**: every request body is limited to 1 MB (16 MB for firmware uploads), also
  without Content-Length (413 `too_large`).
* **Load**: `python -m simulators.platform_loadtest` — real hub-api + PostgreSQL, devices in
  separate processes; results in the phase report.
* **Penetration tests**: `tests/api/test_platform_pentest.py` (every platform route with another
  organization's real ids, every device route without a credential, forged / expired tokens,
  oversized bodies, injection in search, credential guessing throttled, security headers) in
  addition to `tests/api/test_pentest.py`.

# Maker panel and the completed app

`maker/` — the manufacturer's panel at **/maker** (`HUB_MAKER_DIR`, `off` to disable), built the
same way as the app (ES modules, no build step, same CSP, fa/en with RTL). Menu: dashboard,
products, devices & production, labels & QR, warranty, support, settings. Each page uses only
what the signed-in user's role in the selected manufacturer organization allows
(`product.manage`, `device.register`, `product.read`, `org.members`).

* **Product**: details and picture; inputs/outputs (the template's tags, group add such as
  8 relays, a tag editor, the per-protocol address of the selected tag, a preview of the buyer's
  app); commands (which outputs and parameters are controllable, their class, the native
  command timeout); protocols (connection defaults and per-tag addresses, forms generated from
  the hub's own models via `GET /api/platform/protocols`); versions (open a published version,
  upgrade devices with the breaking-change preview) and firmware (upload with SHA-256, offer,
  job status, withdraw). Saving goes to the draft and is validated by the server.
* **Production**: register a range (duplicates checked first) or a CSV, move a whole batch to a
  stage, the devices by stage, labels: new one-time codes printed as QR labels (the browser's
  print dialog makes the PDF) or the CSV for the production line, optionally with device
  credentials. The QR encoder is `maker/js/qr.js` (byte mode, level M, versions 1–10).
* **Warranty** starts at the buyer's first activation (`platform_devices.activated_at`) and runs
  `warranty_months`; `warranty_until` is in every device record. **Support** looks a device up
  and says what its state suggests, with new code / reset / block.

New API (all under `/api/platform`): `PATCH /products/{id}`, `PUT|GET|DELETE
/products/{id}/image` (PNG/JPEG/WebP ≤ 512 KB), `GET /stats[?product=]`, `GET /protocols`,
`POST /products/{id}/devices/check`, `POST /products/{id}/devices/lifecycle`,
`GET /devices?lifecycle=stock&batch=`, `PATCH /devices/{id}` with `warranty_months`; for the
app `PUT /my/devices/{id}/room` (rooms are per home, on the access row: the maker never sees
them), `GET /my/devices?org=`, `GET /my/devices/{id}/history?tag=`, `GET /my/summary`,
`GET /my/notifications`; `GET /catalog` lists each product's protocols. Migration
`0015_maker_home`.

The app now follows the design: home picker, bell, indoor temperature / energy today / alerts,
quick scenes, room filter, protocol badges; device page with all on / all off, a 24-hour chart
and where each command came from (app, automation, scene); device settings (name, room, facts,
sharing, edge-hub equipment); scenes and automations as two tabs with builders that also edit
(time / interval / once / value / offline triggers, a time window and a value condition,
several actions); adding equipment behind an edge hub; notifications. What is still open is
in `docs/fa/platform/todo-smart-home.md`.

# Second round: homes, groups, energy, offline control, maker extras

Migration `0016_home_maker_plus` (device groups, home settings, SMS settings, tickets and
messages, branding, plans, invoices; these tables are reached only through the API).

**App** (`backend/api/home.py`): `POST /api/account/homes`, `PATCH /api/account/homes/{org}`,
`POST /api/account/delete` (password + `"DELETE"`: devices of the personal home released, roles
and sessions end, name / e-mail / password erased); `GET|PUT
/api/platform/organizations/{org}/settings` (latitude, longitude, radius, tariff: tiers, peak
hours and factor, fixed charge, tax); `…/groups` (CRUD; a group view is composed by the app:
all members' tags together, or one device / one model); `…/my/devices/{id}/timers` (a countdown
is a `once` automation with `timer: true`, removed after it ran); `GET /api/platform/my/energy`
(kWh per Tehran day from kWh counters or hourly power averages, the Jalali month's total and
cost); `GET /api/platform/my/stream` (server-sent events: `samples`, `status` of the caller's
devices; ends after five minutes so the client reconnects with a fresh token);
`…/my/devices/{id}/tickets`, `/api/platform/my/tickets[/{id}[/messages]]`;
`POST …/my/devices/{id}/actions/{key}` (a template command: every parameter checked first,
then each written through the control-safety layer in order); `…/my/devices/{id}/sms`.

**Automations**: triggers `sun` (`sunrise` / `sunset`, `offset_min`, the home's coordinates,
weekdays; `hub/sun.py`, NOAA algorithm) and `presence` (`arrive` / `leave`, reported by the app
with `POST /api/platform/organizations/{org}/presence` while it is open); `local: true`
automations whose trigger, conditions and writes concern one device only are sent to that
device as `{"type": "rules", ...}` (MQTT `d/<id>/rules`, TCP, HTTP queue, and
`GET /ingest/v1/devices/{id}/rules`) and run by the device while it has no contact with the hub.

The rules message carries `server_time` (UTC, outside `version`): a device without NTP sets its
clock from it, and a device whose clock is not set runs no `time` rule.

**Names and sub-groups**: the owner home names any output or reading of a device and picks its
icon (`PUT /api/platform/my/devices/{id}/labels/{key}` `{name, icon, favorite}`; table
`output_labels`, per owner home, never shown to the maker); `/view` returns the home's name as
`display_name` (the maker's in `default_name`), `icon` and `favorite`. Favourites are the
one-touch shortcuts on the home screen. A group has `sections`: named parts
(`{name, icon, outputs: [{device_id, key}]}`, outputs of the group's devices only) with a switch
for all of their outputs.

**SMS fallback**: the owner stores the device's SIM number; the hub makes a random 32-byte key
per device and sends `{"type": "config", "sms": {"enabled": true, "key": ..., "device_id": ...}}`
over the device's own connection. The app keeps the key on the phone and writes, with no
internet, `HUB <counter> relay_1=1 relay_3=0 <sig>`: `counter` grows with every message (Unix
seconds), `sig` = first 8 bytes in hex of HMAC-SHA256(key, "<device_id> <counter> <items>").
The device rejects a wrong signature and a counter not above the last accepted one (no replay,
no tampering); `PUT .../sms {"new_key": true}` replaces a key after a phone is lost
(`NativeDeviceSim.sms` and `backend.api.home.sms_signature` are the reference).

**Product templates**: tag kind `calculated` with a `formula` over the product's other keys
(the hub's formula language: units checked on saving, `integrate(x)`, `runtime(c)`); the hub
runs one calculation engine for all platform devices (`Hub.platform_calcs`), results are
stored like any tag. `commands`: several parameters at once (fixed values or entered by the
buyer).

**Maker** (`backend/api/maker.py`): branding (`…/organizations/{org}/branding[/logo]`; public
`/api/public/brand/{org}[/logo]` for the app opened with `?brand=<org>`), tickets
(`/api/platform/tickets[/{id}[/messages]]`, warranty claims approved / rejected), `GET
/api/platform/trend` (per week: registered, activated, tickets; activated devices offline over
three days), `GET /api/platform/devices.csv` (no owner data), billing
(`…/organizations/{org}/billing`; `PUT …/plan` and `POST /api/platform/invoices/{id}/status`
for platform administrators). Payment itself is outside the platform.
