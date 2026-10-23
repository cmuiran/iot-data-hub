"""db/migrations repeat some enum lists from hub/models.py; keep them from drifting apart.

Later migrations may redefine a CHECK (e.g. 0003 adds modbus_rtu), so the last definition wins.
"""

from __future__ import annotations

import re
from pathlib import Path

from hub.models import PROTOCOLS, QUALITY_CODES, SEGMENT_PATTERN, Access, DataType

MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "db" / "migrations").glob("*.sql"))
SCHEMA = "\n".join(p.read_text(encoding="utf-8") for p in MIGRATIONS)


def _in_list_raw(column: str) -> str:
    """Body of the last ``CHECK (<column> IN (...))`` across all migrations."""
    matches = re.findall(rf"\b{column}\s+IN\s*\(([^)]*)\)", SCHEMA)
    assert matches, f"no CHECK ({column} IN (...)) in db/migrations"
    return matches[-1]


def _in_list(column: str) -> set[str]:
    return set(re.findall(r"'([^']*)'", _in_list_raw(column)))


def test_data_types_match() -> None:
    assert _in_list("data_type") == {str(t) for t in DataType}


def test_protocols_match() -> None:
    assert _in_list("protocol") == set(PROTOCOLS)


def test_access_values_match() -> None:
    assert _in_list("access") == {str(a) for a in Access}


def test_quality_codes_match() -> None:
    codes = {int(c) for c in re.findall(r"\d+", _in_list_raw("quality"))}
    assert codes == set(QUALITY_CODES.values())


def test_segment_regex_matches() -> None:
    assert f"'^{SEGMENT_PATTERN}$'" in SCHEMA


def test_migrations_are_numbered_and_unique() -> None:
    numbers = [p.name.split("_", 1)[0] for p in MIGRATIONS]
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)
    assert all(n.isdigit() and len(n) == 4 for n in numbers)
