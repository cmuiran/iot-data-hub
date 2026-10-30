"""Product templates: the tags of a product and how each protocol addresses them.

A template version is defined once per product version and stored as JSON (product_versions).
Tags are protocol independent (key, kind, type, unit, range, access, UI hints); a *binding* per
protocol adds the protocol address of every tag (Modbus register, MQTT JSON path, OPC UA NodeId,
SNMP OID, ...) plus connection defaults. A binding tag is validated with the protocol's own tag
model (``ModbusTag``, ``MqttTag``, ...), so a template is checked by exactly the rules the YAML
configuration uses.

Rules on top of the device models:

* tag keys are stable identifiers: ``display_name`` changes, the key never does;
* inputs (digital/analog inputs, variables) are read-only; only outputs and parameters may be
  ``read_write`` (and then need min/max like any writable tag; default is read-only);
* every binding maps every tag, and may not override what the template defines (type, unit,
  range, access).
"""

from __future__ import annotations

import math
import re
from enum import StrEnum
from functools import cache
from typing import Annotated, Any, Literal, get_args

from pydantic import Field, ValidationError, field_validator, model_validator

from hub.models import (
    INTEGER_RANGES,
    NUMERIC_TYPES,
    Access,
    AnyDeviceModel,
    DataType,
    Segment,
    TagBase,
    WriteClass,
    _Strict,
)

MAX_TAGS = 2000

# Fields a template tag defines; a binding may not set them.
TEMPLATE_TAG_FIELDS = frozenset({"name", "description", "unit", "data_type", "access",
                                 "write_class", "min", "max", "states"})
# Device fields the platform sets itself; a binding's ``device`` options may not set them.
RESERVED_DEVICE_FIELDS = frozenset({"device_id", "name", "site", "area", "protocol", "enabled",
                                    "description", "connection", "tags"})


@cache
def device_classes() -> dict[str, type[AnyDeviceModel]]:
    """protocol -> device model, for every protocol a product can be bound to (not calc)."""
    out: dict[str, type[AnyDeviceModel]] = {}
    for cls in get_args(AnyDeviceModel):
        (protocol,) = get_args(cls.model_fields["protocol"].annotation)
        if protocol != "calc":
            out[protocol] = cls
    return out


def tag_model(protocol: str) -> type[TagBase]:
    """The tag model of a protocol (``MqttTag`` for mqtt, ...)."""
    cls = device_classes()[protocol]
    (inner,) = get_args(cls.model_fields["tags"].annotation)
    return inner


class TagKind(StrEnum):
    DIGITAL_INPUT = "digital_input"
    DIGITAL_OUTPUT = "digital_output"
    ANALOG_INPUT = "analog_input"
    ANALOG_OUTPUT = "analog_output"
    VARIABLE = "variable"          # a measured / reported value that is not a physical input
    PARAMETER = "parameter"        # a setting of the device (mode, limit, ...)
    CALCULATED = "calculated"      # computed by the hub from the device's other tags (formula)


READ_ONLY_KINDS = frozenset({TagKind.DIGITAL_INPUT, TagKind.ANALOG_INPUT, TagKind.VARIABLE,
                             TagKind.CALCULATED})
DIGITAL_KINDS = frozenset({TagKind.DIGITAL_INPUT, TagKind.DIGITAL_OUTPUT})
ANALOG_KINDS = frozenset({TagKind.ANALOG_INPUT, TagKind.ANALOG_OUTPUT})

Text = Annotated[str, Field(min_length=1, max_length=80)]


class TagUi(_Strict):
    """How apps show the tag (the dashboard builds cards, controls and charts from this)."""

    visible: bool = True
    chart: bool = True
    scenario: bool = True          # may be used in automations
    order: int = Field(default=0, ge=0, le=100_000)
    group: str | None = Field(default=None, min_length=1, max_length=40)
    icon: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    # the tags of one air conditioner / thermostat (the same ``group``, or the device's only
    # set): the app shows them as one climate panel (on/off, mode, set point, fan speed, room)
    role: Literal["climate_power", "climate_mode", "climate_setpoint", "climate_fan",
                  "climate_temp"] | None = None


class TagDefinition(_Strict):
    key: Segment
    kind: TagKind
    display_name: dict[Literal["fa", "en"], Text] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=500)
    data_type: DataType
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    access: Access = Access.READ
    write_class: WriteClass | None = None
    min: float | None = None
    max: float | None = None
    states: dict[int, Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=64, description="labels of enum values / bool states")
    default: bool | int | float | str | None = None
    formula: str | None = Field(default=None, min_length=1, max_length=2000, description=(
        "calculated tags: the formula over the device's other tag keys, in the hub's formula "
        "language (power * 1000, integrate(power) for energy, if(temperature > 30[°C], 1, 0))"))
    ui: TagUi = TagUi()

    def common(self) -> dict[str, Any]:
        """The protocol-independent tag fields, as the device models name them."""
        out: dict[str, Any] = {"name": self.key, "data_type": self.data_type,
                               "access": self.access}
        for name in ("description", "unit", "write_class", "min", "max", "states"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out

    @model_validator(mode="after")
    def _check(self) -> TagDefinition:
        if self.kind in DIGITAL_KINDS and self.data_type is not DataType.BOOL:
            raise ValueError(f"{self.kind} tags are bool, not {self.data_type}")
        if self.kind in ANALOG_KINDS and self.data_type not in NUMERIC_TYPES:
            raise ValueError(f"{self.kind} tags are numeric, not {self.data_type}")
        if self.kind in READ_ONLY_KINDS and self.access is Access.READ_WRITE:
            raise ValueError(f"{self.kind} tags are read-only; only outputs and parameters "
                             "may be read_write")
        if (self.kind is TagKind.CALCULATED) != (self.formula is not None):
            raise ValueError("a calculated tag needs a formula; only calculated tags have one")
        if self.kind is TagKind.CALCULATED and self.data_type not in (
                DataType.FLOAT64, DataType.FLOAT32, DataType.BOOL):
            raise ValueError("calculated tags are float64, float32 or bool")
        try:   # the generic tag rules (min/max for writable tags, states, integer ranges, ...)
            TagBase.model_validate(self.common())
        except ValidationError as exc:
            raise ValueError(_first_error(exc)) from None
        self._check_default()
        return self

    def _check_default(self) -> None:
        value = self.default
        if value is None:
            return
        if self.data_type is DataType.BOOL:
            ok = isinstance(value, bool)
        elif self.data_type is DataType.STRING:
            ok = isinstance(value, str)
        elif self.data_type in INTEGER_RANGES:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, int | float) and not isinstance(value, bool) \
                and math.isfinite(value)
        if not ok:
            raise ValueError(f"default {value!r} is not a {self.data_type} value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            if self.min is not None and value < self.min or \
                    self.max is not None and value > self.max:
                raise ValueError(f"default {value!r} is outside min/max")
            if self.states and self.data_type in INTEGER_RANGES and value not in self.states:
                raise ValueError(f"default {value!r} is not one of the states")


class ProtocolBinding(_Strict):
    """How one protocol reaches a product's tags.

    ``connection``: defaults merged under each device's own connection (a device adds what is
    specific to it: host, unit id, ...); strings may contain ``{device_id}``, ``{identifier}`` and
    ``{serial}`` (e.g. ``telemetry_topic: d/{device_id}/telemetry``).
    ``device``: device-level options of the protocol (``poll_interval_ms``, ...).
    ``tags``: tag key -> protocol address fields (plus scale/offset/history options).
    """

    connection: dict[str, Any] = Field(default_factory=dict)
    device: dict[str, Any] = Field(default_factory=dict)
    tags: dict[Segment, dict[str, Any]]

    @field_validator("device")
    @classmethod
    def _no_reserved(cls, value: dict[str, Any]) -> dict[str, Any]:
        bad = sorted(set(value) & RESERVED_DEVICE_FIELDS)
        if bad:
            raise ValueError(f"device options may not set {', '.join(bad)}")
        return value


class CommandParam(_Strict):
    tag: Segment
    value: bool | int | float | None = None     # None: the user enters it when sending


class CommandDefinition(_Strict):
    """A command that sets several parameters at once (e.g. "eco mode": mode = 2 and
    setpoint = 24), each through the control-safety layer in order."""

    key: Segment
    display_name: dict[Literal["fa", "en"], Text] = Field(min_length=1)
    icon: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    params: list[CommandParam] = Field(min_length=1, max_length=20)


class ProductTemplate(_Strict):
    tags: list[TagDefinition] = Field(min_length=1, max_length=MAX_TAGS)
    bindings: dict[str, ProtocolBinding] = Field(min_length=1)
    commands: list[CommandDefinition] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _check(self) -> ProductTemplate:
        keys = [t.key for t in self.tags]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(f"duplicate tag keys: {', '.join(duplicates)}")
        measured = [t.key for t in self.tags if t.kind is not TagKind.CALCULATED]
        known = device_classes()
        for protocol, binding in self.bindings.items():
            if protocol not in known:
                raise ValueError(f"unknown protocol {protocol!r} (one of {', '.join(known)})")
            missing = [k for k in measured if k not in binding.tags]
            extra = sorted(set(binding.tags) - set(measured))
            if missing:
                raise ValueError(f"binding {protocol}: no address for {', '.join(missing)}")
            if extra:
                raise ValueError(f"binding {protocol}: unknown or calculated tags "
                                 f"{', '.join(extra)}")
            build_tags(self, protocol)          # raises with the binding/tag that is wrong
        self._check_formulas()
        by_key = {t.key: t for t in self.tags}
        for c in self.commands:
            for prm in c.params:
                tag = by_key.get(prm.tag)
                if tag is None or tag.access is not Access.READ_WRITE:
                    raise ValueError(f"command {c.key}: {prm.tag} is not a controllable tag")
        ckeys = [c.key for c in self.commands]
        if len(set(ckeys)) != len(ckeys):
            raise ValueError("duplicate command keys")
        self._check_climate()
        return self

    def _check_climate(self) -> None:
        """A climate role is a promise to the app (it shows a control for it), so the tag must
        be able to do it: power a controllable bool, mode and fan controllable with their
        states, the set point a controllable number with its range, the room a number; one tag
        per role in each group."""
        seen: set[tuple[str | None, str]] = set()
        for t in self.tags:
            role = t.ui.role
            if role is None:
                continue
            if (t.ui.group, role) in seen:
                raise ValueError(f"{t.key}: a second {role} in the same group")
            seen.add((t.ui.group, role))
            writable = t.access is Access.READ_WRITE
            numeric = t.data_type in NUMERIC_TYPES
            if role == "climate_power":
                ok = writable and t.data_type is DataType.BOOL
                need = "a controllable bool"
            elif role in ("climate_mode", "climate_fan"):
                ok = writable and t.data_type in INTEGER_RANGES and bool(t.states)
                need = "a controllable integer with its states (0=cool, 1=heat, ...)"
            elif role == "climate_setpoint":
                ok = writable and numeric and t.min is not None and t.max is not None
                need = "a controllable number with min and max"
            else:
                ok = numeric
                need = "a number"
            if not ok:
                raise ValueError(f"{t.key}: {role} needs {need}")

    def _check_formulas(self) -> None:
        calc = calc_device(self, "dtemplatecheck000000000", "check")
        if calc is None:
            return
        from hub.calc.engine import compile_calcs

        catalogue = {f"{calc.site}.{calc.area}.{calc.device_id}.{t.key}": TagBase.model_validate(
            t.common()) for t in self.tags if t.kind is not TagKind.CALCULATED}
        catalogue |= {calc.tag_path(t.name): t for t in calc.tags}
        _, _, issues = compile_calcs([calc], catalogue)
        errors = [i for i in issues if i.error]
        if errors:
            raise ValueError(f"{errors[0].location}: {errors[0].message}")

    def tag(self, key: str) -> TagDefinition:
        for t in self.tags:
            if t.key == key:
                return t
        raise KeyError(key)


def build_tags(template: ProductTemplate, protocol: str) -> list[TagBase]:
    """The protocol's tag models for a template (validated)."""
    binding = template.bindings[protocol]
    model = tag_model(protocol)
    out: list[TagBase] = []
    for t in template.tags:
        if t.kind is TagKind.CALCULATED:        # the hub computes it: no protocol address
            continue
        address = binding.tags[t.key]
        overridden = sorted(set(address) & TEMPLATE_TAG_FIELDS)
        if overridden:
            raise ValueError(f"binding {protocol}, tag {t.key}: {', '.join(overridden)} "
                             "come from the template, not the binding")
        try:
            out.append(model.model_validate(t.common() | address))
        except ValidationError as exc:
            raise ValueError(f"binding {protocol}, tag {t.key}: {_first_error(exc)}") from None
    return out


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    message = str(err.get("msg", "invalid")).removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


def diff_templates(old: ProductTemplate, new: ProductTemplate,
                   protocol: str | None = None) -> dict[str, Any]:
    """What changes for a device moving from ``old`` to ``new`` (for one protocol, if given).

    ``breaking``: a tag disappears, changes its data type, or stops being writable (history,
    automations or apps relying on it would break), or the protocol has no binding any more.
    Display names, units, ranges, UI hints and addresses are not breaking.
    """
    old_tags = {t.key: t for t in old.tags}
    new_tags = {t.key: t for t in new.tags}
    added = sorted(set(new_tags) - set(old_tags))
    removed = sorted(set(old_tags) - set(new_tags))
    changed: list[dict[str, Any]] = []
    breaking_changes: list[str] = []
    for key in sorted(set(old_tags) & set(new_tags)):
        a, b = old_tags[key], new_tags[key]
        fields = [f for f in ("kind", "data_type", "unit", "access", "write_class", "min",
                              "max", "states", "display_name", "ui")
                  if getattr(a, f) != getattr(b, f)]
        if protocol and protocol in old.bindings and protocol in new.bindings and \
                old.bindings[protocol].tags.get(key) != new.bindings[protocol].tags.get(key):
            fields.append("address")
        if fields:
            changed.append({"key": key, "fields": fields})
        if "data_type" in fields:
            breaking_changes.append(f"{key}: data type {a.data_type} -> {b.data_type}")
        if a.access is Access.READ_WRITE and b.access is not Access.READ_WRITE:
            breaking_changes.append(f"{key}: no longer writable")
    breaking_changes += [f"{key}: removed" for key in removed]
    if protocol and protocol in old.bindings and protocol not in new.bindings:
        breaking_changes.append(f"the {protocol} binding was removed")
    return {"added": added, "removed": removed, "changed": changed,
            "breaking": bool(breaking_changes), "breaking_changes": breaking_changes}


# Platform devices report on change (and a heartbeat at least hourly), so a reading holds
# until the next one: a formula keeps using it, and a running total keeps counting, for an hour.
HELD_S = 3600


def calc_device(template: ProductTemplate, device_id: str, name: str) -> Any | None:
    """The calculated tags of one device as a hub calc device with the device's own id, so
    their paths are the device's (``platform.devices.<id>.<key>``); None if there are none.
    A formula names the device's other tags by their keys."""
    from hub.models import CALC_ALIAS_PATTERN, CalcDevice, CalcTag
    from hub.platform.devices import PLATFORM_AREA, PLATFORM_SITE

    calculated = [t for t in template.tags if t.kind is TagKind.CALCULATED]
    if not calculated:
        return None
    alias = re.compile(CALC_ALIAS_PATTERN)
    prefix = f"{PLATFORM_SITE}.{PLATFORM_AREA}.{device_id}."
    inputs = {t.key: prefix + t.key for t in template.tags if alias.fullmatch(t.key)}
    tags = []
    for t in calculated:
        used = {k: v for k, v in inputs.items()
                if k != t.key and re.search(rf"\b{re.escape(k)}\b", t.formula or "")}
        tags.append(CalcTag.model_validate(
            {"name": t.key, "data_type": t.data_type, "unit": t.unit,
             "description": t.description, "formula": t.formula, "inputs": used,
             "missing": "skip", "stale_s": HELD_S, "max_gap_s": HELD_S}))
    return CalcDevice.model_validate({"device_id": device_id, "name": name,
                                      "site": PLATFORM_SITE, "area": PLATFORM_AREA,
                                      "protocol": "calc", "enabled": True, "tags": tags})


def calc_rows(template: ProductTemplate) -> list[TagBase]:
    """Rows for the tags table (history, alarms) of the calculated tags: the same for every
    device of the version."""
    return [TagBase.model_validate(t.common()) for t in template.tags
            if t.kind is TagKind.CALCULATED]
