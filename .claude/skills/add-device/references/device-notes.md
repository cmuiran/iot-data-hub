# Device notes (by family)

What to check before onboarding common equipment. Source: `docs/device_catalog.xlsx` (a market-experience list, **not verified per model**). Protocol support depends on model, firmware, licence and add-on modules, so confirm with the datasheet every time.

**This file never contains register addresses, NodeIds or IPs.** Those come only from the device's own datasheet, the PLC programmer, or a live browse/read. If a user asks you to "use the usual PM5560 registers", ask for the register list instead.

Phase column = when the hub can talk to it: **1** direct (Modbus TCP / MQTT / OPC UA), **2** needs a simple gateway (RS-485 → TCP, LoRaWAN → MQTT), **3** needs a protocol gateway (S7comm, FINS, EtherNet/IP, …) — out of scope until such a gateway exists.

Contents: PLCs · Drives · Meters and analysers · I/O and gateways · IoT · SCADA

## PLCs

| Family | Hub input | Phase | Check before onboarding |
|---|---|---|---|
| Siemens S7-1500 | OPC UA | 1 | The OPC UA server must be enabled in TIA Portal (and licensed). Ask for the endpoint, the security policy actually configured, and an exported or browsed list of NodeIds — S7 NodeIds look like `ns=3;s="DB_Name"."Var"`; keep the quotes exactly as exported. |
| Siemens S7-1200 | Modbus TCP | 1 | Modbus comes from the `MB_SERVER` block in the PLC program, so the register map is **whatever the PLC programmer mapped** (usually a DB or MW area). Ask the programmer for the map; there is no datasheet map. OPC UA only with suitable firmware and licence. |
| Siemens S7-300 / S7-400 | via gateway (MQTT / OPC UA) | 3 | S7comm only; needs Node-RED, Neuron or Kepware. Register the gateway's output, named after the PLC. |
| Schneider Modicon M221 / M241 / M340 | Modbus TCP | 1 | Ethernet port required (M221: models with Ethernet). Map = %MW words defined in the program; ask the programmer. Schneider documentation often uses 0-based %MW numbers, not Modicon 4xxxx — check the base. |
| Delta DVP / AS | Modbus TCP | 1 / 2 | Models without Ethernet need RS-485 + RTU→TCP gateway (see SKILL.md "Devices behind a gateway"). Delta manuals list device addresses (D, M, X, Y) with their Modbus hex equivalents — use the Modbus column. |
| Mitsubishi FX5U / FX3U | Modbus TCP | 1 / 2 | FX5U has Modbus TCP built in (must be configured); FX3U usually needs an Ethernet module. |
| Omron NX / NJ | OPC UA | 1 | Depends on model; confirm OPC UA server is enabled. |
| Omron CP1L / CJ2 | via gateway | 3 | FINS / EtherNet/IP only. |
| Allen-Bradley CompactLogix | via gateway | 3 | EtherNet/IP (CIP) only. |
| Beckhoff CX (TwinCAT) | OPC UA | 1 | Needs the TF6100 licence. |

## Drives (VFD)

| Family | Hub input | Phase | Check before onboarding |
|---|---|---|---|
| ABB ACS580 / ACS355 | Modbus TCP (gateway) | 2 | RS-485 built in; Ethernet needs an adapter module. Drives expose **status and control words** — model status flags with `bit`, keep control words read-only until the control-safety layer exists. |
| Siemens SINAMICS G120 | Modbus TCP (gateway) | 2 | Depends on control unit (PN vs RS-485 variant). |
| Danfoss VLT FC51 / FC302 | Modbus TCP (gateway) | 2 | FC302 Ethernet via option card. Danfoss parameter numbers are not register addresses: the manual gives the conversion — follow it, do not guess. |
| Delta VFD-E / MS300 | Modbus TCP (gateway) | 2 | MS300 with Ethernet module can speak Modbus TCP directly. |
| Schneider ATV320 / ATV630 | Modbus TCP | 1 / 2 | ATV630 has Ethernet built in; ATV320 usually RS-485. |

Drives in general: scaled values are common (frequency ×0.1 or ×0.01 Hz, current ×0.1 A) — take the scale from the datasheet. Any writable tag (speed reference, start/stop) is a control point: `access: read_write` with `min`/`max` only on explicit request, and always flag it.

## Meters and analysers

| Family | Hub input | Phase | Check before onboarding |
|---|---|---|---|
| Schneider PM5560 | Modbus TCP | 1 | Large map; energy values are typically 64-bit (`int64`) and floats are typically 1-based Modicon-style in the register list — verify base and word order with one live value. |
| Siemens PAC3200 / PAC4200 | Modbus TCP | 1 | Mostly float32 measurands; check word order against the display. |
| Socomec DIRIS A-series | Modbus TCP / RTU | 1 / 2 | Model-dependent Ethernet; confirm with datasheet. |
| Selec MFM384 | Modbus TCP (gateway) | 2 | RS-485 only; map in the product manual. |
| ABB B23 / B24 | Modbus TCP (gateway) | 2 | RS-485 versions need a gateway. |

Meters in general: decide units carefully (W vs kW, Wh vs kWh) and keep the unit the datasheet states; energy counters are monotonic, so a decreasing value later usually means wrong word order or a 32-bit counter that wrapped.

## I/O and gateways

| Family | Role | Check before onboarding |
|---|---|---|
| Moxa MGate MB3170 / MB3270 | RTU → TCP gateway | Each RS-485 device gets its own device file: gateway IP, the device's own slave id as `unit_id`. The gateway's own serial timeout must be shorter than the hub's `timeout_ms`. |
| Moxa MGate 5105 / HMS Anybus | fieldbus converter | Output protocol depends on model (Modbus TCP or MQTT) — register the output side. |
| Moxa NPort | serial device server | Raw TCP only, **not** Modbus TCP. Cannot be onboarded directly; needs a real protocol gateway. |
| WAGO 750-352 | Modbus TCP coupler | Register layout depends on the plugged I/O modules (process image); get the process-image table from the configurator. |
| Advantech ADAM-6017 / 6050 | Modbus TCP remote I/O | Map is in the module manual; analog inputs often need scaling from raw counts. |

## IoT and telemetry

| Family | Hub input | Phase | Check before onboarding |
|---|---|---|---|
| ESP32 custom firmware | MQTT (JSON) | 1 | Topic and JSON format are ours to define: prefer `<site>/<area>/<device_id>/telemetry` and include a `ts` with UTC offset or Unix seconds so `timestamp_path` can be used. |
| Shelly Pro / Plus | MQTT | 1 | Enable MQTT in the device settings; the topic prefix is set there. Get one real message from MQTT Explorer before mapping. |
| Milesight UG65 + EM300-TH (LoRaWAN) | MQTT | 2 | The gateway's network server publishes decoded uplinks; payload shape depends on the decoder — always map from a captured message. Sensors buffer, so a device timestamp matters here. |
| Generic RS-485 temperature/humidity sensor | Modbus TCP (gateway) | 2 | Brand unknown → no map → cannot be onboarded until the register list is found. |

## SCADA

| Family | Notes |
|---|---|
| Ignition | Exchanges data over OPC UA (Ignition's server) or MQTT **Sparkplug B**. Sparkplug B is binary protobuf, not JSON — the hub has no Sparkplug driver, so do not register it as a `mqtt` JSON device. OPC UA from Ignition can be onboarded as a normal `opcua` device. |
