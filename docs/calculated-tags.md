# Calculated tags (BMS phase 1)

A calculated tag is computed by the hub from other tags. It lives in a device with
`protocol: calc` (example: `config/devices/calc_kish.yaml`) and is an ordinary, read-only tag:
history, alarms, dashboards, reports, northbound outputs and the building model treat it like
any other. Code: `hub/calc/` (`units.py`, `expr.py`, `engine.py`), `hub/drivers/calc.py`.

```yaml
device_id: calc_kish
name: Site calculations
site: kish
area: energy
protocol: calc
tags:
  - name: site_power
    unit: kW
    data_type: float64          # float64 / float32, or bool for conditions
    formula: grid + solar
    inputs:
      grid: kish.substation.meter_01.active_power
      solar: kish.solar_roof.inverter_01.ac_power
    missing: bad                # bad | skip (aggregates leave missing inputs out, result uncertain)
    stale_s: 300                # an input older than this counts as missing
    min_interval_s: 1           # at most one result per second
    period_s: 10                # evaluate at least this often
    max_gap_s: 300              # integrate/runtime: longer gaps are not counted
```

## Formula language

| Element | Examples |
|---|---|
| Inputs | an alias from `inputs` (`grid`), or a full tag path (`kish.substation.meter_01.active_power`) |
| Numbers | `1000`, `0.5`; with a unit: `5[K]`, `20[°C]`, `0[m3/h]`, `1[kW]`. A bare number is dimensionless, except `0` |
| Arithmetic | `+ - * /`, parentheses |
| Comparison / logic | `< <= > >= == !=`, `and`, `or`, `not`, `true`, `false` (no chained comparisons) |
| Functions | `abs(x)`, `min(…)`, `max(…)`, `avg(…)`, `sum(…)`, `if(condition, a, b)` |
| Building aggregates | `sum_of("building_a", "active_power")`, `avg_of`, `min_of`, `max_of`, `count_of`: every point with that role in the location and below (retired equipment excluded) |
| Running totals (whole formula only) | `integrate(power)` → energy, `integrate(flow)` → volume; `runtime(condition)` → running time. They continue after a restart from the last stored result |

Typical formulas: total power `p1 + p2 + p3`; temperature difference `t_return - t_supply`
(unit `K`); efficiency `p_out / p_in` (unit `%`); specific energy
`if(flow > 0[m3/h], power / flow, 0)` (unit `kWh/m3`); COP `q_cool / p_elec`; running hours
`runtime(pump_on)` (unit `h`); floor total `sum_of("floor_3", "active_power")`.

## Safety

The formula is tokenized and parsed by hand into a small syntax tree and compiled into closures.
There is no `eval`, no Python `ast`, no attribute access and no import: the only callable names
are the functions above (`tests/calc/test_expr.py` checks this, and fuzzes 3000 random inputs).
Limits: 2000 characters, 1000 tokens, nesting depth 40, 200 tags per formula, 500 points per
aggregate.

## Units and data types

Units come from the tags (`unit:`). The engine computes in SI and converts to the calculated
tag's unit: `W + kW + MW` is fine, `kW + kWh` is refused when the configuration is loaded, and
the result must match the tag's unit (`kWh` from `integrate(kW)`, `kWh/m3` from `kW / (m3/h)`).
Temperatures: `°C - °C` is a difference (5 °C = 5 K), two absolute temperatures cannot be added,
`avg()` of temperatures is allowed. A unit that is not in the table (`kvar`, `mg/l`) is its own
dimension: it combines only with itself, and the configuration check warns. String tags cannot
be inputs. Bool inputs are conditions.

## Quality

| Situation | Result |
|---|---|
| all inputs good and fresh | good |
| an input uncertain, or `missing: skip` left one out, or a running total had a gap | uncertain |
| an input missing, bad, or older than `stale_s` | bad, reason `input_bad` / `stale` |
| division by zero, non-finite result | bad, reason `math_error` |

## Dependencies

Calculated tags may use other calculated tags; they are evaluated in dependency order within one
pass. A cycle (`a → b → a`, or a formula that reads its own result) is refused with the path of
the cycle.

## Versions, preview, publish

* Every definition has a version hash (formula, inputs, unit, data type, missing, stale_s,
  max_gap_s). `calc_versions` stores each version with the time it came into force, so every
  stored result can be traced to its formula (`GET /api/calc/tags/{tag}/versions`).
* `POST /api/calc/preview` evaluates a definition once against the live values and stores
  nothing (only tags the user may read).
* Publishing is saving the calc device through `/api/config/devices` (validated on a copy of the
  configuration first, audited, rollback in the configuration history). It needs `config.edit`
  **and** `calc.publish`.
* Dashboard: **Calculated tags** page (list, live values, versions, editor with preview).
