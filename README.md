# Industrial IoT Data Hub

Collects data from field devices over **Modbus TCP/RTU**, **MQTT/JSON**, **OPC UA**, **Siemens S7**,
**HTTP/REST** and **SNMP**, normalizes it
into one data model, stores history in PostgreSQL (+ TimescaleDB), raises alarms, and serves a
REST + WebSocket API with user management, RBAC and a control-safety layer for writes, and a
Persian/English web dashboard. Project rules: [CLAUDE.md](CLAUDE.md). Backend details, API
reference and operations: [docs/backend.md](docs/backend.md); dashboard: [docs/frontend.md](docs/frontend.md).

## Status

| Area | Contents | State |
|---|---|---|
| Devices | Modbus TCP, Modbus RTU (serial, RFC 2217, RTU over TCP; bit fields, BCD), MQTT/JSON (TLS with plant CA and client certificates), OPC UA client (incl. Basic256Sha256 certificates), Siemens S7 (S7comm), HTTP/REST JSON, SNMP v2c/v3; value labels | done |
| Data | live cache, PostgreSQL/TimescaleDB, deadband/heartbeat filter, hourly roll-up, retention, daily backups | done |
| Alarms | limits, offline, ack, shelving, notifications (e-mail, Telegram, SMS gateway, webhook) | done |
| Users | login, lockout, IP throttling, rotating refresh tokens, sessions, RBAC by site/area, audit | done |
| Control | 8-gate safety layer, two-person confirmation, read-back, scheduled sequences | done |
| API | REST + WebSocket, config changes without restart, reports (Excel/CSV), dashboards storage | done |
| Integration | OPC UA server and Modbus TCP server for SCADA (writes via the safety layer), northbound MQTT to a central broker, Prometheus metrics | done |
| Operations | Windows service (WinSW), rotating logs with redaction, active/standby HA | done |
| Security hardening | HTTPS, MFA (TOTP), idle timeout, password history/expiry, network allowlist, rate limit, WebSocket origin check, encrypted backups, tamper-evident audit, certificate expiry alarms | done |
| Web dashboard (fa/en, RTL) | live overview, devices, alarms (horn, notifications), trends, reports, control with two-person confirmation, schedules, dashboard builder, users, configuration, audit, system; Jalali calendar; light/dark; phone layout | done |

Not verified yet: real field hardware, TimescaleDB-specific parts (tests skip without it), the
Windows service installation itself (needs an elevated shell and WinSW), real SMTP/Telegram/SMS
providers (tested against local servers).

## Quick start (no hardware, no database install)

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
```

Run the whole system against simulated devices (two terminals):

```
.venv\Scripts\hub-sim.exe --config-dir config --out .sim
.venv\Scripts\hub-admin.exe --embedded-db .sim/db create-admin --username boss
.venv\Scripts\hub-api.exe --config-dir .sim/config --env-file .sim/sim.env --dev --embedded-db .sim/db
```

Then open http://127.0.0.1:8000/ and sign in (the API itself: http://127.0.0.1:8000/api/docs).

## Commands

| Command | Purpose |
|---|---|
| `hub-api` | the backend: drivers, storage, alarms, control, HTTP/WebSocket API |
| `hub-admin` | `migrate`, `create-admin`, `reset-password`, `unlock`, `list-users`, `backup`, `decrypt-backup`, `make-cert`, `make-tls-cert`, `reset-mfa`, `verify-audit`, `service-config` |
| `hub` | collector only: prints live values (commissioning), optional `--database-url` |
| `hub-sim` | simulates every device of a config folder on 127.0.0.1 |
| `python -m hub.validate_config` | checks `config/` (devices, brokers, alarms, control) |

## Layout

```
config/          brokers.yaml, devices/*.yaml, alarms.yaml, control.yaml, notifications.yaml
hub/             models, config loader, codec, drivers, alarm engine, runtime, storage
backend/         FastAPI app, auth, RBAC, control-safety layer, server/admin CLIs
frontend/        web dashboard (served by hub-api at /): index.html, css/, js/, i18n/fa.json + en.json
simulators/      Modbus TCP, MQTT broker + devices, OPC UA, S7 PLC, HTTP/JSON, SNMP agent; hub-sim
db/migrations/   0001_core, 0002_timescale (optional), 0003_operations, 0004_security, 0005_protocols
scripts/windows/ install-service.ps1 / uninstall-service.ps1 (WinSW)
docs/            backend.md, frontend.md, device_catalog.xlsx
tests/           unit, drivers, storage, api, e2e, frontend (600+ tests, no hardware needed)
.claude/skills/  add-device: onboard devices from datasheets, payloads, Excel
```

## Adding a device

Ask Claude Code in this folder ("add an energy meter at 192.168.10.31 unit 1, here is the
register list…"), or fill a copy of `.claude/skills/add-device/templates/devices_template.xlsx`:

```
.venv\Scripts\python.exe .claude\skills\add-device\scripts\excel_to_devices.py devices.xlsx --dry-run
.venv\Scripts\python.exe .claude\skills\add-device\scripts\excel_to_devices.py devices.xlsx --check
```

Restart `hub-api` after changing `config/`.

## Safety defaults

* Every tag is read-only unless `access: read_write` with `min`/`max`.
* Writes are refused unless `HUB_CONTROL_ENABLED=true` **and** the user has `tag.write` for the
  tag's site/area **and** every check in `config/control.yaml` passes. Every attempt is audited.
* No secrets in config: credentials are environment variable names (`.env`, see `.env.example`).
