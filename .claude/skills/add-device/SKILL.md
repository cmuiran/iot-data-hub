---
name: add-device
description: Register a new field device (sensor, meter, PLC, drive, controller, gateway) in the Industrial IoT Data Hub by generating a validated config/devices/<device_id>.yaml for Modbus TCP/RTU, MQTT/JSON, OPC UA, Siemens S7, HTTP/REST JSON or SNMP. Use whenever the user asks to add, register, onboard or configure a device, including when they paste a datasheet, register map, sample JSON payload, OPC UA node list, S7 DB layout, OID list or Excel row, or describe it in Persian or English (e.g. "یک کنتور مودباس اضافه کن", "سنسور دما با MQTT تعریف کن", "add a PLC at 192.168.10.50"). Also for bulk onboarding from devices.xlsx and for changing an existing device (add a tag, change IP, unit id or topic, enable/disable). Prefer this over hand-writing device YAML. Not for ESP32 firmware settings, driver/simulator code, sending values to a device, or protocols without a hub driver (Sparkplug B, EtherNet/IP, FINS, BACnet, IEC 104).
---

# Add a field device

Turn whatever the user has (a sentence, a datasheet, a payload, a spreadsheet) into a device file that passes `python -m hub.validate_config`. The value of this skill is correctness: a wrong register address or a made-up NodeId silently reads the wrong data from a plant, so every address in the output must trace back to something the user gave you.

## 0. Read the sources of truth first

Do this before asking questions or writing anything — the schema changes over time and memory is not good enough.

1. Read `hub/models.py` completely (field names, enums, validators, defaults). If it disagrees with `references/schema-cheatsheet.md`, `models.py` wins; tell the user the cheatsheet is stale.
2. Read `CLAUDE.md` sections 4–5 (naming, hard rules).
3. List `config/devices/*.yaml` (existing `device_id`s, sites, areas, style of the neighbours), read `config/brokers.yaml`, and skim `config/alarms.yaml` / `config/control.yaml` if they exist.
   When the brand or model is known, read its entry in `references/device-notes.md` (connection pitfalls per device family; it never contains register addresses).
4. Find a Python that can run the validator: the hub needs **Python ≥ 3.12** (`StrEnum`, `datetime.UTC`) with pydantic + PyYAML. Use the project venv — `.venv\Scripts\python.exe` on Windows, `.venv/bin/python` elsewhere — not a bare `python`, which may be an older interpreter that fails with `ImportError` before validating anything. If none works, say so; do not skip validation silently.

Reply in the user's language (Persian or English), but everything written to files — identifiers, descriptions, comments — is English (CLAUDE.md §7). Pasted Persian/Arabic-Indic digits (۰-۹) must be converted to ASCII digits.

## 1. Gather inputs — ask only for what is missing

Identify the protocol from the input (register map → `modbus_tcp`, or `modbus_rtu` when the hub itself is on the RS-485 line: a COM/USB port or an RFC 2217 serial server; JSON payload/topic → `mqtt`; NodeIds/endpoint → `opcua`; Siemens addresses like `DB1.DBD0`, `M10.0` or a TIA Portal DB → `s7` (S7-300/400/1200/1500 without OPC UA); a REST/JSON URL → `http`; OIDs, MIB, UPS/PDU/switch → `snmp`). Ask if it is genuinely unclear. Then work out which of these are still unknown and ask for those in **one** message (use AskUserQuestion when a choice list fits; plain text for free-form values like IPs). Do not re-ask things already stated or visible in the pasted material.

| Always | `device_id`, display `name`, `site`, `area` |
|---|---|
| Modbus TCP | `host`, `unit_id` (port defaults to 502), tag addresses, byte/word order if any 32/64-bit tag; `framer: rtu` only for a transparent serial server |
| Modbus RTU | `port` (COM3, /dev/ttyUSB0, rfc2217://host:port), `unit_id` 1–247, baud rate and parity (from the device menu; all devices on one port must match) |
| MQTT | `broker_ref` (must exist in `config/brokers.yaml`), `telemetry_topic`; optional `status_topic`, `command_topic` |
| OPC UA | `endpoint_url`, security policy + mode, auth type, namespace URI if node ids use bare `s=` |

Defaults you may *propose* (never silently apply) so the question is a quick yes: the only broker in `config/brokers.yaml` for `broker_ref`; a display `name` built from the area and device kind ("Gate energy meter"); for `device_id`:
- the user explicitly called something the id/name of the device ("call it th-01") → that string normalised to the segment rules (`th-01` → `th_01`);
- otherwise — including when an identifier only appears *inside* a topic, host name or tag (`kish/gate/th-01/data`) — the repo pattern, kind + next free counter (`env_sensor_01` exists → `env_sensor_02`; `meter_01` → `meter_02`), and keep the original identifier in `description` so the two can be matched later.
The MQTT topic always keeps the user's exact string. When the user cannot be asked (batch/unattended run, or a test), apply the proposal, mark it in the file with a `# assumption:` comment, and list it under **Assumptions** in the report.

A `site` or `area` the user states explicitly is a given, not an invention — use it even if no existing device has it, and mention in the report that it is new (a typo like `substaton` next to an existing `substation` is the case to ask about).

**Collision pre-check (before writing).** Compare the new device with what exists: same Modbus `host:port:unit_id`, same `device_id`, overlapping MQTT topic on the same broker (wildcards count: `kish/warehouse/+/telemetry` overlaps every device in that area), same OPC UA endpoint. A collision usually means the user is re-adding or mis-typing an existing device, so ask which it is — and give the evidence that helps them decide: the existing device's id, site/area and whether its register map/payload looks like the new one (a different map or area points to a mistyped IP/unit, the same map to a re-add). Never change their IP, unit or topic to dodge it. If you cannot ask, write the device with `enabled: false` and a `# TODO` comment naming the clash (the validator then reports a warning instead of an error) and say so in the report.

Never invent addresses, register numbers, NodeIds, IPs, topics or unit ids. If the user cannot supply a value, follow the TODO policy below.

**TODO policy.** A tag whose address/`json_path`/`node_id` is unknown is left out of the YAML and listed under a `# TODO` comment block at the top of the file and in the final report. Only three connection values may take a visible placeholder — `host: todo.invalid` (reserved, never resolves), `unit_id: 0`, `endpoint_url: opc.tcp://todo.invalid:4840` — and then the device must carry `enabled: false` plus a `# TODO` comment on that line, so the hub cannot talk to a made-up address. Any other missing required field (broker, topic, security settings): ask, or stop and report that the device could not be completed.

## 2. Parse the source

- **Modbus** (PDF/Excel/pasted table): extract per register the address as written, area/function code, type, count, scale, unit, access. Convert to the schema's 0-based `address` + `register_type`; the notation rules, worked examples and the word-order table are in `references/modbus-addressing.md` — read it whenever addresses are not already 0-based protocol addresses, or any 32/64-bit value is involved. Decide `data_type` (`int64`/`uint64` occupy 4 registers — typical for energy counters; packed BCD → `encoding: bcd` on `uint16`/`uint32`; a field of several bits → `bit` + `bit_count`, `uint16`, read-only; coded values (mode 0/1/2…) → `states` labels), status-word flags as `bool` tags with `bit: 0-15` (0 = LSB) on the holding/input register — read-only, and it is fine to keep the raw word as a `uint16` tag on the same address; `scale`/`offset` (engineering = raw × scale + offset, so "resolution 0.1" → `scale: 0.1`), and `byte_order`/`word_order` (device-level, not per tag). **Flag, do not guess**, when addressing base (0 vs 1) or word order is ambiguous: ask the user for one known live value, or record it as a warning in the summary. Pull `max_registers_per_read` from the datasheet if it states a limit.
- **MQTT** (sample payload): for each leaf field propose `json_path` (`$.a.b`, `$['key with space']`, `$.list[0]`), infer `data_type` (true/false → `bool`; decimal → `float32`; text → `string`; integer → an integer type (smallest that fits, `int32` if unsure) **only** for counts, states, codes and percentages like battery level, but `float32` for measured quantities (temperature, humidity, pressure, power…) because a sample such as `"h":51` is often just a value that happened to be whole) and `unit` from key names or datasheet. Mark inferred units/types as "inferred" in a YAML comment and in the report. Do not convert units the user stated (`W` stays `W` even if another meter uses `kW`). Skip metadata fields (message ids, sequence numbers) unless the user wants them. A device timestamp in the payload (`ts`, `time`, …) goes into the connection as `timestamp_path` + `timestamp_format`, not into a tag: 10-digit integers are `unix_s`, 13-digit are `unix_ms`, ISO strings are `iso8601` **only if they carry an offset** (`Z`, `+03:30`) — a naive ISO time is rejected at runtime, so leave the timestamp out and warn. Unsure of the unit → ask, or note it as an assumption. No timestamp in the payload → samples get the hub's receive time; say so, because buffered messages will be stamped late. Several sample payloads → take the union and note fields that appear only sometimes.
- **OPC UA** (browse export, NodeId list): map each variable node to a tag; keep the NodeId string exactly as exported. Use the node's DataType for `data_type` (Double → `float64`, Float → `float32`, Int16/UInt16/Int32/UInt32/Int64/UInt64/Boolean/String map directly; Byte/SByte → `uint16`/`int16`; DateTime, arrays, structures and others → TODO). Skip folders/objects/methods.
- **Siemens S7** (TIA Portal DB view, symbol table, STEP 7 address list): one tag per variable with its absolute address (`DB1.DBD0`, `DB1.DBX6.2`, `MW20`, `IW64`). TIA types → `data_type`: Real → `float32`, LReal → `float64` (`DBBx`), Int → `int16` (`DBW`), Word/UInt → `uint16`, DInt → `int32` (`DBD`), DWord/UDInt → `uint32`, Bool → `bool` (`DBX`), Byte/USInt → `uint16` (`DBB`), String[n] → `string` + `string_length: n`. Offsets come from the DB with "optimized block access" **off** (the offset column only appears then) — if the user only has symbolic names, ask for the offsets; never compute them from the order of declarations. Remind them that the CPU needs PUT/GET enabled. Rack/slot: S7-1200/1500 0/1, S7-300 0/2.
- **HTTP/REST** (API doc, sample response): `base_url` = scheme + host (+ fixed prefix); one tag per value with `path` (the endpoint) and `json_path`, like MQTT. Credentials → `auth` with env var names; prefer `https` (`ca_file_env` for a device certificate signed by the plant CA). Read-only.
- **SNMP** (MIB, OID list, vendor doc): numeric OIDs only (resolve names with the vendor MIB or a MIB browser; scalar objects end in `.0`, table cells end in the row index). Standard UPS-MIB (RFC 1628) and IF-MIB OIDs are fine to use directly. Prefer SNMPv3 with `sha256`/`aes128`; v2c only when the device cannot do v3. Enumerations (battery status, output source) → integer + `states`. Read-only.
- **Bulk (Excel with many devices)**: use `scripts/excel_to_devices.py` (see below) instead of writing files by hand.

## 3. Write `config/devices/<device_id>.yaml`

Follow `hub/models.py` exactly: unknown keys are errors (`extra="forbid"`), enum values are lowercase strings, `poll_interval_ms` exists for every protocol except MQTT (MQTT pushes), `count` is only for `string` tags. Omit fields that equal the model default unless they are informative. Use the existing files in `config/devices/` as the style reference and `references/schema-cheatsheet.md` for a quick field list and one full example per protocol. The file name must equal `device_id` (the validator warns otherwise); refuse to overwrite an existing file without the user's say-so.

## 4. Naming and access

- Segments (`site`, `area`, `device_id`, tag `name`): lowercase ASCII snake_case, start with a letter, ≤64 chars, no dots. Tag path is `<site>.<area>.<device_id>.<tag>` and addresses never go in names. Translate Persian names to short English names (`دمای ورودی` → `inlet_temperature`).
- `device_id` is globally unique across all sites — check every file in `config/devices/`. Follow the existing pattern (`plc_01`, `meter_01`, `env_sensor_01`: kind + two-digit counter). Reuse an existing `site`/`area` spelling when it matches; ask before inventing a new one.
- Units: reuse the spelling already in the repo (`degC`, `%`, `V`, `A`, `kW`, `kWh`, `rpm`, `bar`, `h`).
- **Every tag is `access: read` (the default; omit the key).** If the user wants a writable tag: for numeric types require `min` and `max` in engineering units from the datasheet or the user — never choose them yourself (for `bool` coils/flags the model rejects `min`/`max`, so there are none to set; the on/off range is the whole safety check, say so) — set `access: read_write`, make sure the register is `coil` or `holding` (input/discrete_input are read-only in Modbus; strings can't be writable), and tell the user that config alone grants nothing: writes still go through the control-safety layer in `backend/` (RBAC, per-tag enablement, range check, audit — CLAUDE.md §5.1 and §5.4). List every writable tag in the summary.
- Secrets never go in YAML. Credentials are env-var **names** (`username_env`, `password_env`, `client_certificate_env`, …) in UPPER_SNAKE_CASE following `HUB_<PROTOCOL>_<DEVICE>_<WHAT>`; add the names with placeholder values (`changeme`) to `.env.example`, never to `.env`. A new MQTT broker goes into `config/brokers.yaml` the same way (ask for host/port/TLS; credentials as env names).

## Devices behind a gateway

Two ways for serial (RS-485) devices: the hub owns the line (`protocol: modbus_rtu`, a USB/RS-485 adapter or an RFC 2217 serial server), or a Modbus TCP gateway converts it (`protocol: modbus_tcp`, described here — the usual case). A transparent serial server that passes raw RTU frames over TCP is `modbus_tcp` with `framer: rtu`.

Serial (RS-485 / Modbus RTU) devices reach the hub through a Modbus TCP gateway (Moxa MGate, Anybus, a PLC acting as gateway…). Model each serial device as its **own** `modbus_tcp` device file with the gateway's `host`/`port` and the device's own RTU slave address as `unit_id`; the gateway itself gets no device file unless it exposes data of its own.

- Several devices then share one `host:port` — that is expected; only the same `unit_id` twice is a collision.
- Ask for the RTU slave address of every device (from its front-panel menu or the commissioning sheet); never number them yourself.
- The serial line is slow and shared: keep `poll_interval_ms` realistic (≥ 1000 ms is a safe start for a few devices at 9600–19200 baud), raise `timeout_ms` above the gateway's own serial timeout, and put the gateway name and serial settings (baud, parity) in each device's `description`.
- Byte/word order and addressing come from the *field device's* datasheet, not the gateway's.
- Protocol gateways (Node-RED, Neuron, Kepware) that translate S7comm/FINS/EtherNet/IP expose data as Modbus TCP, MQTT or OPC UA: register the gateway's *output* (its Modbus map, topics or NodeIds), and name the device after the field device, not the gateway.

## Data volume and liveness

- Every tag is stored on change by default, plus a heartbeat every `history_heartbeat_s` (300 s). For noisy analog values propose a `history_deadband` from the datasheet resolution or the user's wish (e.g. 0.5 V on a 230 V voltage) — never invent one silently; `history: false` for values nobody needs in history (counters of diagnostics, serial numbers).
- MQTT devices without an LWT `status_topic` need `stale_after_s` (about 3× the publish interval) so a dead sensor does not keep its last value as good; ask for the publish interval, the validator warns when neither is set.

## Changing an existing device

"Add a register/tag to meter_01", "change the IP of plc_01", "disable env_sensor_01" use the same rules: read the existing file, edit it in place, keep every other tag and comment untouched, re-run the collision pre-check and the validator. Removing tags or devices is the user's call: do it only when asked explicitly.

## 5. Validate

Run from the project root with the interpreter found in step 0.4:

```
.venv\Scripts\python.exe -m hub.validate_config      # Windows
.venv/bin/python -m hub.validate_config              # Linux/macOS
```

Exit code 0 = no errors (warnings allowed), 1 = errors, 2 = no config directory. Read the line for your new device *and* the lines of existing devices — a collision is reported on both files.

Fix every ERROR and rerun until there are none. Typical causes: address overlap with another tag, `unit_id` collision on the same host/port, unknown `broker_ref`, topic collision, missing `min`/`max` on a writable tag, int range violations of `min`/`max` vs `scale`. Warnings about unset env vars are expected for new secrets (mention them); other warnings go in the summary. Never "fix" an error by deleting a safety field or loosening a range the user did not ask to change.

## 6. Alarms, control policy, simulation

- Simulation needs no extra work: `hub-sim` builds simulators straight from the device YAML. Suggest
  `hub-sim --config-dir config --out .sim` + `hub --config-dir .sim/config` to see the new tags
  decode before touching hardware.
- If the user mentions limits ("alarm above 35 °C", "warn when the breaker opens"), add a rule to
  `config/alarms.yaml` (schema `AlarmRule`: `hi`/`hihi`/`lo`/`lolo` for numbers, `equals` for
  bools, `deadband`, `delay_s`, `severity`); never invent limits.
- A new `read_write` tag gets the `defaults` of `config/control.yaml`. Mention that a per-tag entry
  can require a second person (`confirm: true`), limit the step (`max_step`) or add interlocks,
  and add one only when the user asks.

## 7. Finish with a short report

One table of tags created (name, address/path/NodeId, type, unit, access), then bullet lists for **TODO / unknowns**, **Assumptions** (every default you applied without being told: broker, device_id, display name, word order left at big/big, inferred units/types, `enabled: false` holds, a new site/area), **Warnings** (ambiguity decisions — addressing base, word order, bit numbering not yet checked against a live value — writable tags, collisions, unset env vars) and the **Next step** — test the connection with QModMaster (Modbus; compare a known value to check addressing and word order), MQTT Explorer (check topic and payload against `json_path`s), or UaExpert (check NodeIds and security policy), then enable the device if it was written with `enabled: false`. Keep it short; the YAML is the deliverable.

## Bulk onboarding with Excel

`templates/devices_template.xlsx` has the sheets `Devices`, `ModbusRegisters`, `MqttTags`, `OpcuaTags` (rows whose first cell starts with `#` are examples/comments and are skipped; a `ReadMe` sheet explains the columns). Copy it (never edit the template itself), fill it (or fill it from the user's data), then from the project root with the step-0 interpreter — `<skill-dir>` is this skill's own directory, wherever it is installed:

```
<python> -m pip install -e ".[dev]"   # includes openpyxl (dev-only; not a hub runtime dependency)
<python> <skill-dir>/scripts/excel_to_devices.py devices.xlsx --dry-run
<python> <skill-dir>/scripts/excel_to_devices.py devices.xlsx --check
```

What it does:
- one YAML per device into `config/devices/`; refuses to overwrite without `--force` (only with the user's say-so);
- converts Modbus addresses (`address_style`: `protocol` | `one_based` | `modicon`). Modicon **coil** addresses must be typed as text (`'00001`): Excel drops leading zeros from numbers and the row is then refused with a hint;
- skips tags whose address is `TODO`/blank and lists them in the file header; unknown host/unit_id/endpoint get the placeholders from the TODO policy and `enabled: false`;
- **validates before writing**: every generated device is checked together with the existing config using `hub.config_loader`; a device that would add an ERROR (duplicate id, host:port:unit_id clash, overlapping topic, unknown broker, schema error) is reported and not written, the others are. This needs the hub importable (project root, Python ≥ 3.12); if it prints "pre-write validation skipped", fix the interpreter instead of ignoring it;
- `--check` then runs the full validator.

Exit code 1 means at least one device or tag was not generated — read every `ERROR` line. Then still do steps 4–7 on the result: review the generated YAML, resolve refused devices with the user (same collision rules as a single device), and report. The validator shows placeholder devices as `OK` because they are disabled; take the `TODO` lines from the script output, not from the validator, for the report.

## Bundled resources

- `references/modbus-addressing.md` — 0/1-based, 0x/1x/3x/4x notation, function codes, 32/64-bit word orders, status bits, with examples.
- `references/schema-cheatsheet.md` — field reference and a complete example for each protocol.
- `references/device-notes.md` — per device family (PLCs, drives, meters, gateways, IoT): which protocol to use and what to check before onboarding.
- `scripts/excel_to_devices.py`, `templates/devices_template.xlsx` — bulk onboarding (tested by `tests/skills/test_excel_to_devices.py`).
- `evals/evals.json` — scenarios for checking the skill after a change (run them with the skill-creator skill).
