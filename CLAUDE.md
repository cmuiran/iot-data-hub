# CLAUDE.md — Industrial IoT Data Hub

Project-wide rules for anyone (human or agent) working in this repository.

## 1. Purpose

A **Data Hub** for industrial monitoring and control:

- Collect data from heterogeneous field devices: **MQTT/JSON**, **Modbus TCP**, **OPC UA** PLCs
  (more protocols later — drivers are pluggable).
- **Normalize** every reading into one unified data model (`hub/models.py`).
- **Store history** in PostgreSQL + TimescaleDB.
- Expose a **web dashboard** with user management (**RBAC**) and **safe remote control**.

## 2. Tech stack

| Concern        | Choice                                              |
|----------------|-----------------------------------------------------|
| Language       | Python 3.12, `asyncio` throughout (no blocking I/O in the event loop) |
| Modbus TCP     | `pymodbus` (async client)                           |
| MQTT           | `aiomqtt` (preferred) / `paho-mqtt`                 |
| OPC UA         | `asyncua`                                           |
| API / backend  | FastAPI                                             |
| Storage        | PostgreSQL + TimescaleDB (hypertables for history)  |
| Config         | YAML files, validated with Pydantic models          |
| Tests          | pytest (+ pytest-asyncio)                           |

Do not add new runtime dependencies without stating why in the commit message.

## 3. Folder structure

```
docs/              Architecture and operations notes (backend.md), device_catalog.xlsx
config/            brokers.yaml, devices/<id>.yaml, alarms.yaml, control.yaml, notifications.yaml
hub/
  models.py        Schema of record (Pydantic): devices, tags, alarms, control policy, Sample
  config_loader.py Loads + cross-checks config/; validate_config.py is its CLI
  codec.py         Register/byte-order decoding, scaling, coercion (pure, all drivers use it)
  drivers/         base.py (interface, reconnect, WriteGrant) + modbus_tcp, mqtt_json, opcua
  alarms.py        Limit/offline alarm engine
  history.py       Which samples are stored (deadband / heartbeat)
  maintenance.py   Hourly roll-up, retention, pg_dump backups
  pki.py           OPC UA certificates and trust lists
  runtime.py       Hub: drivers -> latest cache, live bus, alarms, batched storage writer
  storage/         migrate.py (db/migrations), postgres.py (the only store), records.py
  main.py          `hub`: collector-only console (commissioning)
backend/
  app.py, api/     FastAPI routes (REST + /api/ws); errors are {"error": code, "message"}
  auth.py          Login, lockout, rotating refresh tokens; security.py (argon2, JWT)
  rbac.py          Roles x site/area scopes, default deny
  control.py       Control-safety layer: the ONLY caller of Driver.write_tag
  config_admin.py  Validated config changes from the API + live reload
  notifications.py e-mail / Telegram / SMS / webhook; schedules.py + cron.py: scheduled writes
  reports.py       Period reports (Excel/CSV); opcua_server.py: OPC UA server output
  ha.py            Active/standby via a PostgreSQL advisory lock
  totp.py, crypto.py  MFA codes; AES-GCM for TOTP secrets and backups (HUB_DATA_KEY)
  guards.py        Network allowlist, rate limit, WebSocket origin check
  security_watch.py Daily certificate-expiry and audit-chain checks (system alarms)
  server.py        `hub-api` (everything in one process); admin.py `hub-admin`
simulators/        Modbus TCP server, MQTT broker + devices, OPC UA server; run.py = `hub-sim`
db/migrations/     0001_core, 0002_timescale (optional), 0003_operations, 0004_security
scripts/windows/   Windows service install/uninstall (WinSW)
frontend/          Web dashboard (not started)
tests/             unit, drivers (vs simulators), storage/db (real PostgreSQL via pgserver),
                   api, e2e (uvicorn + WebSocket + all three protocols)
.claude/skills/    Project-specific Claude Code skills (add-device, ...)
```

ESP32 firmware (e.g. the HLK-LD2450 radar console) lives in its own repository; to the hub such a
board is just another MQTT/JSON field device.

`docs/device_catalog.xlsx` lists candidate equipment and which hub input each one uses. It is a
planning aid, not a verified compatibility list.

Common commands (Windows; use `.venv/bin/...` elsewhere):

```
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q              # needs no hardware and no installed database
.venv\Scripts\python.exe -m hub.validate_config
.venv\Scripts\hub-sim.exe --config-dir config --out .sim
.venv\Scripts\hub-api.exe --config-dir .sim/config --dev --embedded-db .sim/db
```

Async code runs on the selector event loop on Windows (aiomqtt and psycopg need it): start
programs with `hub.runtime.run()`, never bare `asyncio.run()`.

## 4. Tag naming convention

```
<site>.<area>.<device_id>.<tag>        e.g.  kish.pump_station.plc_01.motor_speed
```

- Lowercase, `snake_case`, **ASCII only** (`[a-z0-9_]`), exactly four dot-separated segments.
- `device_id` is **globally unique** across all sites — config validation must reject duplicates.
- Protocol-specific addresses (Modbus register, OPC UA NodeId, MQTT topic/JSON path) live in the
  device config and map *to* a tag name; they never appear in tag names.
- Validate names with a single shared regex/validator in `hub/models.py`; do not re-implement it.

## 5. Hard rules

1. **Never write to a real device without going through the control-safety layer.** Drivers expose
   `write_tag()`, but it only accepts a `WriteGrant`, and only `backend/control.py` creates one —
   after RBAC, enablement (`HUB_CONTROL_ENABLED`, device, tag), range/step/rate checks,
   interlocks, optional two-person confirmation, and a `requested` audit row. Do not create a
   `WriteGrant` anywhere else, including scripts and tests outside `tests/drivers`. Schedules
   and OPC UA server writes go through `ControlService.request_write` as a real user too.
2. **No secrets in code or config.** Credentials, keys, and connection passwords come from
   environment variables / `.env` (gitignored). Commit only `.env.example` with placeholder values.
3. **Every driver must be testable against a simulator.** A new driver is not done until a matching
   simulator exists in `simulators/` and tests in `tests/` exercise it — no hardware required for CI.
4. **Read-only first.** No tag is writable by default. Writes require explicit `access: read_write`
   (with `min`/`max`) on the tag in config *and* a role permitted to write; the default for both
   is deny. Check config with `python -m hub.validate_config` before deploying.

## 6. Coding conventions

- Type hints everywhere; Pydantic models for all data crossing a boundary (config, API, driver output).
- Drivers implement a common async interface (connect / read / subscribe / write / close) and
  emit normalized samples with timestamp (UTC) and quality — never raw protocol objects.
- Handle disconnects with reconnect + backoff; a failing device must not stall other drivers.
- Use `logging`, not `print`. Never log secrets.
- `hub/models.py` is the schema of record. A change to it updates, in the same commit,
  `db/migrations/` (a new numbered file; never edit an applied migration) and the add-device skill (`references/schema-cheatsheet.md`, the Excel template
  and importer). `tests/db/` and `tests/skills/` fail when the enum lists or examples drift.
- Quality is stored as `QUALITY_CODES` (0 good, 1 uncertain, 2 bad); timestamps are UTC, taken
  from the device when it provides one (`timestamp_path` for MQTT), else the hub receive time.
- Keep tests fast and deterministic; mark hardware-dependent tests and skip them by default.

## 7. Language

- Code, comments, identifiers, commit messages, and docs: **English**.
- User-facing UI supports **Persian (fa) with RTL layout**, plus English. All UI strings go through
  i18n resources — no hard-coded user-facing text in components.
