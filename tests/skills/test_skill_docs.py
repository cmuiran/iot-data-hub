"""The add-device skill's YAML examples must stay valid against hub/models.py."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from hub.models import DEVICE_ADAPTER, PROTOCOLS, DataType

REFERENCES = (Path(__file__).resolve().parents[2] / ".claude" / "skills" / "add-device"
              / "references")
CHEATSHEET = (REFERENCES / "schema-cheatsheet.md").read_text(encoding="utf-8")


def _device_examples() -> list[str]:
    blocks = re.findall(r"```yaml\n(.*?)```", CHEATSHEET, flags=re.DOTALL)
    return [b for b in blocks if b.startswith("device_id:")]


def test_cheatsheet_has_one_example_per_protocol() -> None:
    protocols = sorted(yaml.safe_load(b)["protocol"] for b in _device_examples())
    assert protocols == sorted(PROTOCOLS)   # a new protocol needs its example


@pytest.mark.parametrize("block", _device_examples(), ids=lambda b: b.split("\n")[0])
def test_cheatsheet_examples_validate(block: str) -> None:
    DEVICE_ADAPTER.validate_python(yaml.safe_load(block))


def test_skill_frontmatter_within_upload_limits() -> None:
    text = (REFERENCES.parent / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, _ = text.split("---", 2)
    meta = yaml.safe_load(frontmatter)
    assert meta["name"] == "add-device"
    assert len(meta["name"]) <= 64
    assert 0 < len(meta["description"]) <= 1024  # claude.ai rejects longer descriptions


def test_cheatsheet_lists_every_data_type() -> None:
    row = next(line for line in CHEATSHEET.splitlines() if line.startswith("| `data_type`"))
    assert set(re.findall(r"`([a-z0-9]+)`", row)) - {"data_type"} == {str(t) for t in DataType}
