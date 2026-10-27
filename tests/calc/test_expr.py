"""Formula language: parsing, type/unit checks, evaluation, refusal of hostile input."""

from __future__ import annotations

import random
import re
import string
from datetime import UTC, datetime

import pytest

from hub.building import BuildingModel
from hub.calc.expr import (
    MISSING,
    CalcError,
    CalcMathError,
    Context,
    compile_formula,
    output_for,
    tokenize,
)
from hub.models import BuildingFile, CalcTag, DataType, Quality, Sample, TagBase

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def tag(unit: str | None = None, data_type: str = "float32") -> TagBase:
    return TagBase(name="x", unit=unit, data_type=DataType(data_type))


CATALOGUE = {
    "s.a.m1.p": tag("kW"), "s.a.m2.p": tag("W"), "s.a.m3.p": tag("MW"),
    "s.a.m1.e": tag("kWh"), "s.a.t.supply": tag("°C"), "s.a.t.ret": tag("degC"),
    "s.a.f.flow": tag("m3/h"), "s.a.p.run": tag(data_type="bool"),
    "s.a.p.mode": tag(data_type="string"), "s.a.q.kvar": tag("kvar"),
    "s.a.x.ratio": tag(),
}
INPUTS = {"p1": "s.a.m1.p", "p2": "s.a.m2.p", "p3": "s.a.m3.p", "e1": "s.a.m1.e",
          "ts": "s.a.t.supply", "tr": "s.a.t.ret", "flow": "s.a.f.flow", "run": "s.a.p.run",
          "mode": "s.a.p.mode", "q": "s.a.q.kvar", "r": "s.a.x.ratio"}


class Values(Context):
    def __init__(self, **by_alias: object) -> None:
        super().__init__()
        self.v = {INPUTS[k]: val for k, val in by_alias.items()}

    def raw(self, path: str) -> object:
        return self.v.get(path, MISSING)


def run(formula: str, out_unit: str | None = None, out_type: str = "float64",
        skip: bool = False, **values: object) -> object:
    used = {k: v for k, v in INPUTS.items() if k in formula}
    compiled = compile_formula(formula, inputs=used, catalogue=CATALOGUE, skip_missing=skip)
    out = output_for(compiled, CalcTag(name="out", unit=out_unit, data_type=DataType(out_type),
                                       formula=formula))
    raw = compiled.fn(Values(**values))
    return raw if raw is MISSING else out.convert(raw)


def test_sum_of_feeders_in_mixed_units() -> None:
    assert run("p1 + p2 + p3", "kW", p1=100, p2=500, p3=0.2) == pytest.approx(300.5)
    assert run("sum(p1, p2, p3)", "MW", p1=100, p2=500, p3=0.2) == pytest.approx(0.3005)


def test_average_min_max() -> None:
    assert run("avg(p1, p2)", "W", p1=1, p2=500) == pytest.approx(750)
    assert run("min(p1, p2)", "kW", p1=1, p2=500) == pytest.approx(0.5)
    assert run("max(ts, tr)", "°C", ts=7, tr=12) == pytest.approx(12)


def test_temperature_difference() -> None:
    assert run("tr - ts", "K", ts=7, tr=12) == pytest.approx(5)
    assert run("tr - ts", "°C", ts=7, tr=12) == pytest.approx(5)     # a difference, no offset
    assert run("ts + 2[K]", "°C", ts=7) == pytest.approx(9)
    assert run("avg(ts, tr)", "°C", ts=7, tr=12) == pytest.approx(9.5)
    assert run("tr - ts > 4[K]", None, "bool", ts=7, tr=12) is True


def test_specific_energy_and_efficiency() -> None:
    # 180 kW / 60 m3/h = 3 kWh/m3
    assert run("p1 / flow", "kWh/m3", p1=180, flow=60) == pytest.approx(3)
    assert run("p2 / p1", "%", p1=10, p2=9500) == pytest.approx(95)
    assert run("if(flow > 0[m3/h], p1 / flow, 0)", "kWh/m3", p1=1, flow=0) == 0


def test_conditions() -> None:
    assert run("run and p1 > 0", None, "bool", run=True, p1=3) is True
    assert run("not run or p1 >= 5[kW]", None, "bool", run=True, p1=3) is False
    assert run("if(run, p1, 0)", "kW", run=False, p1=3) == 0


def test_full_tag_paths_work_without_inputs() -> None:
    compiled = compile_formula("s.a.m1.p * 2", inputs={}, catalogue=CATALOGUE)
    assert compiled.refs == {"s.a.m1.p"}


@pytest.mark.parametrize(("formula", "message"), [
    ("p1 + e1", "units do not match"),
    ("ts + tr", "cannot add two temperatures"),
    ("sum(ts, tr)", "cannot add temperatures"),
    ("p1 + 5", "units do not match"),
    ("p1 > 5[kWh]", "units do not match"),
    ("run + 1", "needs numbers"),
    ("mode", "string tag"),
    ("p1 + q", "units do not match"),
    ("if(p1, 1, 2)", "must be a condition"),
    ("unknown_alias", "unknown name"),
    ("s.a.zz.p", "unknown tag"),
    ("p1 + integrate(p1)", "must be the whole formula"),
    ("runtime(p1)", "needs a condition"),
    ("integrate(ts)", "needs a rate"),
    ("1 < 2 < 3", "cannot be chained"),
    ("5[parsec]", "unknown unit"),
    ("-ts", "cannot negate"),
    ("open('x')", "unknown function"),
    ("p1.__class__", "unexpected"),
    ("p1 ** 2", "unexpected"),
    ("__import__", "unexpected character"),
    ("p1; p2", "unexpected character"),
    ("", "unexpected"),
])
def test_refused(formula: str, message: str) -> None:
    with pytest.raises(CalcError, match=message):
        compile_formula(formula, inputs=INPUTS, catalogue=CATALOGUE)


def test_output_unit_must_match() -> None:
    compiled = compile_formula("p1 + p2", inputs=INPUTS, catalogue=CATALOGUE)
    with pytest.raises(CalcError, match="kWh"):
        output_for(compiled, CalcTag(name="o", unit="kWh", data_type=DataType.FLOAT64,
                                     formula="x"))
    with pytest.raises(CalcError, match="set unit"):
        output_for(compiled, CalcTag(name="o", data_type=DataType.FLOAT64, formula="x"))
    with pytest.raises(CalcError, match="bool"):
        output_for(compiled, CalcTag(name="o", data_type=DataType.BOOL, formula="x"))


def test_missing_inputs_and_skip() -> None:
    assert run("p1 + p2", "kW", p1=1) is MISSING
    assert run("sum(p1, p2)", "kW", p1=1) is MISSING
    ctx = Values(p1=1)
    compiled = compile_formula("sum(p1, p2)", inputs=INPUTS, catalogue=CATALOGUE,
                               skip_missing=True)
    assert compiled.fn(ctx) == pytest.approx(1000) and ctx.uncertain
    assert run("sum(p1, p2)", "kW", skip=True) is MISSING          # nothing left


def test_division_by_zero_is_an_error_not_a_crash() -> None:
    with pytest.raises(CalcMathError):
        run("p1 / p2", "1", p1=1, p2=0)


def test_unknown_unit_combines_with_itself_and_warns() -> None:
    compiled = compile_formula("q + q", inputs={"q": "s.a.q.kvar"}, catalogue=CATALOGUE)
    assert any("kvar" in w for w in compiled.warnings)
    output_for(compiled, CalcTag(name="o", unit="kvar", data_type=DataType.FLOAT64,
                                 formula="x"))


def test_unused_inputs_warn() -> None:
    compiled = compile_formula("p1", inputs={"p1": "s.a.m1.p", "p2": "s.a.m2.p"},
                               catalogue=CATALOGUE)
    assert compiled.warnings == ["inputs not used in the formula: p2"]


def test_building_aggregates() -> None:
    model = BuildingModel(BuildingFile.model_validate({
        "locations": [
            {"id": "c", "kind": "campus", "name": "C", "site": "s"},
            {"id": "b", "kind": "building", "name": "B", "parent": "c"},
            {"id": "f1", "kind": "floor", "name": "F1", "parent": "b"},
            {"id": "f2", "kind": "floor", "name": "F2", "parent": "b"},
        ],
        "equipment": [
            {"id": "m1", "name": "M1", "type": "meter", "location": "f1",
             "points": [{"role": "power", "tag": "s.a.m1.p"}]},
            {"id": "m2", "name": "M2", "type": "meter", "location": "f2",
             "points": [{"role": "power", "tag": "s.a.m2.p"}]},
            {"id": "old", "name": "Old", "type": "meter", "location": "f2", "status": "retired",
             "points": [{"role": "power", "tag": "s.a.m3.p"}]},
        ]}))
    compiled = compile_formula('sum_of("b", "power")', inputs={}, catalogue=CATALOGUE,
                               building=model)
    assert compiled.refs == {"s.a.m1.p", "s.a.m2.p"}        # retired equipment left out
    assert compiled.fn(Values(p1=2, p2=500)) == pytest.approx(2500)
    floor = compile_formula('count_of("f1", "power")', inputs={}, catalogue=CATALOGUE,
                            building=model)
    assert floor.fn(Values(p1=2)) == 1
    with pytest.raises(CalcError, match="no point"):
        compile_formula('sum_of("b", "flow")', inputs={}, catalogue=CATALOGUE, building=model)
    with pytest.raises(CalcError, match="unknown location"):
        compile_formula('sum_of("x", "power")', inputs={}, catalogue=CATALOGUE, building=model)


def test_limits() -> None:
    with pytest.raises(CalcError, match="nested too deeply"):
        compile_formula("(" * 60 + "p1" + ")" * 60, inputs=INPUTS, catalogue=CATALOGUE)
    with pytest.raises(CalcError, match="nested too deeply"):
        compile_formula("-" * 60 + "p1", inputs=INPUTS, catalogue=CATALOGUE)
    with pytest.raises(CalcError, match="too long"):
        tokenize("p1 + " * 600)


def test_fuzz_never_raises_anything_but_calc_error() -> None:
    rng = random.Random(1405)
    alphabet = string.ascii_letters + string.digits + " +-*/()[],.<>=!'\"_%°\\;:{}@#$&|^~`"
    pieces = ["p1", "p2", "ts", "run", "sum(", "if(", "integrate(", "5[kW]", "and", "or",
              "not", ")", ",", "(", "+", "-", "/", "*", ">", "==", '"b"', "__", "lambda"]
    for _ in range(3000):
        if rng.random() < 0.5:
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        else:
            text = " ".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        try:
            compiled = compile_formula(text, inputs=INPUTS, catalogue=CATALOGUE)
        except CalcError:
            continue
        try:
            compiled.fn(Values(p1=1, p2=2, ts=20, run=True))
        except CalcMathError:
            pass


def test_no_eval_in_the_calc_package() -> None:
    from pathlib import Path

    import hub.calc

    for path in Path(hub.calc.__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in (r"(?<![\w.])eval\(", r"(?<![\w.])exec\(", r"(?<![\w.])compile\(",
                          "__import__", r"import ast", r"getattr\("):
            assert not re.search(forbidden, text), (path.name, forbidden)


def test_preview_reports_stale_and_bad_inputs() -> None:
    from hub.calc.engine import preview

    calc = CalcTag(name="o", unit="kW", data_type=DataType.FLOAT64, formula="p1 + p2",
                   inputs={"p1": "s.a.m1.p", "p2": "s.a.m2.p"}, stale_s=60)
    fresh = Sample(tag="s.a.m1.p", value=1.0, timestamp=NOW)
    old = Sample(tag="s.a.m2.p", value=1.0, timestamp=NOW.replace(hour=11))
    result = preview(calc, "s.a.calc.o", CATALOGUE, {"s.a.m1.p": fresh, "s.a.m2.p": old}, NOW)
    assert result.sample.quality is Quality.BAD and result.sample.reason == "stale"
    good = Sample(tag="s.a.m2.p", value=500.0, timestamp=NOW, quality=Quality.UNCERTAIN)
    result = preview(calc, "s.a.calc.o", CATALOGUE, {"s.a.m1.p": fresh, "s.a.m2.p": good}, NOW)
    assert result.sample.value == pytest.approx(1.5)
    assert result.sample.quality is Quality.UNCERTAIN and set(result.inputs) == {
        "s.a.m1.p", "s.a.m2.p"}
    bad = Sample(tag="s.a.m2.p", value=None, timestamp=NOW, quality=Quality.BAD)
    result = preview(calc, "s.a.calc.o", CATALOGUE, {"s.a.m1.p": fresh, "s.a.m2.p": bad}, NOW)
    assert result.sample.reason == "input_bad" and result.sample.value is None
