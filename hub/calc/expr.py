"""Formula language for calculated tags.

The formula is tokenized and parsed by hand into a small syntax tree, type- and unit-checked,
and compiled into nested closures. Nothing in a formula can call Python: the only callable
names are the functions in :data:`FUNCTIONS`. Limits on length, tokens, nesting depth and
references keep a hostile formula from exhausting the hub.

Grammar (lowest precedence first)::

    formula := or
    or      := and ('or' and)*
    and     := not ('and' not)*
    not     := 'not' not | compare
    compare := add (('<' | '<=' | '>' | '>=' | '==' | '!=') add)?
    add     := mul (('+' | '-') mul)*
    mul     := unary (('*' | '/') unary)*
    unary   := '-' unary | primary
    primary := NUMBER ['[' unit ']'] | 'true' | 'false' | STRING
             | NAME '(' [formula (',' formula)*] ')' | NAME | '(' formula ')'

``NAME`` is an input alias (``p1``) or a full tag path (``kish.substation.meter_01.p_total``).
A number with a unit (``5[K]``, ``20[°C]``, ``1000[W]``) is a quantity; a bare number is
dimensionless, except ``0``, which compares with and adds to anything. ``°C``/``°F`` literals are
absolute temperatures, ``K`` literals are temperature differences.

Functions: ``abs``, ``min``, ``max``, ``avg``, ``sum`` (any number of arguments), ``if(c, a, b)``;
over the building model ``sum_of``, ``avg_of``, ``min_of``, ``max_of``, ``count_of``
(``("building_a", "active_power")``: every point with that role in the location and below);
and, as the whole formula only, ``integrate(x)`` (energy from power, volume from flow) and
``runtime(condition)`` (running hours).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from hub.building import BuildingModel
from hub.calc.units import (
    DIMENSIONLESS,
    TEMPERATURE,
    TIME,
    Dim,
    Unit,
    UnitError,
    describe,
    dim_mul,
    parse_unit,
)
from hub.models import NUMERIC_TYPES, DataType, TagBase

MAX_TOKENS = 1000
MAX_DEPTH = 40
MAX_REFS = 200
MAX_EXPANDED = 500


class CalcError(ValueError):
    """The formula is invalid. ``pos`` is the character offset (0-based) when known."""

    def __init__(self, message: str, pos: int | None = None) -> None:
        super().__init__(message if pos is None else f"{message} (at character {pos + 1})")
        self.message = message
        self.pos = pos


class CalcMathError(ArithmeticError):
    """Division by zero or a non-finite result while evaluating."""


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()          # an input without a fresh good value


# --------------------------------------------------------------------------------------------
# Tokens and syntax tree
# --------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<unit>\[[^\[\]\n]{0,64}\])
  | (?P<name>[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)
  | (?P<str>"[a-z0-9_]{1,64}"|'[a-z0-9_]{1,64}')
  | (?P<op><=|>=|==|!=|[<>+\-*/(),])
""", re.VERBOSE)
_KEYWORDS = {"and", "or", "not", "true", "false"}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int


def tokenize(text: str) -> list[Token]:
    out: list[Token] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            raise CalcError(f"unexpected character {text[pos]!r}", pos)
        kind = m.lastgroup or ""
        if kind != "ws":
            value = m.group()
            if kind == "name" and value in _KEYWORDS:
                kind = "kw"
            out.append(Token(kind, value, pos))
            if len(out) > MAX_TOKENS:
                raise CalcError(f"formula too long (more than {MAX_TOKENS} tokens)")
        pos = m.end()
    out.append(Token("end", "", len(text)))
    return out


@dataclass(frozen=True)
class Node:
    kind: str                          # num bool str ref neg not bin call
    pos: int
    value: Any = None                  # number, bool, string, name, operator or function name
    unit: str | None = None            # number literal: text inside [...]
    args: tuple[Node, ...] = ()


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = tokenize(text)
        self.i = 0
        self.depth = 0

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def take(self, kind: str, text: str | None = None) -> Token | None:
        t = self.tok
        if t.kind == kind and (text is None or t.text == text):
            self.i += 1
            return t
        return None

    def expect(self, kind: str, text: str) -> Token:
        t = self.take(kind, text)
        if t is None:
            raise CalcError(f"expected {text!r}, found {self.tok.text or 'end of formula'!r}",
                            self.tok.pos)
        return t

    def parse(self) -> Node:
        node = self.or_()
        if self.tok.kind != "end":
            raise CalcError(f"unexpected {self.tok.text!r}", self.tok.pos)
        return node

    def _nest(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CalcError(f"formula nested too deeply (more than {MAX_DEPTH} levels)",
                            self.tok.pos)

    def or_(self) -> Node:
        node = self.and_()
        while (t := self.take("kw", "or")) is not None:
            node = Node("bin", t.pos, "or", args=(node, self.and_()))
        return node

    def and_(self) -> Node:
        node = self.not_()
        while (t := self.take("kw", "and")) is not None:
            node = Node("bin", t.pos, "and", args=(node, self.not_()))
        return node

    def not_(self) -> Node:
        if (t := self.take("kw", "not")) is not None:
            self._nest()
            node = Node("not", t.pos, args=(self.not_(),))
            self.depth -= 1
            return node
        return self.compare()

    def compare(self) -> Node:
        node = self.add()
        t = self.tok
        if t.kind == "op" and t.text in ("<", "<=", ">", ">=", "==", "!="):
            self.i += 1
            node = Node("bin", t.pos, t.text, args=(node, self.add()))
            u = self.tok
            if u.kind == "op" and u.text in ("<", "<=", ">", ">=", "==", "!="):
                raise CalcError("comparisons cannot be chained; use 'and'", u.pos)
        return node

    def add(self) -> Node:
        node = self.mul()
        while self.tok.kind == "op" and self.tok.text in ("+", "-"):
            t = self.tok
            self.i += 1
            node = Node("bin", t.pos, t.text, args=(node, self.mul()))
        return node

    def mul(self) -> Node:
        node = self.unary()
        while self.tok.kind == "op" and self.tok.text in ("*", "/"):
            t = self.tok
            self.i += 1
            node = Node("bin", t.pos, t.text, args=(node, self.unary()))
        return node

    def unary(self) -> Node:
        if (t := self.take("op", "-")) is not None:
            self._nest()
            node = Node("neg", t.pos, args=(self.unary(),))
            self.depth -= 1
            return node
        return self.primary()

    def primary(self) -> Node:
        t = self.tok
        if t.kind == "num":
            self.i += 1
            unit = self.take("unit")
            return Node("num", t.pos, float(t.text), unit=unit.text[1:-1] if unit else None)
        if t.kind == "kw" and t.text in ("true", "false"):
            self.i += 1
            return Node("bool", t.pos, t.text == "true")
        if t.kind == "str":
            self.i += 1
            return Node("str", t.pos, t.text[1:-1])
        if t.kind == "name":
            self.i += 1
            if self.take("op", "(") is None:
                return Node("ref", t.pos, t.text)
            self._nest()
            args: list[Node] = []
            if self.take("op", ")") is None:
                args.append(self.or_())
                while self.take("op", ",") is not None:
                    args.append(self.or_())
                self.expect("op", ")")
            self.depth -= 1
            return Node("call", t.pos, t.text, args=tuple(args))
        if self.take("op", "(") is not None:
            self._nest()
            node = self.or_()
            self.expect("op", ")")
            self.depth -= 1
            return node
        if t.kind == "unit":
            raise CalcError("a unit in [...] must follow a number", t.pos)
        raise CalcError(f"unexpected {t.text or 'end of formula'!r}", t.pos)


def parse(text: str) -> Node:
    return _Parser(text).parse()


# --------------------------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------------------------

TempKind = Literal["abs", "delta"] | None


@dataclass(frozen=True)
class VType:
    kind: Literal["num", "bool", "str"]
    dim: Dim = DIMENSIONLESS
    temp: TempKind = None              # only for the temperature dimension
    zero: bool = False                 # the literal 0: fits any dimension

    def show(self) -> str:
        if self.kind != "num":
            return self.kind
        text = describe(self.dim)
        if self.temp == "abs":
            return "temperature"
        if self.temp == "delta":
            return "temperature difference"
        return text


BOOL = VType("bool")
STR = VType("str")
NUMBER = VType("num")


def _num(dim: Dim, temp: TempKind = None) -> VType:
    return VType("num", dim, temp if dim == TEMPERATURE else None)


class Context:
    """What a compiled formula reads while it is evaluated (implemented by the engine and by
    the preview): input values in engineering units, or MISSING."""

    def __init__(self) -> None:
        self.uncertain = False         # an input was uncertain, or an aggregate skipped one
        self.reason: str | None = None  # why a value is missing: stale | input_bad

    def raw(self, path: str) -> Any:   # pragma: no cover - interface
        raise NotImplementedError


Fn = Callable[[Context], Any]


@dataclass
class Compiled:
    """A checked formula. ``evaluate`` returns the value in SI (or bool) or MISSING."""

    fn: Fn
    vtype: VType
    refs: set[str] = field(default_factory=set)
    stateful: Literal["integrate", "runtime"] | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Env:
    inputs: Mapping[str, str]
    catalogue: Mapping[str, TagBase]
    building: BuildingModel | None
    skip_missing: bool
    refs: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    units: dict[str, Unit] = field(default_factory=dict)

    def unit_of(self, path: str, tag: TagBase) -> Unit:
        if path not in self.units:
            try:
                unit = parse_unit(tag.unit)
            except UnitError as exc:
                raise CalcError(f"{path}: {exc}") from None
            if not unit.known:
                self.warnings.append(f"unit {tag.unit!r} of {path} is not in the unit table: it "
                                     "only combines with the same unit")
            self.units[path] = unit
        return self.units[path]


# --------------------------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------------------------

_AGGREGATES = ("min", "max", "avg", "sum")
_BUILDING_AGGREGATES = ("sum_of", "avg_of", "min_of", "max_of", "count_of")
FUNCTIONS = frozenset({"abs", "if", *_AGGREGATES, *_BUILDING_AGGREGATES, "integrate", "runtime"})


def _missing(*values: Any) -> bool:
    return any(v is MISSING for v in values)


def _same(a: VType, b: VType, what: str, pos: int) -> VType:
    if a.kind != b.kind:
        raise CalcError(f"{what}: cannot combine {a.show()} with {b.show()}", pos)
    if a.kind != "num":
        return a
    if a.zero:
        return b
    if b.zero:
        return a
    if a.dim != b.dim:
        raise CalcError(f"{what}: units do not match ({a.show()} and {b.show()})", pos)
    if a.temp != b.temp:
        raise CalcError(f"{what}: cannot combine a {a.show()} with a {b.show()}", pos)
    return a


def _compile(node: Node, env: _Env, top: bool = False) -> tuple[Fn, VType, str | None]:
    """(closure, type, stateful kind)."""
    kind = node.kind
    if kind == "num":
        value = node.value
        if node.unit is None:
            return (lambda _c: value), VType("num", zero=value == 0), None
        try:
            unit = parse_unit(node.unit)
        except UnitError as exc:
            raise CalcError(str(exc), node.pos) from None
        if not unit.known:
            raise CalcError(f"unknown unit {node.unit!r}", node.pos)
        temp: TempKind = ("abs" if unit.offset else "delta") if unit.dim == TEMPERATURE else None
        si = unit.to_si(value, difference=temp == "delta")
        return (lambda _c: si), _num(unit.dim, temp), None
    if kind == "bool":
        b = node.value
        return (lambda _c: b), BOOL, None
    if kind == "str":
        raise CalcError("text is only allowed as the arguments of sum_of/avg_of/...", node.pos)
    if kind == "ref":
        return _compile_ref(node, env)
    if kind == "neg":
        fn, t, _ = _compile(node.args[0], env)
        if t.kind != "num":
            raise CalcError(f"'-' needs a number, not {t.show()}", node.pos)
        if t.temp == "abs":
            raise CalcError("cannot negate an absolute temperature", node.pos)
        return (lambda c: MISSING if (v := fn(c)) is MISSING else -v), t, None
    if kind == "not":
        fn, t, _ = _compile(node.args[0], env)
        if t != BOOL:
            raise CalcError(f"'not' needs a condition, not {t.show()}", node.pos)
        return (lambda c: MISSING if (v := fn(c)) is MISSING else not v), BOOL, None
    if kind == "bin":
        return _compile_binary(node, env)
    if kind == "call":
        return _compile_call(node, env, top)
    raise CalcError(f"unsupported expression {kind}", node.pos)  # pragma: no cover


def _compile_ref(node: Node, env: _Env) -> tuple[Fn, VType, str | None]:
    name = node.value
    if name in FUNCTIONS:
        raise CalcError(f"{name} is a function: call it with (...)", node.pos)
    path = env.inputs.get(name) if "." not in name else name
    if path is None:
        raise CalcError(f"unknown name {name!r}: add it to inputs or use a full tag path",
                        node.pos)
    tag = env.catalogue.get(path)
    if tag is None:
        raise CalcError(f"unknown tag {path}", node.pos)
    env.refs.add(path)
    if len(env.refs) > MAX_REFS:
        raise CalcError(f"more than {MAX_REFS} tags in one formula", node.pos)
    if tag.data_type is DataType.BOOL:
        return (lambda c: c.raw(path)), BOOL, None
    if tag.data_type not in NUMERIC_TYPES:
        raise CalcError(f"{path} is a {tag.data_type} tag; formulas use numbers and bools",
                        node.pos)
    unit = env.unit_of(path, tag)

    def read(c: Context) -> Any:
        v = c.raw(path)
        return MISSING if v is MISSING else unit.to_si(float(v))

    return read, _num(unit.dim, "abs"), None


def _compile_binary(node: Node, env: _Env) -> tuple[Fn, VType, str | None]:
    op = node.value
    (fa, ta, _), (fb, tb, _) = (_compile(a, env) for a in node.args)
    if op in ("and", "or"):
        if ta != BOOL or tb != BOOL:
            raise CalcError(f"'{op}' needs conditions on both sides", node.pos)
        if op == "and":
            return (lambda c: MISSING if _missing(a := fa(c), b := fb(c)) else a and b), BOOL, None
        return (lambda c: MISSING if _missing(a := fa(c), b := fb(c)) else a or b), BOOL, None
    if op in ("==", "!=", "<", "<=", ">", ">="):
        if ta.kind == "bool" or tb.kind == "bool":
            if op not in ("==", "!=") or ta != tb:
                raise CalcError(f"'{op}' cannot compare {ta.show()} with {tb.show()}", node.pos)
        else:
            _same(ta, tb, f"'{op}'", node.pos)
        cmp = _COMPARE[op]
        return (lambda c: MISSING if _missing(a := fa(c), b := fb(c)) else cmp(a, b)), BOOL, None
    if ta.kind != "num" or tb.kind != "num":
        raise CalcError(f"'{op}' needs numbers, not {ta.show()} and {tb.show()}", node.pos)
    if op in ("+", "-"):
        t = _additive(op, ta, tb, node.pos)
        if op == "+":
            return (lambda c: MISSING if _missing(a := fa(c), b := fb(c)) else a + b), t, None
        return (lambda c: MISSING if _missing(a := fa(c), b := fb(c)) else a - b), t, None
    t = _num(dim_mul(ta.dim, tb.dim, 1 if op == "*" else -1))
    if op == "*":
        return (lambda c: MISSING if _missing(a := fa(c), b := fb(c)) else a * b), t, None

    def divide(c: Context) -> Any:
        a, b = fa(c), fb(c)
        if _missing(a, b):
            return MISSING
        if b == 0:
            raise CalcMathError("division by zero")
        return a / b

    return divide, t, None


def _additive(op: str, a: VType, b: VType, pos: int) -> VType:
    if a.zero or b.zero:
        return b if a.zero else a
    if a.dim != b.dim:
        raise CalcError(f"'{op}': units do not match ({a.show()} and {b.show()})", pos)
    if a.dim != TEMPERATURE:
        return _num(a.dim)
    kinds = (a.temp, b.temp)
    if op == "+":
        if kinds == ("abs", "abs"):
            raise CalcError("cannot add two temperatures; use avg() or subtract them", pos)
        return _num(TEMPERATURE, "abs" if "abs" in kinds else "delta")
    if kinds == ("abs", "abs"):
        return _num(TEMPERATURE, "delta")
    if kinds == ("delta", "abs"):
        raise CalcError("cannot subtract a temperature from a temperature difference", pos)
    return _num(TEMPERATURE, a.temp)


_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
}


def _aggregate(name: str, fns: list[Fn], skip: bool) -> Fn:
    reducer: Callable[[list[Any]], Any] = {
        "min": min, "max": max, "sum": math.fsum, "avg": lambda v: math.fsum(v) / len(v),
        "count": len,
    }[name]

    def run(c: Context) -> Any:
        values = [fn(c) for fn in fns]
        present = [v for v in values if v is not MISSING]
        if len(present) < len(values):
            if not skip or not present:
                return MISSING
            c.uncertain = True
        return reducer(present)

    return run


def _compile_call(node: Node, env: _Env, top: bool) -> tuple[Fn, VType, str | None]:
    name, args = node.value, node.args
    if name not in FUNCTIONS:
        raise CalcError(f"unknown function {name}()", node.pos)
    if name in _BUILDING_AGGREGATES:
        return _compile_building_aggregate(node, env)
    if name in ("integrate", "runtime"):
        if not top:
            raise CalcError(f"{name}() must be the whole formula (it keeps a running total)",
                            node.pos)
        if len(args) != 1:
            raise CalcError(f"{name}() takes one argument", node.pos)
        fn, t, _ = _compile(args[0], env)
        if name == "runtime":
            if t != BOOL:
                raise CalcError("runtime() needs a condition, e.g. runtime(pump_on)", node.pos)
            return fn, _num(TIME), "runtime"
        if t.kind != "num" or t.temp == "abs":
            raise CalcError(f"integrate() needs a rate (power, flow), not {t.show()}", node.pos)
        return fn, _num(dim_mul(t.dim, TIME)), "integrate"
    compiled = [_compile(a, env) for a in args]
    fns = [c[0] for c in compiled]
    types = [c[1] for c in compiled]
    if name == "if":
        if len(args) != 3:
            raise CalcError("if() takes three arguments: if(condition, then, else)", node.pos)
        if types[0] != BOOL:
            raise CalcError("the first argument of if() must be a condition", node.pos)
        t = _same(types[1], types[2], "if()", node.pos)
        cond, then, other = fns

        def choose(c: Context) -> Any:
            v = cond(c)
            if v is MISSING:
                return MISSING
            return then(c) if v else other(c)

        return choose, t, None
    if name == "abs":
        if len(args) != 1 or types[0].kind != "num" or types[0].temp == "abs":
            raise CalcError("abs() takes one number (not an absolute temperature)", node.pos)
        fn = fns[0]
        return (lambda c: MISSING if (v := fn(c)) is MISSING else abs(v)), types[0], None
    if not args:
        raise CalcError(f"{name}() needs at least one argument", node.pos)
    t = types[0]
    for other_t in types[1:]:
        t = _same(t, other_t, f"{name}()", node.pos)
    if t.kind != "num":
        raise CalcError(f"{name}() needs numbers", node.pos)
    if name == "sum" and t.temp == "abs":
        raise CalcError("cannot add temperatures; use avg()", node.pos)
    return _aggregate(name, fns, env.skip_missing), t, None


def _compile_building_aggregate(node: Node, env: _Env) -> tuple[Fn, VType, str | None]:
    name, args = node.value, node.args
    if len(args) != 2 or any(a.kind != "str" for a in args):
        raise CalcError(f'{name}() takes a location and a point role, e.g. '
                        f'{name}("building_a", "active_power")', node.pos)
    location, role = args[0].value, args[1].value
    model = env.building
    if model is None or location not in model.locations:
        raise CalcError(f"unknown location {location!r} (config/building.yaml)", args[0].pos)
    paths = model.points_in(location, role)
    if not paths:
        raise CalcError(f"no point with role {role!r} in {location}", args[1].pos)
    if len(paths) > MAX_EXPANDED:
        raise CalcError(f"{name}() matches more than {MAX_EXPANDED} points", node.pos)
    compiled = [_compile_ref(Node("ref", node.pos, p), env) for p in paths]
    t = compiled[0][1]
    for c in compiled[1:]:
        t = _same(t, c[1], f"{name}()", node.pos)
    fns = [c[0] for c in compiled]
    if name == "count_of":
        return _aggregate("count", fns, True), NUMBER, None
    if t.kind != "num":
        raise CalcError(f"{name}() needs numeric points; {role} is {t.show()}", node.pos)
    if name == "sum_of" and t.temp == "abs":
        raise CalcError("cannot add temperatures; use avg_of()", node.pos)
    return _aggregate(name.removesuffix("_of"), fns, env.skip_missing), t, None


def compile_formula(formula: str, *, inputs: Mapping[str, str],
                    catalogue: Mapping[str, TagBase], building: BuildingModel | None = None,
                    skip_missing: bool = False) -> Compiled:
    for alias, path in inputs.items():
        if alias in FUNCTIONS or alias in _KEYWORDS:
            raise CalcError(f"input name {alias!r} is reserved")
        if path not in catalogue:
            raise CalcError(f"input {alias}: unknown tag {path}")
    env = _Env(inputs, catalogue, building, skip_missing)
    fn, vtype, stateful = _compile(parse(formula), env, top=True)
    if vtype.kind == "str":  # pragma: no cover - strings are refused earlier
        raise CalcError("a formula must give a number or a condition")
    unused = sorted(a for a, p in inputs.items() if p not in env.refs)
    if unused:
        env.warnings.append(f"inputs not used in the formula: {', '.join(unused)}")
    return Compiled(fn, vtype, env.refs, stateful, env.warnings)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Output:
    """Conversion of a compiled formula's SI result to the calculated tag."""

    unit: Unit | None                  # None: bool
    difference: bool = False

    def convert(self, value: Any) -> Any:
        if self.unit is None:
            return bool(value)
        out = self.unit.from_si(float(value), difference=self.difference)
        if not math.isfinite(out):
            raise CalcMathError("result is not a finite number")
        return out


def output_for(compiled: Compiled, tag: TagBase) -> Output:
    """Check the formula's result against the tag's data type and unit."""
    t = compiled.vtype
    if tag.data_type is DataType.BOOL:
        if t != BOOL:
            raise CalcError(f"the tag is bool but the formula gives {t.show()}")
        return Output(None)
    if t.kind != "num":
        raise CalcError(f"the formula gives a condition; use data_type: bool (not "
                        f"{tag.data_type})")
    try:
        unit = parse_unit(tag.unit)
    except UnitError as exc:
        raise CalcError(f"unit: {exc}") from None
    if t.zero and not t.dim:
        return Output(unit)
    if unit.dim != t.dim:
        hint = "" if tag.unit else " (set unit:, e.g. kW)"
        raise CalcError(f"the formula gives {t.show()} but the tag unit {tag.unit or 'none'!r} "
                        f"is {describe(unit.dim)}{hint}")
    return Output(unit, difference=t.temp == "delta")


__all__ = [
    "MISSING", "CalcError", "CalcMathError", "Compiled", "Context", "Output", "compile_formula",
    "output_for", "parse", "tokenize",
]
