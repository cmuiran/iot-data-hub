# Backend: architecture, security, API and operations

## 1. Architecture

One process (`hub-api`) runs everything:

```
 field devices                     hub-api process
 ─────────────                     ───────────────────────────────────────────────────────
 Modbus TCP ──► ModbusTcpDriver ─┐
 MQTT/JSON  ──► MqttJsonDriver  ─┼─► Hub ─┬─► latest cache ──────────► REST /api/... (FastAPI)
 OPC UA     ──► OpcUaDriver     ─┘        ├─► LiveBus ────────────────► WebSocket /api/ws
                  ▲                       ├─► AlarmEngine ──► alarm_events
                  │ WriteGrant            └─► batched writer ──► PostgreSQL (+TimescaleDB)
                  └──────────── ControlService (backend/control.py) ◄── POST /api/control/write
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
.venv\Scripts\hub-api.exe --config-dir .sim/config --dev --embedded-db .sim/db
.venv\Scripts\hub-admin.exe --embedded-db .sim/db create-admin --username boss
```

(Run `hub-admin` before `hub-api`, or point it at the running database with `HUB_DATABASE_URL`;
two processes must not both start the embedded database.) API docs: http://127.0.0.1:8000/api/docs

### Commissioning a device

`hub --config-dir config` prints every tag of every device every 5 s (no API, no login). Compare
with the device display: wrong base address or word order shows up immediately.

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

