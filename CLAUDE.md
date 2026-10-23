# CLAUDE.md — Industrial IoT Data Hub

Project-wide rules for anyone (human or agent) working in this repository.

## 1. Purpose

A **Data Hub** for industrial monitoring and control:

- Collect data from heterogeneous field devices: **MQTT/JSON**, **Modbus TCP/RTU**, **OPC UA**,
  **Siemens S7**, **HTTP/REST JSON**, **SNMP** (drivers are pluggable).
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
| Siemens S7     | `python-snap7` (asyncio client; its server is the simulator) |
| SNMP           | `pysnmp` (v2c/v3, asyncio; its agent engine is the simulator) |
| HTTP/REST      | `httpx`                                             |
| API / backend  | FastAPI                                             |
| Storage        | PostgreSQL + TimescaleDB (hypertables for history)  |
| Config         | YAML files, validated with Pydantic models          |
| Tests          | pytest (+ pytest-asyncio)                           |

Do not add new runtime dependencies without stating why in the commit message. After any
dependency change run `python scripts/lock.py` (requirements.lock = what ships and is audited).

## 3. Folder structure

```
docs/              Architecture and operations notes (backend.md), device_catalog.xlsx;
                   docs/fa/ Persian user guides (install, commissioning/SAT, operator, admin, training)
config/            brokers.yaml, devices/<id>.yaml, alarms.yaml, control.yaml, notifications.yaml,
                   northbound.yaml, modbus_server.yaml (*.example.yaml committed)
hub/
  models.py        Schema of record (Pydantic): devices, tags, alarms, control policy, Sample
  config_loader.py Loads + cross-checks config/ (incl. the Windows socket budget);
                   validate_config.py is its CLI
  paths.py         program_path(): db/ and frontend/ next to the code, $HUB_HOME or cwd
  codec.py         Register/byte-order decoding, scaling, coercion (pure, all drivers use it)
  drivers/         base.py (interface, reconnect, WriteGrant) + modbus_tcp, mqtt_json, opcua,
                   s7, http_json, snmp
  alarms.py        Limit/offline alarm engine
  history.py       Which samples are stored (deadband / heartbeat)
  maintenance.py   Hourly roll-up, retention, pg_dump backups
  pki.py           OPC UA certificates and trust lists
  runtime.py       Hub: drivers -> latest cache, live bus, alarms, batched storage writer
  storage/         migrate.py (db/migrations), postgres.py (the only store), records.py
  main.py          `hub`: collector-only console (commissioning)
  commission.py    `hub-commission`: read-only field tools (read report, Modbus probe/scan, OPC UA
                   browse, MQTT listen, SNMP walk, S7 dump, HTTP get) with tag suggestions
backend/
  app.py, api/     FastAPI routes (REST + /api/ws); errors are {"error": code, "message"}
  auth.py          Login, lockout, rotating refresh tokens; security.py (argon2, JWT)
  rbac.py          Roles x site/area scopes, default deny
  control.py       Control-safety layer: the ONLY caller of Driver.write_tag
  config_admin.py  Validated config changes from the API + live reload
  notifications.py e-mail / Telegram / SMS / webhook; schedules.py + cron.py: scheduled writes
  reports.py       Period reports (Excel/CSV); opcua_server.py: OPC UA server output
  modbus_server.py Modbus TCP server output; northbound.py: publish values to another broker
  ha.py            Active/standby via a PostgreSQL advisory lock
  totp.py, crypto.py  MFA codes; AES-GCM for TOTP secrets and backups (HUB_DATA_KEY)
  guards.py        Network allowlist, rate limit, WebSocket origin check
  security_watch.py Daily certificate-expiry and audit-chain checks (system alarms)
  health_watch.py  Self-monitoring every 30 s: database, disk, backup/restore, clock, storage
  logging_setup.py Text/JSON logs, syslog (RFC 5424) for a SIEM, redaction; logger hub.audit
  server.py        `hub-api` (everything in one process); admin.py `hub-admin`
simulators/        Modbus TCP, MQTT broker + devices, OPC UA, S7 PLC, HTTP/JSON, SNMP agent;
                   run.py = `hub-sim` (writes .sim/sim.env for hub-api --env-file);
                   loadtest.py = `hub-loadtest` (hundreds of devices, real hub + database)
db/migrations/     0001_core, 0002_timescale (optional), 0003_operations, 0004_security,
                   0005_protocols
scripts/windows/   install.ps1 (first install), build-bundle.ps1 (offline zip), service
                   install/uninstall (WinSW), upgrade.ps1 / rollback.ps1
scripts/           check.py (= CI locally), lock.py (requirements.lock)
.github/workflows/ ci.yml: lint + bandit + pip-audit, tests on Windows and Linux, load test,
                   bundle and Docker image on tags
Dockerfile, docker-compose.yml  Linux deployment with PostgreSQL/TimescaleDB
frontend/          Web dashboard served by hub-api at / (docs/frontend.md): plain ES modules,
                   no build step, no npm; js/views/ one file per page; i18n/fa.json + en.json
tests/             unit, drivers (vs simulators), storage/db (real PostgreSQL via pgserver),
                   api, e2e (uvicorn + WebSocket + all three protocols), frontend (static rules)
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
.venv\Scripts\hub-api.exe --config-dir .sim/config --env-file .sim/sim.env --dev --embedded-db .sim/db
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
   and OPC UA / Modbus server writes go through `ControlService.request_write` as a real user
   too. HTTP and SNMP devices are read-only; northbound MQTT is output only.
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
- Start event loops only with `hub.runtime.run()` / `new_event_loop()` (selector loop on Windows
  that survives sockets closed while registered); never plain `asyncio.run` for hub code.
- Use `logging`, not `print`. Never log secrets.
- API changes: `/api/v1` is a contract for integrations. Add fields and routes freely; renaming
  or removing one needs a new version (`/api/v2`) next to v1 (backend/app.py `ApiVersions`).
- Migrations are forward-only and the hub refuses a newer schema: never edit or delete an
  applied migration; a rollback restores the pre-upgrade backup (docs/backend.md section 24).
- `hub/models.py` is the schema of record. A change to it updates, in the same commit,
  `db/migrations/` (a new numbered file; never edit an applied migration) and the add-device skill (`references/schema-cheatsheet.md`, the Excel template
  and importer). `tests/db/` and `tests/skills/` fail when the enum lists or examples drift.
- Quality is stored as `QUALITY_CODES` (0 good, 1 uncertain, 2 bad); timestamps are UTC, taken
  from the device when it provides one (`timestamp_path` for MQTT), else the hub receive time.
- Keep tests fast and deterministic; mark hardware-dependent tests and skip them by default.
- Before committing: `python scripts/check.py` (ruff, bandit, lock, pip-audit, config, tests).
  Suppress a bandit finding only with `# nosec <id>` plus the reason in a comment.
- Hot path (per sample): no per-row database round trips, no work for absent subscribers;
  re-run `hub-loadtest` after changing drivers, runtime or storage (docs/backend.md section 26).

## 7. Language

- Code, comments, identifiers, commit messages, and docs: **English**. Exception: the user and
  operations guides for the plant staff live in Persian in `docs/fa/` (installation, commissioning
  and SAT, operator, administrator, training). Keep them in step with docs/backend.md, which stays
  the technical reference; command names, settings and code in them stay in English.
- User-facing UI supports **Persian (fa) with RTL layout**, plus English. All UI strings go through
  i18n resources — no hard-coded user-facing text in components. Add every new key to both
  `frontend/i18n/fa.json` and `en.json` (tests/frontend checks parity and usage).
- Frontend code: build DOM with `ui.h()` (text nodes), never `innerHTML`; no inline scripts or
  `style=` attributes (the CSP forbids them; set styles through `el.style`); no external URLs;
  commands only via `POST /api/control/write`.
