"""Unit table: parsing, dimensions, conversion."""

from __future__ import annotations

import pytest

from hub.calc.units import DIMENSIONLESS, TEMPERATURE, UnitError, parse_unit


def test_power_and_energy_share_dimensions_with_their_multiples() -> None:
    assert parse_unit("kW").dim == parse_unit("W").dim == parse_unit("MW").dim
    assert parse_unit("kWh").dim == parse_unit("J").dim
    assert parse_unit("kW").dim != parse_unit("kWh").dim
    assert parse_unit("kWh").to_si(1) == pytest.approx(3.6e6)
    assert parse_unit("MW").from_si(2.5e6) == pytest.approx(2.5)


@pytest.mark.parametrize("text", ["m3", "m^3", "m³"])
def test_exponents(text: str) -> None:
    assert parse_unit(text).dim == parse_unit("l").dim
    assert parse_unit(text).factor == pytest.approx(1.0)


def test_compound_units() -> None:
    flow = parse_unit("m3/h")
    assert flow.factor == pytest.approx(1 / 3600)
    specific = parse_unit("kWh/m3")
    assert specific.dim == parse_unit("J/m3").dim
    assert specific.to_si(1) == pytest.approx(3.6e6)
    assert parse_unit("kW/m2").dim == parse_unit("W/m²").dim


def test_temperature_offsets() -> None:
    c = parse_unit("°C")
    assert c.dim == TEMPERATURE and c.to_si(20) == pytest.approx(293.15)
    assert c.to_si(5, difference=True) == pytest.approx(5)
    assert parse_unit("degC").to_si(0) == pytest.approx(273.15)
    assert parse_unit("°F").to_si(212) == pytest.approx(373.15)
    with pytest.raises(UnitError):
        parse_unit("°C/h")


def test_dimensionless_and_percent() -> None:
    assert parse_unit(None).dim == DIMENSIONLESS
    assert parse_unit("%").dim == DIMENSIONLESS and parse_unit("%").factor == 0.01


def test_unknown_unit_is_its_own_dimension() -> None:
    kvar = parse_unit("kvar")
    assert not kvar.known
    assert kvar.dim == parse_unit("kvar").dim != parse_unit("kW").dim
    assert parse_unit("mg/l").dim != parse_unit("kg/m3").dim   # mg is not in the table


def test_malformed() -> None:
    with pytest.raises(UnitError):
        parse_unit("kWh/m2/a")
    with pytest.raises(UnitError):
        parse_unit("kW*")
    with pytest.raises(UnitError):
        parse_unit("x" * 65)
