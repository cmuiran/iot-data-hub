# Modbus addressing and data layout

Contents: 1. What the schema wants · 2. Areas and function codes · 3. Modicon notation · 4. Converting addresses (examples) · 5. Deciding 0-based vs 1-based · 6. Data types and scaling · 6a. Status bits · 7. 32/64-bit byte/word order · 8. Pitfalls

## 1. What the schema wants

Each Modbus tag has `register_type` (`coil` | `discrete_input` | `holding` | `input`) and `address`, the **0-based address as sent on the wire** (0–65535). `count` is derived from `data_type` (see section 6) and only written for `string`. Datasheets rarely use this form, so convert.

## 2. Areas and function codes

| Area | Modicon prefix | `register_type` | Unit | Read FC | Write FC | Writable? |
|---|---|---|---|---|---|---|
| Coils | `0xxxx` | `coil` | 1 bit | 01 | 05 (single), 15 (multiple) | yes |
| Discrete inputs | `1xxxx` | `discrete_input` | 1 bit | 02 | – | no |
| Input registers | `3xxxx` | `input` | 16 bit | 04 | – | no |
| Holding registers | `4xxxx` | `holding` | 16 bit | 03 | 06 (single), 16 (multiple), 22 (mask), 23 (read/write) | yes |

If a datasheet says "function code 03/06/16" the area is holding; "04" is input; "01/05/15" coils; "02" discrete inputs. `bool` tags are `coil` or `discrete_input`, or one bit of a holding/input register (section 6a).

## 3. Modicon notation

The leading digit names the area; the rest is a **1-based** number inside that area.

- 5-digit form: prefix + 4 digits → 1…9999 (`40001` … `49999`).
- 6-digit form: prefix + 5 digits → 1…65536 (`400001` … `465536`), used by devices with more than 9999 registers.
- Rule: `address = number_after_prefix − 1`.
- Four-digit or shorter numbers (`4001`, `101`) are NOT unambiguous notation — treat as plain numbers and settle the base via section 5.

## 4. Converting addresses (examples)

| Datasheet says | Interpretation | `register_type` | `address` |
|---|---|---|---|
| `40001` | Modicon, 5-digit | holding | 0 |
| `40108` | Modicon | holding | 107 |
| `30001` | Modicon | input | 0 |
| `30011` (float, 2 regs) | Modicon | input | 10 (occupies 10–11) |
| `00017` | Modicon | coil | 16 |
| `10003` | Modicon | discrete_input | 2 |
| `400001` / `465536` | Modicon, 6-digit | holding | 0 / 65535 |
| "Holding register 101", table is 1-based | 1-based | holding | 100 |
| "Register 100, FC03", table is 0-based | 0-based | holding | 100 |
| `0x0064` in a frame example | protocol address | (from FC) | 100 |

A 32-bit value at "40101–40102" is ONE tag: `address: 100`, `data_type: float32` (or `int32`/`uint32`); the second register is not a separate tag, and the validator will report an overlap if you add one.

## 5. Deciding 0-based vs 1-based

Pick the base from evidence, in this order:

1. The table is in Modicon notation (5/6 digits starting 0/1/3/4) → 1-based, subtract 1.
2. The datasheet has an example request frame or says "PDU address" / "protocol address" / shows hex like `0x0000` for the first register → 0-based.
3. The document states "addresses start at 0" or "start at 1" → follow it.
4. Otherwise it is **ambiguous**: ask the user, or ask for one live value to verify (read the voltage register in QModMaster with base 0 and base 1 and see which matches the display), or record the decision as a warning in the summary. A wrong base reads the neighbouring register and the data still looks plausible — that is why it must be flagged, never silently assumed.

## 6. Data types, count and scaling

| `data_type` | Registers (`count`) | Notes |
|---|---|---|
| `bool` | 1 bit | coil / discrete_input only |
| `int16`, `uint16` | 1 | "signed"/"two's complement" → `int16`; otherwise `uint16` |
| `int32`, `uint32`, `float32` | 2 | word order matters (section 7) |
| `float64` | 4 | word order matters |
| `string` | `count` required | 2 ASCII bytes per register |
| `int64`, `uint64` | 4 | energy counters (Wh/kWh) on many analysers; word order matters |
| BCD, multi-bit fields, enums packed in bits | – | not in the schema: keep the raw `uint16`, add a TODO |

Engineering value = `raw × scale + offset`. "Resolution 0.1", "value × 10", "x/10" → raw is 10× the unit, so `scale: 0.1`. "kWh with 2 decimals stored as integer" → `scale: 0.01`. `min`/`max` are in engineering units and are checked against the raw integer range (`(value − offset) / scale` must fit), so `uint16` with `scale: 0.1` accepts `max` up to 6553.5.

## 6a. Status bits inside a register

Drives and PLCs pack flags into a status word ("40100 bit 3 = fault"). Each flag is a `bool` tag on the same `register_type`/`address` with `bit: 0-15`:

```yaml
  - name: status_word          # optional raw word, useful for diagnostics
    data_type: uint16
    register_type: holding
    address: 99                # datasheet 40100
  - name: drive_fault
    data_type: bool
    register_type: holding
    address: 99
    bit: 3
```

- `bit: 0` is the **least significant** bit. Datasheets that number bits 1–16, or write `40100.3` / `40100:03`, need checking: ask, or compare with a known live state (drive running → its "running" bit must be 1).
- Bit tags are read-only. Control words (start/stop/reset bits) are written as a whole `uint16` holding register through the control-safety layer, never as single bits.
- The validator allows bits to share a register with each other and with the raw word; the same bit twice is an error.

## 7. 32/64-bit byte and word order

`byte_order` and `word_order` live on the device connection (`big` or `little`, both default `big`), so they apply to every multi-register tag of that device. If a datasheet shows different orders for different tags, ask; do not average. Convention used here: `word_order` = which register comes first; `byte_order` = byte order inside each 16-bit register. `hub/codec.py` implements exactly this convention (tests: `tests/hub/test_codec.py` use the table below).

Example value `0x12345678` as bytes A=12 B=34 C=56 D=78, and `123.456` as float32 = `0x42F6E979`:

| Common name | Registers on the wire (reg N, reg N+1) | `byte_order` | `word_order` | float32 123.456 shows as |
|---|---|---|---|---|
| ABCD "big-endian" (Modbus standard) | `0x1234`, `0x5678` | big | big | `0x42F6`, `0xE979` |
| CDAB "word-swapped" (very common: many meters, Siemens/Schneider) | `0x5678`, `0x1234` | big | little | `0xE979`, `0x42F6` |
| BADC "byte-swapped" | `0x3412`, `0x7856` | little | big | `0xF642`, `0x79E9` |
| DCBA "little-endian" | `0x7856`, `0x3412` | little | little | `0x79E9`, `0xF642` |

64-bit values (`int64`, `uint64`, `float64`, 4 registers) use the same two settings: `word_order: big` = most significant register first (N … N+3), `little` = least significant register first (N+3 … N reversed). A device that is CDAB for 32-bit values is usually low-word-first for 64-bit values too, but check one counter against the display before trusting it.

How to decide: the datasheet naming (ABCD/CDAB…) or the phrase "low word first" (= `word_order: little`); otherwise use a known live value: read the two raw registers in QModMaster, compare with the device display. A float that decodes to absurd magnitudes (1e30, 1e-38, NaN) is the typical symptom of the wrong order. If neither datasheet nor a live value is available, use the default (`big`/`big`), and flag "word order unverified" in the warnings.

## 8. Pitfalls

- Register gaps are fine; overlaps are errors. Reserved/undefined registers inside a block may make a block read fail on some devices — respect any `max_registers_per_read` limit in the datasheet.
- Some devices need a minimum delay between requests or allow few simultaneous connections; mention datasheet limits in the device `description` and tune `poll_interval_ms` / `timeout_ms` only from documented values.
- Unit id: Modbus TCP gateways in front of serial (RTU) devices use `unit_id` to select the slave; two devices behind the same host:port must have different `unit_id`s (the validator errors on a collision).
- Excel/CSV sources: a cell typed `00001` becomes the number 1, so coil addresses in Modicon notation lose their area prefix. In the bulk workbook, enter them as text (`'00001`); when reading a user's spreadsheet, check whether a column of short numbers next to `3xxxx`/`4xxxx` values is really a coil list.
- Exception 02 (illegal address) on first read usually means wrong base or wrong area, not a network problem.
