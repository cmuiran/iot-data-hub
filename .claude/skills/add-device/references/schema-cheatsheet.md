# Schema cheatsheet (device YAML)

Snapshot of `hub/models.py` and `hub/config_loader.py` for quick lookup. **If it disagrees with the code, the code wins** — re-read `hub/models.py` before writing a file. All models forbid unknown keys.

Contents: Device · Tag (common) · Modbus · MQTT · OPC UA · Siemens S7 · HTTP/REST · SNMP · Cross-file checks · Examples

## Device (top level of `config/devices/<device_id>.yaml`)

| Field | Required | Notes |
|---|---|---|
| `device_id` | yes | segment; globally unique; file name = `<device_id>.yaml` |
| `name` | yes | display name, 1–128 chars |
| `site`, `area` | yes | segments |
| `protocol` | yes | `modbus_tcp` \| `modbus_rtu` \| `mqtt` \| `opcua` \| `s7` \| `http` \| `snmp` (selects the model) |
| `enabled` | no (true) | |
| `description` | no | |
| `poll_interval_ms` | no (1000; http 5000; snmp 10 000) | 100–86 400 000; **not for MQTT** |
| `history_heartbeat_s` | no (300) | 1–86 400: store a sample at least this often even if unchanged |
| `connection` | yes | per protocol, below |
| `tags` | no | list; a device with no tags only gets a warning |

Segment = `[a-z][a-z0-9_]{0,63}`. Tag path = `<site>.<area>.<device_id>.<tag>`.

## Tag (all protocols)

| Field | Default | Notes |
|---|---|---|
| `name` | required | segment, unique within the device |
| `description`, `unit` | none | |
| `data_type` | required | `bool` `int16` `uint16` `int32` `uint32` `int64` `uint64` `float32` `float64` `string` |
| `access` | `read` | `read` \| `read_write` |
| `scale`, `offset` | 1.0, 0.0 | engineering = raw × scale + offset; numeric types only; scale ≠ 0 |
| `min`, `max` | none | engineering units, `min < max`, numeric only; for int types must map into the raw range |
| `history` | true | false = live only, never stored |
| `history_deadband` | none | numeric only: store only when the value moved more than this (engineering units); unset = store every change |
| `states` | none | labels of the values of an integer or bool tag, e.g. `{0: stopped, 1: running}` (bool: keys 0/1); not with scale/offset. **Quote** YAML words `on`, `off`, `yes`, `no` (`0: "off"`) |

`read_write` requires: numeric type → both `min` and `max`; not `string`. (Modbus: also register_type `coil` or `holding`.)

## Modbus (`protocol: modbus_tcp`)

Connection: `host` (required, no scheme/path/spaces), `port` (502), `unit_id` (required, 0–255), `framer` (`socket` = Modbus TCP; `rtu` = RTU frames over TCP for transparent serial servers), `timeout_ms` (1000, 50–60 000), `retries` (3, 0–10), `byte_order` (`big`), `word_order` (`big`), `max_registers_per_read` (125, 1–125).

Tag extras: `register_type` (required: `coil` `discrete_input` `holding` `input`), `address` (required, 0-based, 0–65535), `count` (only for `string`, required there; otherwise omit — a mismatching value is an error).
Optional `bit` (0–15, 0 = LSB): one bit of a `holding`/`input` register as a read-only `bool` (status words).
Optional `bit` + `bit_count` (2–16): a multi-bit field of a register (e.g. bits 4–6 = mode) as `uint16`, read-only; combine with `states`.
Optional `encoding: bcd`: packed decimal digits, `uint16` (0–9999) or `uint32` (0–99 999 999, two registers in `word_order`); writable on holding registers.
Rules: coil/discrete_input → `bool`; `bool` on holding/input needs `bit`; `bit` tags cannot be `read_write`; width: bool 1, 16-bit 1, 32-bit 2, int64/uint64/float64 4; `address + width − 1 ≤ 65535`.
Addressing details: `modbus-addressing.md`.

## Modbus RTU (`protocol: modbus_rtu`)

Connection: `port` (required: `COM3`, `/dev/ttyUSB0`, `rfc2217://host:port` for an RFC 2217 serial server such as a Moxa NPort, `socket://host:port`), `unit_id` (required, **1–247**), `baudrate` (9600; 1200…115200), `bytesize` (8; 7/8), `parity` (`N`; `N`/`E`/`O`), `stopbits` (1; 1/2), `timeout_ms` (1000), `retries` (2), `byte_order`, `word_order`, `max_registers_per_read` (60).
Tags: exactly like Modbus TCP. Devices on the same `port` share the line: same line settings (else ERROR), different `unit_id`s (else ERROR). A Modbus TCP gateway in front of RS-485 is `modbus_tcp` with the gateway's host (see SKILL.md "Devices behind a gateway"); use `modbus_rtu` only when the hub itself owns the serial port or an RFC 2217 server.

## MQTT (`protocol: mqtt`)

Connection: `broker_ref` (required, id from `config/brokers.yaml`), `telemetry_topic` (required, filter; wildcards `+`/`#` allowed), `command_topic` (topic name, no wildcards), `status_topic` (filter), `payload_format` (`json`), `qos` (1; 0/1/2), `stale_after_s` (no telemetry for that long -> offline; set it when there is no LWT `status_topic`, the validator warns otherwise).
Tag extra: `json_path` (required): `$.a.b`, `$['key with space']`, `$.list[0]`; must select a field, not `$` itself; key chars `[A-Za-z0-9_-]` after the dot.
Device timestamp: `timestamp_path` (JSON path) + `timestamp_format` (`unix_s` | `unix_ms` | `iso8601`), both or neither. ISO strings must carry an offset (`Z`, `+03:30`); naive ones are rejected at runtime. Without them, samples get the hub's receive time.
Broker entry (`config/brokers.yaml`): `broker_id`, `host`, `port` (1883), `tls` (false), `keepalive_s` (60), `username_env`, `password_env` (env var **names**; password needs username; use `tls: true` with credentials or the validator warns). TLS options (need `tls: true`): `ca_file_env` (env var with the path of the plant CA; default = system CAs), `client_certificate_env` + `client_private_key_env` (both; mutual TLS), `tls_server_name` (name in the broker certificate when it differs from `host`). Certificate and host name are always verified.

## OPC UA (`protocol: opcua`)

Connection: `endpoint_url` (required, `opc.tcp://host[:port][/path]`), `security_policy` and `security_mode` (both required; `None`/`None` or both secure: policies `None` `Basic256Sha256` `Aes128_Sha256_RsaOaep` `Aes256_Sha256_RsaPss`, modes `None` `Sign` `SignAndEncrypt`), `auth` (`{type: anonymous}` default, or `{type: username, username_env, password_env}`), `namespace` (URI for node ids written without `ns=`/`nsu=`), `client_certificate_env` + `client_private_key_env` (env var names, **required for any secure policy**), `timeout_ms` (5000).
Tag extra: `node_id` (required): `[ns=<n>;|nsu=<uri>;]` + `i=<int>` | `s=<string>` | `g=<guid>` | `b=<base64>`.
Username auth with policy `None` sends the password in clear → validator warning.

## Siemens S7 (`protocol: s7`)

Connection: `host` (required), `port` (102), `rack` (0, 0–7), `slot` (1, 0–31; S7-1200/1500 0/1, S7-300 0/2), `timeout_ms` (2000), `max_bytes_per_read` (200, 16–940), `max_gap_bytes` (16).
Tag extras: `address` (required), `string_length` (1–254, only and always for `string`).
Addresses: `DB1.DBX6.2` (bit) `DB1.DBB6` (byte) `DB1.DBW6` (word) `DB1.DBD6` (double word); `M10.0` `MB10` `MW10` `MD10`; inputs `I0.1` `IW64` (also `E`); outputs `Q0.0` `QW2` (also `A`).
Types per size: X → `bool`; B → `uint16`/`int16` (BYTE/SINT), `int64`/`uint64`/`float64` (8 bytes: LINT/ULINT/LREAL), `string` (S7 STRING); W → `int16`/`uint16`; D → `int32`/`uint32`/`float32` (REAL). Inputs are read-only. PLC side: PUT/GET allowed, DBs **not** optimized.

## HTTP / REST (`protocol: http`) — read-only

Connection: `base_url` (required, `http(s)://host[:port][/path]`, no query), `auth` (`{type: none}` default | `{type: basic, username_env, password_env}` | `{type: bearer, token_env}` | `{type: header, header: X-Api-Key, value_env}`), `headers` (non-secret only; `Authorization`/`Cookie`/`X-Api-Key` refused), `timeout_ms` (5000), `verify_tls` (true), `ca_file_env`, `timestamp_path` + `timestamp_format`.
Tag extras: `path` (appended to base_url, starts with `/`, may carry a query; one request per distinct path and poll), `json_path` (required). `access` must be `read`.

## SNMP (`protocol: snmp`) — read-only

Connection: `host` (required), `port` (161), `security` (required): `{version: "2c", community_env}` or `{version: "3", username, auth_protocol md5|sha|sha224|sha256|sha384|sha512, auth_key_env, priv_protocol des|aes128|aes192|aes256, priv_key_env}` (protocol and key go together; priv needs auth), `timeout_ms` (2000), `retries` (1), `max_oids_per_request` (20).
Tag extra: `oid` (required, numeric, e.g. `1.3.6.1.2.1.33.1.2.4.0`; MIB names are not resolved). TruthValue (1/2) maps to `bool`; counters to integers; text numbers to float. Quote `version: "3"`.

## Cross-file checks (`hub/config_loader.py`)

Errors: duplicate `device_id`; duplicate tag name in a device; Modbus ranges overlapping within the same `register_type` (bit tags may share a register; the same bit twice is an error); tag wider than `max_registers_per_read`; same Modbus `host:port:unit_id` used by two devices; unknown `broker_ref`; overlapping MQTT topics on the same broker, wildcards included (`a/+/t` overlaps `a/x/t`; also between a device's own telemetry/command/status). With a disabled device involved, the collisions are warnings instead.
Warnings: file name ≠ `device_id`; device with no tags; same `json_path`/`node_id` used twice; referenced env var not set; broker credentials without TLS.

## Examples

Modbus TCP:

```yaml
device_id: meter_02
name: Feeder 2 power meter
site: kish
area: substation
protocol: modbus_tcp
description: Energy meter, registers per vendor map rev. B, word order CDAB.
poll_interval_ms: 1000
connection:
  host: 192.168.10.22
  unit_id: 1
  word_order: little      # CDAB
  max_registers_per_read: 60
tags:
  - name: voltage_l1
    unit: V
    data_type: float32
    register_type: input
    address: 0            # datasheet 30001
  - name: energy_total
    unit: kWh
    data_type: uint32
    register_type: input
    address: 10
    scale: 0.01
  - name: breaker_closed
    data_type: bool
    register_type: discrete_input
    address: 0
  - name: alarm_active    # status word 40051, bit 2
    data_type: bool
    register_type: holding
    address: 50
    bit: 2
```

Modbus RTU (two meters on one RS-485 adapter):

```yaml
device_id: meter_10
name: Workshop meter
site: kish
area: workshop
protocol: modbus_rtu
poll_interval_ms: 2000
history_heartbeat_s: 600
connection:
  port: COM4
  unit_id: 3
  baudrate: 19200
  parity: E
tags:
  - name: voltage_l1
    unit: V
    data_type: float32
    register_type: input
    address: 0
    history_deadband: 0.5
```

MQTT/JSON (payload `{"ts": 1767225600, "data": {"temp": 23.4, "hum": 41.0}, "battery": 87, "door": false}`):

```yaml
device_id: env_sensor_02
name: Cold room environment sensor
site: kish
area: warehouse
protocol: mqtt
connection:
  broker_ref: plant_broker
  telemetry_topic: kish/warehouse/env_sensor_02/telemetry
  qos: 1
  timestamp_path: $.ts
  timestamp_format: unix_s
tags:
  - name: temperature
    unit: degC
    data_type: float32
    json_path: $.data.temp
    min: -40
    max: 85
  - name: battery_level
    unit: "%"
    data_type: uint16
    json_path: $.battery
  - name: door_open
    data_type: bool
    json_path: $.door
```

OPC UA:

```yaml
device_id: plc_02
name: Booster pump PLC
site: kish
area: pump_station
protocol: opcua
poll_interval_ms: 500
connection:
  endpoint_url: opc.tcp://192.168.10.51:4840
  security_policy: Basic256Sha256
  security_mode: SignAndEncrypt
  client_certificate_env: HUB_OPCUA_CLIENT_CERT_PATH
  client_private_key_env: HUB_OPCUA_CLIENT_KEY_PATH
  auth:
    type: username
    username_env: HUB_OPCUA_PLC02_USERNAME
    password_env: HUB_OPCUA_PLC02_PASSWORD
  namespace: urn:kish:pump_station:plc_02
tags:
  - name: motor_speed
    unit: rpm
    data_type: float32
    node_id: ns=2;s=Pump2.MotorSpeed
  - name: motor_running
    data_type: bool
    node_id: ns=2;s=Pump2.Running
```

Siemens S7-1200:

```yaml
device_id: plc_s7_02
name: Filter skid PLC
site: kish
area: water_treatment
protocol: s7
connection:
  host: 192.168.10.61
  rack: 0
  slot: 1
tags:
  - name: inlet_flow
    unit: m3/h
    data_type: float32
    address: DB1.DBD0
  - name: pump_running
    data_type: bool
    address: DB1.DBX6.0
  - name: mode
    data_type: uint16
    address: DB1.DBB7
    states: {0: stopped, 1: automatic, 2: manual}
  - name: flow_setpoint
    unit: m3/h
    data_type: float32
    address: DB2.DBD0
    access: read_write
    min: 0
    max: 120
```

HTTP/REST (`GET /api/status` -> `{"ac": {"power_kw": 12.4}, "state": 2}`):

```yaml
device_id: inverter_02
name: Carport inverter
site: kish
area: solar_roof
protocol: http
poll_interval_ms: 5000
connection:
  base_url: https://192.168.10.71
  auth:
    type: bearer
    token_env: HUB_INVERTER02_TOKEN
tags:
  - name: ac_power
    unit: kW
    data_type: float32
    path: /api/status
    json_path: $.ac.power_kw
  - name: state
    data_type: uint16
    path: /api/status
    json_path: $.state
    states: {0: "off", 1: starting, 2: producing}
```

SNMPv3 UPS:

```yaml
device_id: ups_02
name: Control room UPS
site: kish
area: control_room
protocol: snmp
connection:
  host: 192.168.10.81
  security:
    version: "3"
    username: hubmon
    auth_protocol: sha256
    auth_key_env: HUB_UPS02_AUTH_KEY
    priv_protocol: aes128
    priv_key_env: HUB_UPS02_PRIV_KEY
tags:
  - name: battery_charge
    unit: "%"
    data_type: uint16
    oid: 1.3.6.1.2.1.33.1.2.4.0
  - name: battery_status
    data_type: int16
    oid: 1.3.6.1.2.1.33.1.2.1.0
    states: {1: unknown, 2: normal, 3: low, 4: depleted}
```

A writable tag (any protocol) additionally needs `access: read_write`, `min` and `max`; see `config/devices/meter_01.yaml` (`demand_limit`).
