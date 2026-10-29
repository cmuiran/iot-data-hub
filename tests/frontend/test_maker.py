"""The maker panel (maker/) without a browser: every text key exists in fa and en, no server
text can become HTML, no inline style or script (the CSP forbids them), nothing loaded from
outside, every page module the router names exists."""

from __future__ import annotations

import json
import re
from pathlib import Path

MAKER = Path(__file__).resolve().parents[2] / "maker"
JS = sorted((MAKER / "js").rglob("*.js"))


def _keys(d: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for k, v in d.items():
        out |= _keys(v, f"{prefix}{k}.") if isinstance(v, dict) else {f"{prefix}{k}"}
    return out


def _dict(lang: str) -> dict:
    return json.loads((MAKER / "i18n" / f"{lang}.json").read_text("utf-8"))


def test_languages_have_the_same_keys() -> None:
    assert _keys(_dict("fa")) == _keys(_dict("en"))


def test_every_key_used_exists() -> None:
    keys = _keys(_dict("fa"))
    used: set[str] = set()
    for f in JS:
        text = f.read_text("utf-8")
        used |= set(re.findall(r"""\bt\(\s*["']([a-z_]+\.[a-z0-9_.]+)["']""", text))
        used |= set(re.findall(r'\["(?:warn|info|ok)", "([a-z_]+\.[a-z0-9_]+)"\]', text))
    assert len(used) > 300, "the pattern finds the calls"
    assert sorted(used - keys) == []
    # keys built at run time: every value they can take is present
    dynamic = {
        "lifecycle": ["registered", "ready", "sold", "active", "disabled"],
        "kind": ["digital_output", "digital_input", "analog_input", "analog_output", "variable",
                 "parameter"],
        "protocol": ["enip", "fins", "http", "iec104", "mc", "modbus_rtu", "modbus_tcp", "mqtt",
                     "native", "opcua", "s7", "snmp"],
        "menu": ["dashboard", "products", "production", "labels", "warranty", "support",
                 "settings"],
        "product": ["tab_spec", "tab_io", "tab_commands", "tab_protocols", "tab_versions"],
        "job": ["pending", "sent", "downloading", "installed", "failed", "cancelled"],
        "warranty": ["state_covered", "state_soon", "state_expired", "state_not_started",
                     "state_none", "state_void"],
        "roles": ["manufacturer_admin", "production_operator", "manufacturer_support"],
    }
    for group, values in dynamic.items():
        assert {f"{group}.{v}" for v in values} <= keys, group


def test_protocol_names_cover_the_hub() -> None:
    from hub.platform.template import device_classes

    assert set(device_classes()) <= {k.split(".", 1)[1] for k in _keys(_dict("fa"))
                                     if k.startswith("protocol.")}


def test_no_html_injection_inline_code_or_outside_resources() -> None:
    for f in [*JS, MAKER / "index.html"]:
        text = f.read_text("utf-8")
        assert "innerHTML" not in text and "outerHTML" not in text, f
        assert "insertAdjacentHTML" not in text and "eval(" not in text, f
        assert "new Function" not in text, f
        assert not re.search(r"""\bstyle=|\.style\.""", text), f
        assert not re.search(r"https?://(?!www\.w3\.org)", text), f
    html = (MAKER / "index.html").read_text("utf-8")
    assert "<script>" not in html and "<style" not in html


def test_router_pages_exist() -> None:
    main = (MAKER / "js" / "main.js").read_text("utf-8")
    pages = re.findall(r'import\("\./(views/[a-z]+\.js)"\)', main)
    assert len(pages) == 9
    for page in pages:
        assert (MAKER / "js" / page).is_file(), page
