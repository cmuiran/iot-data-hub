"""Device instances of the platform: identifiers and building the device model.

Identifiers (ADR-003):

* ``device_id``: internal, generated (``d`` + 20 base32 characters), never guessable, never
  shown to buyers; it is the device's key everywhere in the hub;
* ``identifier``: printed on the label, entered by the buyer (``CTRL-8R-0001``), from the
  product's pattern, globally unique (case-insensitive);
* ``serial_number``: the manufacturer's serial, unique per product.

Tag paths of platform devices do not depend on who owns the device or where it is:
``platform.devices.<device_id>.<tag key>``. Location (home, room) is assigned separately, and
ownership changes never rename a tag.
"""

from __future__ import annotations

import base64
import re
import secrets
from typing import Any, Literal

from pydantic import Field, field_validator

from hub.models import DEVICE_ADAPTER, AnyDeviceModel, make_tag_path
from hub.models import _Strict as Strict
from hub.platform.template import ProductTemplate, build_tags

PLATFORM_SITE = "platform"
PLATFORM_AREA = "devices"
MAX_BATCH = 10_000

PRODUCT_CODE_PATTERN = r"^[A-Z0-9][A-Z0-9-]{1,31}$"
IDENTIFIER_PATTERN = r"^[A-Z0-9][A-Z0-9-]{2,63}$"
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)
# prefix, {000…} counter, suffix: CTRL-8R-{0000}
_ID_TEMPLATE_RE = re.compile(r"^(?P<prefix>(?:[A-Z0-9][A-Z0-9-]*)?)\{(?P<zeros>0{1,12})\}"
                             r"(?P<suffix>[A-Z0-9-]*)$")
_DEVICE_ID_RE = re.compile(r"^d[a-z2-7]{20}$")


def new_device_id() -> str:
    """``d`` + 20 random base32 characters (100 bits): a valid tag path segment."""
    return "d" + base64.b32encode(secrets.token_bytes(15)).decode("ascii").lower()[:20]


def is_platform_device_id(value: str) -> bool:
    return bool(_DEVICE_ID_RE.fullmatch(value))


def normalize_identifier(value: str) -> str:
    """What a buyer types: surrounding spaces removed, upper case."""
    return value.strip().upper()


def validate_identifier(value: str) -> str:
    value = normalize_identifier(value)
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{value!r} is not a device identifier (A-Z, 0-9 and '-', 3-64 "
                         "characters, starting with a letter or digit)")
    return value


def validate_identifier_pattern(value: str) -> str:
    m = _ID_TEMPLATE_RE.fullmatch(value)
    if m is None:
        raise ValueError(f"{value!r} is not an identifier pattern: use upper-case letters, digits "
                         "and '-' around one counter such as {0000} (CTRL-8R-{0000})")
    length = len(m["prefix"]) + len(m["zeros"]) + len(m["suffix"])
    if not 3 <= length <= 64:
        raise ValueError("identifiers from this pattern must be 3-64 characters long")
    return value


def identifiers(pattern: str, start: int, count: int) -> list[str]:
    """``count`` identifiers from ``pattern`` starting at ``start`` (CTRL-8R-{0000}, 1, 3 ->
    CTRL-8R-0001 .. CTRL-8R-0003). The counter never grows past its width."""
    m = _ID_TEMPLATE_RE.fullmatch(validate_identifier_pattern(pattern))
    if m is None:                       # (validate_identifier_pattern already refused it)
        raise ValueError(f"{pattern!r} is not an identifier pattern")
    width = len(m["zeros"])
    if start < 0 or not 1 <= count <= MAX_BATCH:
        raise ValueError(f"start must be >= 0 and count 1-{MAX_BATCH}")
    last = start + count - 1
    if last >= 10**width:
        raise ValueError(f"the counter of {pattern} has {width} digits: {last} does not fit")
    return [f"{m['prefix']}{n:0{width}d}{m['suffix']}" for n in range(start, last + 1)]


def _substitute(value: Any, names: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in names.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, dict):
        return {k: _substitute(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, names) for v in value]
    return value


def build_device(*, device_id: str, name: str, template: ProductTemplate, protocol: str,
                 connection: dict[str, Any], options: dict[str, Any] | None = None,
                 identifier: str = "", serial: str = "", enabled: bool = True,
                 description: str | None = None) -> AnyDeviceModel:
    """The device model of one platform device: template tags with the protocol binding,
    binding defaults merged with the device's own connection and options. Raises ValueError
    (pydantic's ValidationError) when the result is not a valid device."""
    if protocol not in template.bindings:
        raise ValueError(f"the product has no {protocol} binding "
                         f"(it has {', '.join(sorted(template.bindings))})")
    binding = template.bindings[protocol]
    names = {"device_id": device_id, "identifier": identifier, "serial": serial}
    data: dict[str, Any] = _substitute(binding.device | (options or {}), names)
    data |= {
        "device_id": device_id, "name": name, "site": PLATFORM_SITE, "area": PLATFORM_AREA,
        "protocol": protocol, "enabled": enabled, "description": description,
        "connection": _substitute(binding.connection | connection, names),
        "tags": [t.model_dump(exclude_unset=True) for t in build_tags(template, protocol)],
    }
    return DEVICE_ADAPTER.validate_python(data)


def platform_tag_path(device_id: str, key: str) -> str:
    return make_tag_path(PLATFORM_SITE, PLATFORM_AREA, device_id, key)


class ProductIn(Strict):
    code: str = Field(pattern=PRODUCT_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    image_ref: str | None = Field(default=None, max_length=500)
    identifier_pattern: str
    kind: Literal["device", "edge_hub"] = "device"   # an edge hub has devices behind it
    catalog: bool = False        # any buyer may add it behind an edge hub (third-party gear)

    @field_validator("identifier_pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        return validate_identifier_pattern(value)
