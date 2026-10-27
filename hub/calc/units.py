"""Units of measurement for calculated tags: dimensions, conversion and compatibility.

Every unit is a dimension (exponents of SI base units) plus a factor and an offset to SI:
``si = value * factor + offset``. The formula engine computes in SI and converts the result to
the unit of the calculated tag, so ``W + kW`` is fine and ``kW + kWh`` is refused.

Temperatures have an offset, so the engine also tracks whether a temperature is absolute
(``°C`` reading) or a difference (``t_supply - t_return``): converting a difference ignores the
offset (a difference of 5 °C is 5 K, not 278.15 K).

Unit strings: atoms from :data:`ATOMS`, an optional exponent (``m2``, ``m^2``, ``m²``), joined
with ``*`` / ``·`` and at most one ``/`` (``kWh/m2``, ``m3/h``, ``kWh/t``). A unit that is not in
the table is still usable: it becomes its own dimension (``kvar`` adds only to ``kvar``), and the
configuration check warns about it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Dim = tuple[tuple[str, int], ...]          # sorted (base, exponent) pairs, exponents non-zero
DIMENSIONLESS: Dim = ()
TEMPERATURE: Dim = (("K", 1),)
TIME: Dim = (("s", 1),)


class UnitError(ValueError):
    pass


def dim_mul(a: Dim, b: Dim, sign: int = 1) -> Dim:
    exps: dict[str, int] = dict(a)
    for base, exp in b:
        exps[base] = exps.get(base, 0) + sign * exp
    return tuple(sorted((k, v) for k, v in exps.items() if v != 0))


def dim_pow(a: Dim, n: int) -> Dim:
    return tuple(sorted((k, v * n) for k, v in a if v * n != 0))


@dataclass(frozen=True)
class Unit:
    symbol: str
    dim: Dim
    factor: float = 1.0
    offset: float = 0.0
    known: bool = True

    def to_si(self, value: float, *, difference: bool = False) -> float:
        return value * self.factor + (0.0 if difference else self.offset)

    def from_si(self, value: float, *, difference: bool = False) -> float:
        return (value - (0.0 if difference else self.offset)) / self.factor


def _d(**exps: int) -> Dim:
    return tuple(sorted((k, v) for k, v in exps.items() if v))


_POWER = _d(kg=1, m=2, s=-3)
_ENERGY = _d(kg=1, m=2, s=-2)
_PRESSURE = _d(kg=1, m=-1, s=-2)
_VOLTAGE = _d(kg=1, m=2, s=-3, A=-1)

# symbol: (dimension, factor to SI, offset to SI)
ATOMS: dict[str, tuple[Dim, float, float]] = {
    "1": (DIMENSIONLESS, 1.0, 0.0), "%": (DIMENSIONLESS, 0.01, 0.0),
    "%RH": (DIMENSIONLESS, 0.01, 0.0), "ppm": (DIMENSIONLESS, 1e-6, 0.0),
    "m": (_d(m=1), 1.0, 0.0), "cm": (_d(m=1), 0.01, 0.0), "mm": (_d(m=1), 0.001, 0.0),
    "km": (_d(m=1), 1000.0, 0.0),
    "l": (_d(m=3), 0.001, 0.0), "L": (_d(m=3), 0.001, 0.0),
    "kg": (_d(kg=1), 1.0, 0.0), "g": (_d(kg=1), 0.001, 0.0), "t": (_d(kg=1), 1000.0, 0.0),
    "s": (TIME, 1.0, 0.0), "min": (TIME, 60.0, 0.0), "h": (TIME, 3600.0, 0.0),
    "d": (TIME, 86400.0, 0.0),
    "K": (TEMPERATURE, 1.0, 0.0), "°C": (TEMPERATURE, 1.0, 273.15),
    "degC": (TEMPERATURE, 1.0, 273.15), "°F": (TEMPERATURE, 5 / 9, 273.15 - 32 * 5 / 9),
    "degF": (TEMPERATURE, 5 / 9, 273.15 - 32 * 5 / 9),
    "A": (_d(A=1), 1.0, 0.0), "mA": (_d(A=1), 0.001, 0.0), "kA": (_d(A=1), 1000.0, 0.0),
    "V": (_VOLTAGE, 1.0, 0.0), "mV": (_VOLTAGE, 0.001, 0.0), "kV": (_VOLTAGE, 1000.0, 0.0),
    "W": (_POWER, 1.0, 0.0), "kW": (_POWER, 1e3, 0.0), "MW": (_POWER, 1e6, 0.0),
    "GW": (_POWER, 1e9, 0.0),
    "J": (_ENERGY, 1.0, 0.0), "kJ": (_ENERGY, 1e3, 0.0), "MJ": (_ENERGY, 1e6, 0.0),
    "GJ": (_ENERGY, 1e9, 0.0), "Wh": (_ENERGY, 3600.0, 0.0), "kWh": (_ENERGY, 3.6e6, 0.0),
    "MWh": (_ENERGY, 3.6e9, 0.0), "GWh": (_ENERGY, 3.6e12, 0.0),
    "Pa": (_PRESSURE, 1.0, 0.0), "kPa": (_PRESSURE, 1e3, 0.0), "MPa": (_PRESSURE, 1e6, 0.0),
    "bar": (_PRESSURE, 1e5, 0.0), "mbar": (_PRESSURE, 100.0, 0.0),
    "psi": (_PRESSURE, 6894.757293168, 0.0),
    "Hz": (_d(s=-1), 1.0, 0.0), "rpm": (_d(s=-1), 1 / 60, 0.0),
}
_SUPERSCRIPT = {"²": 2, "³": 3}
_ATOM_RE = re.compile(r"(?P<atom>.+?)(?:\^(?P<exp>-?\d)|(?P<sup>[²³])|(?P<digit>[23]))?")


def _parse_atom(text: str) -> tuple[Dim, float, float, bool]:
    if text in ATOMS:
        dim, factor, offset = ATOMS[text]
        return dim, factor, offset, True
    m = _ATOM_RE.fullmatch(text)
    if m and m.group("atom") in ATOMS and (m.group("exp") or m.group("sup") or m.group("digit")):
        dim, factor, offset = ATOMS[m.group("atom")]
        n = int(m.group("exp") or m.group("digit") or _SUPERSCRIPT[m.group("sup")])
        if offset:
            raise UnitError(f"{text!r}: a temperature with an offset cannot have an exponent")
        return dim_pow(dim, n), factor ** n, 0.0, True
    return ((f"?{text}", 1),), 1.0, 0.0, False


def parse_unit(symbol: str | None) -> Unit:
    """Parse a unit string; ``None`` / empty = dimensionless."""
    text = (symbol or "").strip()
    if not text:
        return Unit("", DIMENSIONLESS)
    if len(text) > 64:
        raise UnitError("unit too long")
    if text.count("/") > 1:
        raise UnitError(f"{text!r}: at most one '/' (write kWh/m2/a as kWh/(m2*a) is not "
                        "supported; use one denominator)")
    num, _, den = text.partition("/")
    dim: Dim = DIMENSIONLESS
    factor, offset, known, atoms = 1.0, 0.0, True, 0
    for sign, part in ((1, num), (-1, den)):
        if not part and sign == -1:
            continue
        for raw in re.split(r"[*·]", part):
            atom = raw.strip()
            if not atom:
                raise UnitError(f"{text!r}: empty unit part")
            d, f, o, k = _parse_atom(atom)
            atoms += 1
            dim = dim_mul(dim, d, sign)
            factor = factor * f if sign == 1 else factor / f
            offset, known = (o if atoms == 1 and sign == 1 else offset), known and k
    if offset and atoms > 1:
        raise UnitError(f"{text!r}: a temperature with an offset (°C, °F) cannot be combined; "
                        "use K for differences in compound units")
    return Unit(text, dim, factor, offset, known)


_NAMES: dict[Dim, str] = {}


def describe(dim: Dim) -> str:
    """A readable name for error messages: "power (W)", else the SI exponents."""
    if not _NAMES:
        for name, symbol in (("power", "W"), ("energy", "J"), ("temperature", "K"),
                             ("pressure", "Pa"), ("voltage", "V"), ("current", "A"),
                             ("time", "s"), ("frequency", "Hz"), ("volume", "m3"),
                             ("volume flow", "m3/s"), ("mass", "kg"), ("length", "m"),
                             ("specific energy", "J/m3")):
            _NAMES[parse_unit(symbol).dim] = f"{name} ({symbol})"
    if not dim:
        return "dimensionless"
    if dim in _NAMES:
        return _NAMES[dim]
    return "·".join(f"{b.lstrip('?')}{'' if e == 1 else e}" for b, e in dim)
