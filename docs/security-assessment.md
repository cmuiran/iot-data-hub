# Security assessment (phase 6)

Date: 2026-10-23. Version: 0.6.0. Scope: the hub (drivers, storage, API, dashboard, outputs),
its configuration and installation scripts. Out of scope: field devices, the plant network, the
Windows/Linux host, PostgreSQL hardening, and the ESP32 radar firmware.

This is an internal, automated assessment that runs again on every change (CI). It does not
replace an independent penetration test before go-live (see the last section).

## 1. Methods

| Check | Tool | Where | Result |
|---|---|---|---|
| Lint, unsafe async patterns | ruff (E, F, W, B, UP, I, ASYNC) | CI `lint`, `scripts/check.py` | clean |
| Static security analysis | bandit (all rules; project-wide skips below) | CI `lint` | 0 findings |
| Known vulnerabilities in what ships | pip-audit on `requirements.lock` (43 packages) | CI `lint` + weekly schedule | 0 known |
| Automated penetration checks | `tests/api/test_pentest.py` (30 checks) | CI `test` | pass |
| Feature security tests | test_auth_users, test_security_phase1, test_data_control | CI `test` | pass |
| Capacity / denial of service by load | `hub-loadtest` | CI `loadtest` (weekly, releases) | see section 4 |

Bandit rules skipped project-wide, with the reason (`pyproject.toml`): B101 (asserts only state
invariants after the real checks), B110 (try/except/pass only in disconnect clean-up), B404/B603
(pg_dump/pg_restore run with argument lists, no shell, paths from configuration). Each remaining
suppression is in the code as `# nosec <id>` with its reason in a comment: the YAML loader is a
SafeLoader subclass (B506), SQL composed from whitelisted identifiers with all values as
parameters (B608), `random` used only for reconnect jitter (B311), XML escaped for writing, never
parsed (B406).

## 2. What the penetration checks try

| Attack | Expected | Test |
|---|---|---|
| Every API route (enumerated from the OpenAPI schema) without a session | 401 everywhere except the 6 public routes | `test_every_route_needs_a_session` |
| What the public routes reveal | status and counts only; no versions of dependencies, no names | `test_public_routes_reveal_nothing` |
| Forged tokens: `alg=none`, wrong key, expired, valid signature without a session, MFA token used as access token, garbage, wrong scheme | 401 | `test_forged_tokens_are_refused` |
| Token reuse after logout | access and refresh token dead at once | `test_logout_kills_the_access_token_at_once` |
| User enumeration at login | unknown user and wrong password answer identically | `test_login_does_not_tell_which_part_was_wrong` |
| Privilege escalation (viewer/operator → admin, user creation, roles, audit, backups, config) | 403/404 | `test_lower_roles_cannot_escalate` |
| Data outside the user's site/area (devices, tags, history, writes) | 404, as if it did not exist | `test_data_outside_the_scope_is_invisible` |
| Secrets in responses (password hashes, TOTP secrets, JWT key) | never | `test_secrets_never_leave_the_server` |
| SQL/NoSQL/template/log4j-style injection, NUL bytes, 5 kB strings in paths, queries and bodies | 4xx, no stack trace, tables intact | `test_injection_in_parameters_gives_clean_errors` |
| Malformed and oversized JSON (2 MB, 5000-level nesting, invalid UTF-8, 1e999) | 4xx | `test_malformed_bodies` |
| Path traversal into the program folder (`.env`, `.git`, `pyproject.toml`, `config/`), encoded and Windows variants | 400/404, no content | `test_no_path_traversal_into_the_program_folder` |
| CORS from a foreign origin | no `Access-Control-Allow-Origin` | `test_unknown_origins_get_no_cors` |
| Error pages, TRACE | generic | `test_errors_are_generic` |
| API map without sign-in | `/api/docs`, `/api/openapi.json` off in production | `test_production_app_hides_the_api_map` |

## 3. Findings and fixes

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | On Windows a URL containing characters that are invalid in file names (e.g. `:`) reached the dashboard's static file handler, which raised `OSError` → HTTP 500 with a logged stack trace (cheap error-log flooding) | Low | `_FrontendFiles` answers 404 (backend/app.py) |
| 2 | A write request or policy query for a tag outside the user's data scope answered `rejected_rbac` / the policy, revealing that the tag exists (an unknown tag gives 404) | Low (information disclosure; the write itself was always refused and audited) | the API answers exactly like an unknown tag; the attempt is still audited as `rejected_rbac` (backend/api/control.py) |
| 3 | Interactive API docs and the full OpenAPI schema were readable without sign-in in production | Low (reconnaissance) | `HUB_API_DOCS` (default off; on with `--dev`) |
| 4 | Load: above ~2000 samples/s the database writer used one round trip per row; the buffer filled and samples were dropped (300 devices × 20 tags at 1 s: 770 000 samples lost in 3 minutes) | High (data loss under load) | one statement per batch with arrays (`unnest`), batched device status (hub/storage/postgres.py) |
| 5 | Windows: the selector event loop (needed by psycopg and aiomqtt) handles at most 512 sockets; past that the whole hub stops ("too many file descriptors in select()") | High (availability) when > ~450 device connections | `validate_config` warns above 320 and refuses above 400 device connections on Windows, with the remedies (gateways, Linux/Docker, two hubs) |
| 6 | `upgrade.ps1` installed the program non-editable, so migrations and the dashboard would not have been found after an upgrade | High (failed upgrade) | editable install with the locked versions; `hub.paths.program_path` also finds the folders via `HUB_HOME` or the working directory |
| 7 | Windows: a socket closed by a library while still registered (paho-mqtt after a refused TLS handshake) made `select()` fail with WinError 10038, which stops the whole event loop, i.e. every driver; found as an intermittent test failure | High (availability; triggered by a misconfigured or hostile broker) | the hub's event loop (`hub.runtime.new_event_loop`) drops the dead socket, logs a warning and continues; `tests/runtime/test_event_loop.py` reproduces it deterministically |
| 8 | pip 25.0.1 in the build environment has known advisories | Info (build tool, not shipped) | CI and the installer upgrade pip first; the audit covers what ships |

## 4. Capacity (load test)

`hub-loadtest --devices 300 --tags 20` (210 Modbus TCP devices with their own address, 60 MQTT,
30 OPC UA; 6000 tags polled every second; simulators in separate processes; one open dashboard
subscribed to the live feed; embedded PostgreSQL 16 without TimescaleDB) on the development
laptop (Windows 11, Python 3.12):

| Figure | Before the fix | After |
|---|---|---|
| samples received | 5 706 /s | 5 856 /s |
| samples stored | 407 /s | 5 189 /s (the rest unchanged, filtered) |
| samples lost | 770 540 | 0 |
| write buffer, max | 200 000 (full) | 3 163 |
| event loop lag p99 | 416 ms | 72 ms |
| history query p95 (under load) | 121 ms | 59 ms |
| hub CPU, average | 97 % of one core | 70 % of one core |
| memory | 245 MB | 124 MB |

All six checks of the load test pass after the fixes (docs/backend.md section 26). Planning
rule: one hub process handles about **5000 samples/s** (e.g. 250 devices × 20 tags at 1 s, or
1250 devices × 20 tags at 5 s) on comparable hardware; a hub uses one CPU core. Above that, split
the plant over two hubs (each with its own config directory) or poll slower.

## 5. Residual risks (accepted, documented)

* Modbus TCP, S7 and SNMP v2c have no authentication or encryption: field devices must be in a
  separate OT network that only the hub can reach (docs/backend.md section 15).
* A PostgreSQL superuser can rewrite the audit tables including the hash chain; the daily anchors
  and the copy of every audit row in the log / SIEM (section 21) reveal it.
* `HUB_DATA_KEY` and `HUB_JWT_SECRET` live in `.env` on the server; `install.ps1` limits the file
  to Administrators and SYSTEM. Whoever is administrator on the server controls the hub.
* The simulators and `hub-sim` listen without authentication: development and training only.

## 6. Before go-live: independent penetration test

Ask the tester to cover, at least: the dashboard and API over HTTPS from the operator network
(authentication, sessions, MFA, RBAC, the control path incl. two-person confirmation), the
OPC UA server and Modbus server outputs if enabled, the server's exposed ports (section 8 of
docs/fa/install.md), TLS configuration, and a configuration review of the Windows host and
PostgreSQL. Retest with `python scripts/check.py` after every fix.
