# Backend: architecture, security, API and operations

## 1. Architecture

One process (`hub-api`) runs everything:

```
 field devices                     hub-api process
 ─────────────                     ───────────────────────────────────────────────────────
 Modbus TCP/RTU ► ModbusTcpDriver ┐                  ┌─► REST /api/... + dashboard (/)
 MQTT/JSON  ──► MqttJsonDriver    ├─► Hub ─┬─► latest ┼─► Modbus TCP server (modbus_server.yaml)
 OPC UA     ──► OpcUaDriver       │        │   cache   └─► OPC UA server (HUB_OPCUA_SERVER)
 Siemens S7 ──► S7Driver          │        ├─► LiveBus ──► WebSocket /api/ws
 HTTP/REST  ──► HttpJsonDriver    │        │          └──► northbound MQTT (northbound.yaml)
 SNMP       ──► SnmpDriver       ─┘        ├─► AlarmEngine ──► alarm_events
                  ▲                        └─► batched writer ──► PostgreSQL (+TimescaleDB)
                  │ WriteGrant
                  └── ControlService (backend/control.py) ◄── dashboard, schedules,
                                                              OPC UA / Modbus server writes
```

* **Drivers** (`hub/drivers/`) own one device each, reconnect with exponential backoff and never
  block one another. Devices behind the same Modbus gateway share one serialized connection.
* **Hub** (`hub/runtime.py`) keeps the latest value per tag in memory, publishes live events and
  buffers samples for the database (bounded; batches of 500 every second). A database outage
  does not stop collection: samples wait in the buffer and are written when it is back.
* **Storage** (`hub/storage/`): PostgreSQL only. `migrate.py` applies `db/migrations/` in order;
  `0002_timescale.sql` (hypertable, compression, retention, hourly aggregates) is applied only when
  the TimescaleDB extension is installed (`HUB_DB_TIMESCALE=auto`).
* **Config** stays in YAML (`config/`), validated at start. Devices and tags are mirrored into
  the database (`devices`, `tags`); removed ones are retired, never deleted. Changing config
  needs a restart of `hub-api`.

## 2. Security model

| Topic | Behaviour |
|---|---|
| Passwords | argon2id; policy: ≥ `HUB_PASSWORD_MIN_LENGTH` (10), not containing the username, ≥ 4 distinct characters |
| Lockout | `HUB_LOGIN_MAX_FAILURES` (5) wrong passwords → locked `HUB_LOCKOUT_MINUTES` (15). Unknown user, disabled user and wrong password give the same answer |
| Access token | JWT HS256, `HUB_ACCESS_TOKEN_MINUTES` (15), bound to a session; every request checks the session and the user, so logout/disable/password change act immediately |
| Refresh token | random 48 bytes, stored only as SHA-256, `HUB_REFRESH_TOKEN_HOURS` (12), rotated on every use. Re-use of an old one revokes **all** sessions of the user |
| New users | must change their password at first login (API returns `password_change_required` until then) |
| RBAC | roles `admin`, `engineer`, `operator`, `viewer`, each granted for everywhere / a site / a site+area. Default deny. `user.manage`, `config.edit`, `audit.read` need an unscoped grant. The last active global admin cannot be disabled or demoted |
| Data scope | lists, details, history, alarms and the WebSocket only show tags/devices inside the user's `data.read` scopes; out-of-scope items answer 404 |
| Audit | `write_audit` (every write attempt: `requested` + one outcome) and `auth_audit` (logins, lockouts, user/role changes, confirmations, alarm acks) are append-only (triggers refuse UPDATE/DELETE/TRUNCATE) |
| Transport | `hub-api` listens on 127.0.0.1 by default. For the network use HTTPS: `HUB_TLS_CERT`/`HUB_TLS_KEY` (section 15) or a TLS reverse proxy |
| MFA | TOTP (authenticator apps) with one-time recovery codes; required for the roles in `HUB_MFA_REQUIRED_ROLES` (section 15) |
| Idle sessions | a session unused for `HUB_SESSION_IDLE_MINUTES` (30) ends |

Permissions per role:

| Permission | viewer | operator | engineer | admin |
|---|:-:|:-:|:-:|:-:|
| data.read | ✔ | ✔ | ✔ | ✔ |
| tag.write | | ✔ | ✔ | ✔ |
| alarm.ack (also shelving) | | ✔ | ✔ | ✔ |
| write.confirm | | | ✔ | ✔ |
| config.edit, audit.read | | | ✔ | ✔ |
| user.manage | | | | ✔ |

## 3. Control (writes to devices)

A write passes these gates in order; the first failure is the result (all are audited):

| # | Gate | Result code |
|---|---|---|
| 1 | tag exists | 404 |
| 2 | `tag.write` for the tag's site/area | `rejected_rbac` (no tag.write anywhere: 403, not audited) |
| 3 | `HUB_CONTROL_ENABLED=true`, device enabled, tag `access: read_write` | `rejected_disabled` |
| 4 | type (bool vs number, strict JSON types), tag `min`/`max`, raw type range | `rejected_range` |
| 5 | `max_step` vs the current good value | `rejected_range` |
| 6 | `min_interval_s` since the previous write | `rejected_rate` |
| 7 | interlocks: referenced tag has a good value younger than `max_age_s` that satisfies `op value` | `rejected_interlock` |
| 8 | `confirm: true` → waits for a second user with `write.confirm` (not the requester), who re-runs 3–7; expires after `HUB_PENDING_WRITE_MINUTES` | `pending_confirmation` → … / `rejected_unconfirmed` |

Then: previous value is read, the value is written, the value is read back (Modbus/OPC UA read
again; MQTT waits for the next telemetry message) and compared → `applied`, `readback_mismatch`
(status `unverified`), `failed` or `timeout` (state unknown). Policy lives in `config/control.yaml`.

## 4. API reference

All bodies are JSON. Errors: `{"error": "<code>", "message": "..."}` (codes are stable, for UI
translation). Interactive docs: `/api/docs` (OpenAPI at `/api/openapi.json`).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | /api/health | — | status, database, device counts |
| POST | /api/auth/login | — | `{username, password}` → access + refresh token |
| POST | /api/auth/refresh | — | `{refresh_token}` → new pair (rotation) |
| POST | /api/auth/logout | logged in | ends the session |
| GET | /api/auth/me | logged in | user, roles, effective permissions with scopes |
| POST | /api/auth/change-password | logged in | `{current_password, new_password}`; ends all sessions |
| GET | /api/devices | data.read | devices + connection state (filter `site`, `area`) |
| GET | /api/devices/{id} | data.read | device with tags and live values |
| GET | /api/tags | data.read | tags with live values (filter `site`, `area`, `device_id`, `tag`) |
| GET | /api/tags/{path}/latest | data.read | latest value |
| GET | /api/tags/{path}/history | data.read | `start`, `end` (UTC offset required), `max_points` → raw or bucketed (avg/min/max) |
| GET | /api/tags/{path}/history.csv | data.read | CSV export |
| GET | /api/alarms | data.read | active alarms (uncleared or unacknowledged) |
| GET | /api/alarms/history | data.read | `since`, `limit` |
| POST | /api/alarms/{id}/ack | alarm.ack | `{comment}` |
| POST | /api/control/write | tag.write | `{tag, value, reason}` → WriteResult |
| GET | /api/control/pending | logged in | writes waiting for confirmation (own, or confirmable) |
| POST | /api/control/pending/{id}/confirm | write.confirm | second person confirms |
| POST | /api/control/pending/{id}/reject | write.confirm / requester | `{reason}` |
| GET | /api/control/policy/{path} | tag.write | effective policy for a tag |
| GET/POST | /api/users | user.manage | list / create `{username, display_name, password, language, roles}` |
| GET/PATCH | /api/users/{id} | user.manage | details / `{display_name, is_active, language, must_change_password}` |
| POST | /api/users/{id}/password | user.manage | admin reset (user must change it) |
| POST | /api/users/{id}/unlock | user.manage | clear lockout |
| POST/DELETE | /api/users/{id}/roles[/{grant}] | user.manage | grant `{role, site, area}` / revoke |
| GET | /api/roles | user.manage | roles and their permissions |
| GET | /api/audit/writes | audit.read | `tag`, `user_id`, `request_id`, `since`, `limit` |
| GET | /api/audit/auth | audit.read | `event`, `limit` |
| GET | /api/system/info | signed in | version, node, HA role, time zone, control on/off, idle minutes, password length (for the dashboard) |
| GET | /api/versions | — | API versions served (`current`, `supported`, `base` = `/api/v1`) and the hub version |
| GET | /api/system/health-checks | config.edit | results of the hub's own health checks (`?run=true` checks now) |
| GET | /api/system/outputs | config.edit | northbound MQTT targets (connected, published, queued, dropped), Modbus server and OPC UA server status |
| GET | /api/system/stats | config.edit | samples received/written/buffered/dropped, device states |
| GET | /api/system/config | config.edit | the running configuration (secrets are env var names) |
| GET | /api/system/events, /api/system/nodes | config.edit | backups, retention, failovers; HA nodes |
| POST | /api/system/backup | config.edit | backup now (needs HUB_BACKUP_DIR) |
| any | /api/config/... | config.edit | devices, alarm rules, control policy, reload (section 7) |
| POST, DELETE, GET | /api/alarms/shelve, /api/alarms/shelve/{source}, /api/alarms/shelves | alarm.ack / data.read | shelving (section 8) |
| GET, POST | /api/notifications/log, /api/notifications/test/{channel} | audit.read / config.edit | notifications (section 8) |
| POST | /api/reports, /api/reports/export | data.read | period reports, Excel/CSV (section 9) |
| any | /api/schedules, /api/schedules/{id}, .../run, .../runs | tag.write | scheduled commands (section 10) |
| GET, DELETE | /api/auth/sessions, /api/users/{id}/sessions | logged in / user.manage | sessions (section 11) |
| any | /api/dashboards, /api/dashboards/{id} | data.read | dashboard layouts (section 11) |
| GET | /metrics | metrics token | Prometheus metrics (section 13) |
| WS | /api/ws | data.read | live stream, see below |

**WebSocket** `/api/ws`: send `{"type":"auth","token":"<access token>"}` first (the token never
goes in the URL). The server answers `snapshot` (tags, devices, active alarms) and then streams
`samples`, `status`, `alarm` and `write` events filtered to the user's scope. Optional
`{"type":"subscribe","tags":[...]}` narrows the stream; `{"type":"ping"}` → `pong`. Close codes:
4401 not authenticated / session ended (checked every 60 s), 4403 no data.read, 1013 too slow.

## 5. Operations

### Install (production, Windows)

1. Python 3.12, then in the project folder: `py -3.12 -m venv .venv` and
   `.venv\Scripts\python.exe -m pip install -e .`
2. PostgreSQL 17 (EnterpriseDB installer) + TimescaleDB for PostgreSQL 17 (run its `setup.exe`
   as administrator, restart the PostgreSQL service). Create a database and a login role:
   `CREATE ROLE hub_app LOGIN PASSWORD '...'; CREATE DATABASE hub OWNER hub_app;`
3. Copy `.env.example` to `.env`; set `HUB_DATABASE_URL`, a long random `HUB_JWT_SECRET`, device
   credentials. Leave `HUB_CONTROL_ENABLED=false` until commissioning is done.
4. `.venv\Scripts\hub-admin.exe create-admin --username <name>` (applies migrations too).
5. `.venv\Scripts\python.exe -m hub.validate_config`, then `.venv\Scripts\hub-api.exe`.
6. Run it as a Windows service with a service wrapper (e.g. NSSM or WinSW) pointing at
   `hub-api.exe` with the project folder as working directory; set it to restart on failure.
7. Revoke UPDATE/DELETE/TRUNCATE on `write_audit` and `auth_audit` from `hub_app` if the schema is
   owned by another role (see the comment at the end of `0001_core.sql`).

### Development without hardware

```
.venv\Scripts\hub-sim.exe --config-dir config --out .sim
.venv\Scripts\hub-api.exe --config-dir .sim/config --env-file .sim/sim.env --dev --embedded-db .sim/db
.venv\Scripts\hub-admin.exe --embedded-db .sim/db create-admin --username boss
```

(Run `hub-admin` before `hub-api`, or point it at the running database with `HUB_DATABASE_URL`;
two processes must not both start the embedded database.) API docs: http://127.0.0.1:8000/api/docs

### Commissioning a device

`hub --config-dir config` prints every tag of every device every 5 s (no API, no login). Compare
with the device display: wrong base address or word order shows up immediately.

The full procedure (and `hub-commission`) is in section 25 and, in Persian, docs/fa/commissioning.md.

### Backups and retention

Back up the database with `pg_dump` (daily) and keep `config/` in git. Raw samples are kept 90
days and hourly aggregates 5 years when TimescaleDB is installed (edit `0002_timescale.sql` before
the first start, or change the policies with `SELECT remove_retention_policy(...)` / `add_...`).

### Troubleshooting

| Symptom | Check |
|---|---|
| `/api/health` says `degraded` | database unreachable; samples are buffered (`/api/system/stats` → `buffered`) |
| device `offline` | `last_error` in `/api/devices`; network, port, unit id; `hub` console |
| device `error` | it answers but some reads fail (Modbus exception): addresses in the YAML |
| writes `rejected_disabled` | `HUB_CONTROL_ENABLED`, device `enabled`, tag `access: read_write` |
| writes `unverified` | the device accepted the write but reports another value: scaling or word order |
| login `account_locked` | wait, or `hub-admin unlock <user>` / `POST /api/users/{id}/unlock` |

## 6. Data volume, housekeeping and backups

* **History filter** (`hub/history.py`): the live cache and WebSocket get every sample; the
  database gets a sample only when the value changed (beyond the tag's `history_deadband`), the
  quality changed, or the device's `history_heartbeat_s` (default 300 s) passed. `history: false`
  keeps a tag live-only. `/api/system/stats` shows `samples_filtered`.
* **Hourly roll-up** (`hub/maintenance.py`, every 5 min): `samples_hourly` holds avg/min/max/
  first/last/n per tag and hour. Hours follow `HUB_TIMEZONE` (Tehran hours start at :30 UTC), so
  daily reports match the local calendar. History requests with buckets of an hour or more read
  the roll-up plus raw data for the newest hours.
* **Retention** (hourly): raw samples older than `HUB_RETENTION_RAW_DAYS` (365) and hourly rows
  older than `HUB_RETENTION_HOURLY_DAYS` (3650) are removed; raw data is never removed before it
  was rolled up. TimescaleDB drops whole chunks, plain PostgreSQL deletes in batches.
* **Backups**: with `HUB_BACKUP_DIR` set, a `pg_dump --format=custom` runs daily at
  `HUB_BACKUP_TIME` (local); the newest `HUB_BACKUP_KEEP` files stay. On demand:
  `hub-admin backup --out backups` or `POST /api/system/backup`. Results: `GET /api/system/events`.
  Restore into an empty database: `pg_restore --clean --if-exists --dbname <url> <file>.dump`.

## 7. Configuration from the API (no restart)

`/api/config/...` (permission `config.edit`) changes devices, alarm rules and control policy.
Every change is validated on a scratch copy of the whole config directory first (the same
loader, cross-device checks included); only a change that adds no error is written (atomically)
and applied with a live reload; only the changed drivers restart. Deleted device files go to
`config/.trash/`. Every change is audited (`config_changed`). After editing files by hand:
`POST /api/config/reload`. API-written files are regenerated, so comments in them are lost.

| Method | Path | Purpose |
|---|---|---|
| POST | /api/config/devices/validate | dry run: schema errors and the issues a device would cause |
| POST / PUT / PATCH / DELETE | /api/config/devices[/{id}] | create, replace, `{enabled}`, delete |
| GET / PUT / DELETE | /api/config/alarms, /api/config/alarms/rules[/{tag}] | alarm rules |
| GET / PUT / DELETE | /api/config/control, /api/config/control/defaults, /api/config/control/tags/{tag} | control policy |
| POST | /api/config/reload | re-read config/ after manual edits |

## 8. Alarms: shelving and notifications

* **Shelving** (`alarm.ack` for the area, max 24 h, reason required): `POST /api/alarms/shelve`
  `{source, minutes, reason}`, `DELETE /api/alarms/shelve/{source}`, `GET /api/alarms/shelves`.
  Shelved alarms are still recorded and marked in `GET /api/alarms` (`shelved`), but send no
  notifications.
* **Notifications** (`config/notifications.yaml`, see `config/notifications.example.yaml`):
  channels `email` (SMTP STARTTLS/SSL), `telegram` (bot), `sms` (any HTTP gateway: URL/params
  templates with `{message}`, `{recipient}`, `{api_key}`) and `webhook` (JSON, optional
  HMAC-SHA256 signature). Rules choose channels by `min_severity`, `events` (raised/cleared),
  `sites`, `areas`, `include_offline`. Messages are Persian or English (`language`). Up to 3
  attempts, `max_per_minute` per channel; every attempt is in `GET /api/notifications/log`
  (audit.read). Test a channel: `POST /api/notifications/test/{channel}` (config.edit).

## 9. Reports

`POST /api/reports` (JSON) and `POST /api/reports/export?format=xlsx` (or `csv`) with
`{title, start, end, interval: hour|day|month, columns: [{tag, function, label}]}`. Functions:
`avg` (sample-weighted), `min`, `max`, `last`, `delta` (consumption of a counter, hour to hour; a
counter reset never gives a negative value). Days and months start at local midnight
(`HUB_TIMEZONE`). Each tag needs `data.read`; up to 50 columns, 3 years, 10 000 rows.

## 10. Scheduled commands

`/api/schedules` (tag.write): `{name, cron: "m h dom mon dow", timezone, enabled, steps}` with
steps `{"write": {tag, value}}`, `{"delay_s": n}`, `{"wait": {tag, op, value, timeout_s}}`.
Writes run through the control-safety layer as the schedule's owner (RBAC checked again at every
run, reason `schedule <name>` in the audit); the first failing step stops the run. Tags whose
policy needs a second person cannot be scheduled. `POST /api/schedules/{id}/run` runs now,
`GET /api/schedules/{id}/runs` shows each run step by step. Missed runs are not caught up.

## 11. Users: sessions, throttling, dashboards

* `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`: own sessions (address, browser,
  current one marked). Admin: `GET` / `DELETE /api/users/{id}/sessions` (log out everywhere).
* More than `HUB_LOGIN_IP_MAX_FAILURES` failed logins from one address within
  `HUB_LOGIN_IP_WINDOW_S` give HTTP 429 with `Retry-After` (in memory, per node).
* `/api/dashboards`: saved layouts (JSON owned by the frontend, at most 256 KB), private or
  `shared`, one default per user; only the owner edits.

## 12. OPC UA server for SCADA

`HUB_OPCUA_SERVER=true` publishes every tag at `opc.tcp://<host>:4840/hub/`, namespace
`urn:iot-data-hub`, NodeId `ns=<n>;s=<tag path>`, tree `Objects/Hub/<site>/<area>/<device>/`,
with quality, source timestamp, `Unit` / `Description` properties and a `Status` per device.
Endpoints are Basic256Sha256 Sign / SignAndEncrypt (None only with
`HUB_OPCUA_SERVER_ALLOW_INSECURE=true`); the server certificate is created in `HUB_PKI_DIR`.
Put client certificates into `<pki>/trusted/` to accept only those clients. Users are hub
accounts with an unscoped `data.read` grant (use a dedicated SCADA account). Writes are off
unless `HUB_OPCUA_SERVER_WRITES=true`; then they go through the control-safety layer as that user
and the result comes back as the OPC UA status code (BadOutOfRange, BadInvalidState for
interlocks, ...).

Certificates for the hub's own OPC UA *client* (PLC connections with security):
`hub-admin make-cert --role client --name hub-client --out pki`, then point the device's
`client_certificate_env` / `client_private_key_env` variables at the two files and trust
`pki/hub-client.der` in the PLC.

## 13. Monitoring, logging, Windows service

* `GET /metrics` (Prometheus text): enabled by `HUB_METRICS_TOKEN`, scraped with
  `Authorization: Bearer <token>`. Samples, storage buffer/errors, device up/state, active alarms
  by severity, control writes by result, logins, notifications, OPC UA server writes, HA role.
* `HUB_LOG_FILE`: rotating log file (`HUB_LOG_MAX_MB`, `HUB_LOG_BACKUPS`). Passwords, tokens and
  database passwords are redacted from every log line.
* Windows service: download WinSW-x64.exe as `scripts\windows\hub-api-service.exe`, then in an
  elevated PowerShell run `scripts\windows\install-service.ps1` (validates the config, writes the
  service definition with `hub-admin service-config`, installs and starts the service, which
  restarts on failure). `uninstall-service.ps1` removes it.

## 14. High availability (two nodes)

`HUB_HA=on` on two servers with the same `HUB_DATABASE_URL`, `HUB_JWT_SECRET` and config
directory, and different `HUB_NODE_ID`. The node holding a PostgreSQL advisory lock is
**active** (drivers, control, schedules, notifications, housekeeping, OPC UA server); the other is
**standby** and answers 503 on every API route, including `/api/health`, so a load balancer or
reverse proxy (health check `GET /api/health`, expect 200) sends traffic to the active node only.
If the active node loses its database connection it stops all device traffic immediately and the
standby takes over within a few seconds; there are never two active nodes. Roles and heartbeats:
`GET /api/system/nodes`; failovers: `GET /api/system/events?kind=failover`. Devices that allow
only one Modbus TCP connection are only ever polled by the active node.

## 15. Security hardening

| Feature | Setting | Notes |
|---|---|---|
| HTTPS | `HUB_TLS_CERT`, `HUB_TLS_KEY` | `hub-admin make-tls-cert --name hub.kish.local --name 192.168.10.5` makes a self-signed certificate (import the `.crt.pem` into the operator PCs' trusted roots), or use one from the company CA. HSTS is sent when TLS is on. Without TLS on a non-local address the server logs a warning |
| Two-factor login | `HUB_DATA_KEY`, `HUB_MFA_REQUIRED_ROLES` | `POST /api/auth/mfa/setup` (QR from `otpauth_uri`) → `POST /api/auth/mfa/enable {code}` (returns 10 one-time recovery codes, shown once). Login then answers `{mfa_required, mfa_token}`; finish with `POST /api/auth/mfa/verify {mfa_token, code}` or `{recovery_code}`. Codes cannot be replayed; wrong codes count towards the lockout. Users of a required role without MFA can only reach the MFA routes (`mfa_enrollment_required`). Lost phone: `POST /api/users/{id}/mfa/reset` or `hub-admin reset-mfa <user>` |
| Idle timeout | `HUB_SESSION_IDLE_MINUTES` (30) | checked on every request and refresh; an open live dashboard keeps its session active |
| Password lifecycle | `HUB_PASSWORD_HISTORY` (3), `HUB_PASSWORD_MAX_AGE_DAYS` (0 = off) | reused passwords are refused (`password_reused`); expired ones force a change at login |
| Network allowlist | `HUB_ALLOWED_NETWORKS` | CIDR list for the API, WebSocket and /metrics (health stays open) |
| Rate limit | `HUB_RATE_LIMIT_PER_MIN` (600) | per client address, 429 + Retry-After; behind a proxy configure uvicorn's `--forwarded-allow-ips` |
| WebSocket origin | `HUB_ALLOWED_ORIGINS` | browsers may open `/api/ws` only from the hub's own host or listed origins |
| Encrypted backups | `HUB_DATA_KEY` | backups become `hub-*.dump.enc` (AES-256-GCM, chunked, tamper-evident); `hub-admin decrypt-backup in out` before `pg_restore` |
| Tamper-evident audit | – | both audit tables form a SHA-256 hash chain (migration 0004). `GET /api/audit/verify`, `hub-admin verify-audit`; the security watch verifies daily, stores the chain heads (`system_events` `audit_anchor`) and raises the critical alarm `system:audit` when the chain is broken |
| Certificate expiry | `HUB_CERT_WARN_DAYS` (30) | HTTPS, OPC UA and device client certificates are checked daily; system alarm `system:cert:<file>` (high, critical when expired), cleared after renewal. `GET /api/system/certificates` |

**Keep `HUB_DATA_KEY` safe.** It is not stored in the database. Losing it makes encrypted
backups unreadable and every MFA enrolment unusable (reset MFA, users enrol again).

What the code cannot do: Modbus has no security at all; keep field devices in a separate OT
network (VLAN + firewall) that only the hub can reach. A database superuser can still rewrite the
whole audit chain; the daily anchors in `system_events` and the log file are what reveal it, so
ship the log file to a separate machine.

## 16. More field protocols (phase 3)

| Protocol | Driver | Typical devices | Writes |
|---|---|---|---|
| `s7` | `hub/drivers/s7.py` (python-snap7, asyncio) | S7-300/400, S7-1200/1500 without OPC UA | yes, via the safety layer |
| `http` | `hub/drivers/http_json.py` (httpx) | inverters, gateways, weather stations, IoT devices with a JSON API | no |
| `snmp` | `hub/drivers/snmp.py` (pysnmp, v2c / v3) | UPS, PDUs, switches, environment monitors | no |

**Siemens S7.** Absolute addresses (`DB1.DBD0`, `DB1.DBX6.2`, `MW20`, `IW64`, `Q0.0`; `E`/`A`
accepted). Values are grouped into byte blocks per DB/area, so a DB with 50 values is one
request; an address the PLC refuses is read tag by tag from then on. A bool is written with the
protocol's single-bit transport, so the other bits of that byte (which the PLC program may be
changing) are never overwritten. In the PLC: allow PUT/GET (S7-1200/1500: Protection & Security
→ Connection mechanisms) and switch off "optimized block access" for the DBs the hub reads;
S7-300 is usually rack 0 slot 2, S7-1200/1500 rack 0 slot 1.

**HTTP/REST.** `base_url` + per-tag `path` and `json_path`; each distinct path is requested once
per poll. Authentication from env vars (basic, bearer, API-key header); TLS verified against the
system CAs or `ca_file_env`. Network errors → offline; HTTP error status or non-JSON → that
path's values bad, state `error`.

**SNMP.** Numeric OIDs (no MIB files on the hub). SNMPv3 with `sha256` + `aes128` is the
recommended setting; v2c sends the community in clear text (the validator warns). A wrong
community gives no answer (offline), a wrong v3 key fails the digest check (offline with that
message), a missing OID marks only that tag bad.

**Value labels (all protocols).** `states: {0: stopped, 1: running}` on an integer or bool tag:
the dashboard shows the label, history and reports keep the number.

**Modbus extras.** `bit` + `bit_count` reads a field of several bits (a mode in bits 4–6) as a
read-only integer; `encoding: bcd` reads/writes packed decimal digits (older meters and drives).
A register that cannot be decoded (not BCD) makes only that tag bad; the device stays online.

## 17. MQTT over TLS

`brokers.yaml`: `tls: true` always verifies the broker certificate and host name (TLS 1.2+),
against the system CAs or the plant CA in `ca_file_env`. `client_certificate_env` +
`client_private_key_env` add a client certificate (mutual TLS, required by hardened brokers);
`tls_server_name` when the certificate name differs from `host` (e.g. connecting by IP). The
`*_env` variables hold file **paths**; certificates and keys stay out of the config. Used by the
MQTT driver and the northbound publisher.

## 18. Northbound MQTT (publish to another broker)

`config/northbound.yaml` (example: `northbound.example.yaml`): per target a broker from
`brokers.yaml`, a `topic_prefix` and tag patterns. Messages `<prefix>/<site>/<area>/<device>/<tag>`
→ `{"v", "ts", "q", "u"}` on change (value or quality) and every `heartbeat_s`, at most one per
`min_interval_s`; `<prefix>/status` (retained) `online` / `offline` (on a clean stop, and from the
broker as last will when the hub disappears). While the broker is unreachable messages queue
(`queue_max`, oldest dropped and counted) and are sent in order afterwards. Only the active HA
node publishes. Output only: nothing received there reaches a device. Status:
`GET /api/system/outputs`, System page, metrics `hub_northbound_*`.

## 19. Modbus TCP server (for SCADA / HMI / PLCs)

`config/modbus_server.yaml` (example: `modbus_server.example.yaml`) maps tags to input/holding
registers, coils and discrete inputs (`data_type` and `scale` per point; `source: quality` puts
the quality code 0/1/2 in a register). Registers are encoded from the latest values on every
request. Modbus has no authentication, so:

* readers can be limited with `allowed_clients` (the validator warns when the server listens on
  the network without it);
* writes need `writable: true` points **and** a `writes` section: only `writes.allowed_clients`
  may write, a multi-register value is written whole, and every write goes through
  `ControlService.request_write` as the hub user `writes.as_user` (give it the operator role for
  the area, nothing more). Answers: applied → normal response; waiting for a second person →
  exception 05; refused → exception 04; not writable → exception 02; client not allowed →
  exception 01.

Status: `GET /api/system/outputs`, System page, metric `hub_modbus_server_events_total`.

## 20. The hub watches itself (phase 4)

`backend/health_watch.py` runs every `HUB_HEALTH_INTERVAL_S` (30 s) on the active node. Each
problem becomes a system alarm, so it appears in the alarm list and reaches people through the
notification rules (put a rule with `min_severity: high` on an e-mail or Telegram channel):

| Check | Alarm | Raised when | Severity |
|---|---|---|---|
| database | `system:database` | 3 checks in a row without an answer | critical |
| disk | `system:disk:<volume>` | free space below `HUB_DISK_WARN_PERCENT` (10) / `HUB_DISK_CRITICAL_PERCENT` (5) on the volumes of config, logs, backups and the working folder | high / critical |
| backup | `system:backup` | the last backup failed, or none for `HUB_BACKUP_MAX_AGE_HOURS` (26) | high |
| restore | `system:restore` | the last restore test failed (section 22) | critical |
| clock | `system:clock` | the clock differs from the database server, or from `HUB_NTP_SERVER`, by more than `HUB_CLOCK_MAX_SKEW_S` (30) | high |
| storage | `system:storage` | more than half the sample buffer waits for the database (high); samples were lost because it was full (critical) | high / critical |
| loop | `system:loop` | the hub was unresponsive for more than 2 s | medium |

Alarms clear by themselves when the condition is gone. A database outage cannot be stored while
it lasts: the alarm is still sent to the notification channels, and is written to the alarm
history (with start and end) once the database answers again. Results: System page ("Hub
health", with "Check now"), `GET /api/system/health-checks`, metrics `hub_health_check{check}`
(0 ok, 1 warning, 2 critical) and `hub_disk_free_percent{volume}`. Certificate expiry and the
audit chain stay with the daily security watch (section 15).

## 21. Structured logs and SIEM

| Setting | Effect |
|---|---|
| `HUB_LOG_FORMAT=json` | the log file (`HUB_LOG_FILE`) gets one JSON object per line: `ts` (UTC), `level`, `logger`, `msg`, `node`, `pid`, plus structured fields |
| `HUB_SYSLOG=udp://siem:514` or `tcp://siem:601` | every record also goes to the syslog server as RFC 5424 with a JSON body; TCP uses newline framing and reconnects after a cut |
| `HUB_SYSLOG_FACILITY` | `local0` (default) … `local7`, `daemon`, … |

Secrets are redacted in every output. The logger `hub.audit` (syslog MSGID `audit`) carries a
copy of every audit row — sign-ins, administration, alarm actions, configuration changes and each
command with its phase and result — with its fields under `audit`; security events
(`login_failed`, `locked`, `mfa_failed`, `network_denied`, …) are logged as warnings. If an
audit row cannot be stored, an error with the full row is logged instead, so the SIEM still has
it. An unreachable syslog server never stops the hub.

## 22. Backups that are known to restore

`HUB_RESTORE_TEST` checks every new backup:

* `archive` (default): the dump's table of contents is read (`pg_restore --list`) — the file is
  complete, readable (with `HUB_DATA_KEY` when encrypted) and contains the hub's tables;
* `full`: in addition, every `HUB_RESTORE_TEST_DAYS` (7) the backup is restored into a temporary
  database on the same server, the schema version and key tables are compared and **both audit
  hash chains are verified in the copy**; the temporary database is dropped. Needs the CREATEDB
  privilege for the hub's database user;
* `off`.

Results are `restore_ok` / `restore_failed` system events and the `system:restore` alarm. By
hand: `hub-admin verify-backup <file> [--full]`. Restore: stop the service, then
`hub-admin restore-backup <file> --yes` (replaces the whole database; decrypts when needed).

## 23. API versions

`/api/v1/...` is the stable API for integrations (SCADA, reports, scripts): it keeps its routes
and fields across hub upgrades; a future incompatible API becomes `/api/v2` next to it.
`/api/...` (without a version) is an alias of the current version, used by the bundled dashboard.
Every API answer carries the header `API-Version: 1`; an unknown version answers 404
`unsupported_api_version`; `GET /api/versions` lists what this hub serves. The live WebSocket is
`/api/v1/ws` (or `/api/ws`).

## 24. Upgrade and rollback

Migrations only go forward, and a hub refuses to start on a database that a **newer** hub has
migrated (`refusing to start: the database has migration(s) … that this version does not
know`), so an old version can never silently run on a newer schema.

Before an upgrade:

```
.venv\Scripts\hub-admin.exe upgrade-check        # config valid, schema state, tools, backup < 24 h, disk
.venv\Scripts\hub-admin.exe migrate --dry-run    # which migrations the new version would add
```

Upgrade (elevated PowerShell; `-Wheelhouse` for an offline plant network):

```
powershell -ExecutionPolicy Bypass -File scripts\windows\upgrade.ps1 -Source D:\hub-0.6.0 [-Wheelhouse D:\wheels]
```

The script runs `upgrade-check`, writes a backup to `backups\pre-upgrade`, stops the service,
moves the current program files and `.venv` to `rollback\<time>-<version>`, installs the new
version, starts it (it migrates the database and records an `upgrade` event) and waits until
`/api/health` is ok and `/api/versions` reports the new version. On any failure it rolls back
on its own. Site data — `.env`, `config\`, `pki\`, `logs\`, `backups\` — is never touched.

Rollback by hand (e.g. a problem found a day later):

```
powershell -ExecutionPolicy Bypass -File rollback\<time>-<version>\scripts\windows\rollback.ps1 -From rollback\<time>-<version>
```

It restores the old program files and `.venv`, and — only if the new version migrated the
database — the pre-upgrade backup (data recorded since the upgrade is then lost; the script says
so). Remove the `rollback\` copy after some days of normal operation.

## 25. Field commissioning (phase 5)

`hub-commission` (hub/commission.py) is read-only: it reads, browses and subscribes, and contains
no write primitive (a test enforces it). Writes stay with the control-safety layer. Credentials
come from the environment (`--env-file .env`); `--device <id>` takes address, security and
credentials from that device's configuration, otherwise host/endpoint flags describe a device that
is not configured yet. Exit code 0 ok, 1 problem found, 2 usage or configuration error.

| Command | What it shows |
|---|---|
| `read [ids] [--seconds 10] [--out f.md\|f.json]` | runs the real drivers (disabled devices too, so a device is checked before it is enabled): per tag value, quality, updates, min/max, hints (no value, BAD read, implausible float → word order, outside min/max). `.md` is a SAT sheet with a "verified on site" column |
| `modbus-probe --address A \| --ref 40101 [--register input] [--count 4] [--expect V]` | the registers from A-1 (shows a 1-based datasheet) as uint16/int16 and every float32/int32/uint32/float64 in the four byte/word orders, the connection's order marked; `--expect` = the value on the display → matching type, order, address and power-of-ten scale, plus a tag snippet. Modbus exceptions are explained |
| `modbus-scan [--units 1-247]` | which unit ids answer (an exception answer counts; gateway exceptions 10/11 do not) |
| `opcua-browse [--endpoint URL \| --device id] [--node i=85] [--depth 4]` | variables with NodeId, type, value and r/rw; `--yaml` tag suggestions with the hub data types |
| `mqtt-listen [--broker id \| --device id \| --host] [--topic] [--seconds]` | topics, message rate, every JSON field with its `json_path`; `--yaml` per topic |
| `snmp-walk [--device id \| --host --community-env VAR] [--oid]` | OIDs with SNMP type and value (stops at the end of the subtree or a non-advancing agent); well-known system and UPS-MIB OIDs get names |
| `s7-dump --db N --start S --size L [--area DB\|M\|I\|Q] [--expect V]` | hex dump plus INT/WORD/DINT/REAL at every even offset; `--expect` → address such as `DB1.DBD4` |
| `http-get [--url \| --device id] [--path]` | status and every JSON field; with `--device` and no `--path` every endpoint the device's tags use |

`--yaml` suggestions are a starting point (names from the source, no units or limits); they go
through the add-device skill or the configuration page and `python -m hub.validate_config` like
any other change.

`hub-admin notify-test [channel ...]` sends a test message on the channels of
`config/notifications.yaml` without a database and prints `sent` or `FAILED - <reason>` per
channel (the API equivalent is `POST /api/notifications/test/{channel}`).

The field procedure, the remote-write test protocol (with `HUB_CONTROL_ENABLED`), the SAT
checklist, the report template and the training plan are the Persian guides in docs/fa/.

## 26. Productization (phase 6)

### Installation and packaging

| Way | How | When |
|---|---|---|
| Offline Windows bundle | `scripts\windows\build-bundle.ps1` on a machine with internet → `release\iot-data-hub-<version>-windows.zip` (+ `.sha256`): program, `wheelhouse\` (exactly `requirements.lock`, plus pip and setuptools), `config.dist\`, `SHA256SUMS.txt`. Refuses to bundle `.env`, keys and certificates | plant networks without internet (the normal case) |
| First install | `scripts\windows\install.ps1 [-DatabaseUrl ...] [-AdminUser ...] [-Account ...] [-NoService]`: `.venv` offline from the wheelhouse, `config\` from the samples, `.env` with random `HUB_JWT_SECRET`/`HUB_DATA_KEY` (never printed) and ACL Administrators + SYSTEM, `HUB_CONTROL_ENABLED=false`, validate, migrate, first admin, service. Idempotent | every new server |
| Docker (Linux) | `Dockerfile` (python:3.12-slim, locked versions, `pg_dump` client, user `hub` uid 10001, health check) and `docker-compose.yml` (TimescaleDB 2.17 / PostgreSQL 17, database not published, config/pki/backups mounted) | sites above the Windows connection budget, or a Linux preference |
| Upgrade | `upgrade.ps1` (section 24) now installs the locked versions and the program editable | — |

`requirements.lock` (generated by `scripts/lock.py` from the installed runtime dependency tree;
`--check` in CI) is the single list of shipped versions: the bundle, the image, CI and the
dependency audit all use it. After changing dependencies: install, test, `python scripts/lock.py`.

The program finds `db/migrations` and `frontend/` next to the code (editable install), else in
`HUB_HOME`, else in the working directory (`hub/paths.py`).

### CI/CD

`.github/workflows/ci.yml`: **lint** (ruff, bandit, pip-audit on the lock) → **test** on
`windows-latest` and `ubuntu-latest` (locked versions, `validate_config`, the whole suite with
the simulators and pgserver) → on tags `v*` and manual runs: **loadtest** (200 × 20 on Windows,
JSON as artifact), **package** (the offline bundle as artifact) and **docker** (image build +
config check inside). A weekly run repeats the audit and the load test so new advisories show
up without a code change. `python scripts/check.py [--quick] [--offline] [--load]` runs the same
checks locally.

### Security

See docs/security-assessment.md: methods, the 30 automated penetration checks
(`tests/api/test_pentest.py`), findings and fixes, residual risks, scope for the independent test.
`HUB_API_DOCS` (default off in production, on with `--dev`) controls `/api/docs` and
`/api/openapi.json`.

### Load test and capacity

```
hub-loadtest --devices 300 --tags 20 --seconds 180 [--mix modbus=0.7,mqtt=0.2,opcua=0.1]
             [--poll-ms 1000] [--database-url URL | --embedded-db DIR] [--json r.json] [--profile p.prof]
```

It generates the configuration, runs the simulators in two separate processes (Modbus/OPC UA on
the proactor loop, broker + MQTT publishers on the selector loop), and measures the real hub with
one live subscriber (an open dashboard). Checks: ≥ 99 % devices online, no sample dropped or
failed to write, newest value per device younger than 3 poll periods + 1 s, event loop lag p99
≤ 250 ms, write buffer ≤ 5 s of input, history query p95 ≤ 500 ms. Exit code 1 when one fails.

Result on the development laptop (Windows 11, Python 3.12, embedded PostgreSQL 16.2 without
TimescaleDB), 300 devices (210 Modbus TCP each with its own address, 60 MQTT, 30 OPC UA),
6000 tags at 1 s, 180 s measured: 5 856 samples/s received, 5 189/s stored (rest unchanged),
0 lost, buffer max 3 163, newest values ≤ 1.9 s old, loop lag p99 72 ms, history query p95
59 ms, CPU 70 % of one core (max 97 %), 124 MB, database 137 MB after the run. All checks pass.

Two limits found and handled:

* **Database writes**: `write_samples` and device status are one statement per batch with
  arrays (`INSERT ... SELECT FROM unnest(...)`); per-row `executemany` capped the hub near
  2000 samples/s and lost data at 300 devices.
* **Windows socket budget**: the selector loop's `select()` takes at most 512 sockets and the
  whole loop fails beyond. `device_connections()` counts the enabled devices' connections
  (a Modbus gateway host:port counts once; every MQTT, OPC UA, S7, HTTP, SNMP device once;
  COM ports not at all); `validate_config` warns above 320 and refuses above 400 on Windows.

Planning rule: about 5000 samples/s per hub (one core) and at most 400 device connections per
hub on Windows; beyond that poll slower, use gateways, run on Linux (Docker) or split the plant
over two hubs.

