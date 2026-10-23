"""The dashboard's files: translations, code rules (CSP, no markup injection), templates.

The frontend has no build step and no JavaScript test runner (no Node on the plant PCs), so the
rules that keep it safe and translatable are checked here on the source files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from backend.control import Status
from backend.settings import BUNDLED_FRONTEND, Settings, _frontend_dir
from hub.drivers.base import DeviceState
from hub.models import DEVICE_ADAPTER, PROTOCOLS, AlarmSeverity, CompareOp, Quality

FRONTEND = BUNDLED_FRONTEND
ROOT = FRONTEND.parent
JS_FILES = sorted((FRONTEND / "js").rglob("*.js"))


def _flatten(node: dict[str, Any], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


@pytest.fixture(scope="module")
def dictionaries() -> dict[str, dict[str, str]]:
    return {lang: _flatten(json.loads((FRONTEND / "i18n" / f"{lang}.json").read_text("utf-8")))
            for lang in ("fa", "en")}


@pytest.fixture(scope="module")
def source() -> str:
    return "\n".join(p.read_text("utf-8") for p in JS_FILES)


def _sql_list(migration: str, start: str) -> list[str]:
    text = (ROOT / "db" / "migrations" / migration).read_text("utf-8")
    block = text[text.index(start):]
    block = block[:block.index("))")]
    return re.findall(r"'([a-z_]+)'", block)


# --- translations -------------------------------------------------------------------------------


def test_languages_have_the_same_keys(dictionaries: dict[str, dict[str, str]]) -> None:
    fa, en = dictionaries["fa"], dictionaries["en"]
    assert set(fa) == set(en), sorted(set(fa) ^ set(en))
    empty = [k for k in fa if not fa[k].strip() or not en[k].strip()]
    assert empty == ["dash.flags"]           # an intentionally unlabeled column


def test_placeholders_match_between_languages(dictionaries: dict[str, dict[str, str]]) -> None:
    fa, en = dictionaries["fa"], dictionaries["en"]
    for key in fa:
        assert set(re.findall(r"\{(\w+)\}", fa[key])) == set(re.findall(r"\{(\w+)\}", en[key])), key


def test_every_key_used_in_the_code_exists(source: str,
                                           dictionaries: dict[str, dict[str, str]]) -> None:
    used = set(re.findall(r"\bt(?:Maybe)?\('([a-z_0-9.]+)'", source))
    used |= set(re.findall(r"(?:label|group): '(nav\.[a-z_]+)'", source))
    missing = sorted(k for k in used if k not in dictionaries["en"])
    assert not missing
    assert len(used) > 500                    # the pattern still finds the calls


@pytest.mark.parametrize(("prefix", "values"), [
    ("device_state", [str(s) for s in DeviceState] + ["connecting"]),
    ("quality", [str(q) for q in Quality]),
    ("severity", [str(s) for s in AlarmSeverity]),
    ("protocol", list(PROTOCOLS)),
    ("op", list(get_args(CompareOp))),
    ("write_status", list(get_args(Status))),
    ("write_result", _sql_list("0001_core.sql", "result           text NOT NULL CHECK")),
    ("auth_event", _sql_list("0004_security.sql", "ADD CONSTRAINT auth_audit_event_check")),
    ("roles", ["admin", "engineer", "operator", "viewer"]),
    ("interlock", ["ok", "blocked", "stale", "unknown"]),
])
def test_enum_values_are_translated(prefix: str, values: list[str],
                                    dictionaries: dict[str, dict[str, str]]) -> None:
    assert values
    for lang, words in dictionaries.items():
        missing = [v for v in values if f"{prefix}.{v}" not in words]
        assert not missing, (lang, prefix, missing)


def test_persian_calendar_lists(dictionaries: dict[str, dict[str, str]]) -> None:
    fa = dictionaries["fa"]
    assert len(fa["calendar.persian_months"].split(",")) == 12
    assert fa["calendar.persian_months"].split(",")[7] == "آبان"
    assert len(fa["calendar.weekdays_short"].split(",")) == 7   # Saturday first


# --- code rules ---------------------------------------------------------------------------------


def test_no_markup_injection_or_eval(source: str) -> None:
    """Text goes in as text nodes (ui.h); data from devices or users is never parsed as HTML."""
    for pattern in (r"\.innerHTML\s*=", r"\.outerHTML\s*=", r"insertAdjacentHTML",
                    r"document\.write", r"\beval\(", r"new Function\(", r"setAttribute\('style'"):
        offenders = [p.name for p in JS_FILES if re.search(pattern, p.read_text("utf-8"))]
        assert not offenders, (pattern, offenders)


def test_index_has_no_inline_code_or_style() -> None:
    html = (FRONTEND / "index.html").read_text("utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)       # every script is a file
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    assert not re.search(r"https?://", html.replace("http-equiv", ""))  # nothing from the internet


def test_no_external_resources(source: str) -> None:
    css = (FRONTEND / "css" / "app.css").read_text("utf-8")
    assert "@import" not in css and "url(http" not in css
    assert not re.search(r"""(?:fetch|import)\(\s*['"`]https?://""", source)


def test_tokens_are_not_put_in_urls(source: str) -> None:
    assert "access_token=" not in source and "token=" not in source.replace("mfa_token", "")
    api = (FRONTEND / "js" / "api.js").read_text("utf-8")
    assert "localStorage" not in api        # the refresh token stays in sessionStorage only


def test_device_writes_only_through_the_control_api(source: str) -> None:
    """The only command route is POST /api/control/write (the control-safety layer)."""
    writes = re.findall(r"api/control/[a-z/${}._]+", source)
    assert {w for w in writes if "write" in w} == {"api/control/write"}


def test_views_are_registered() -> None:
    main = (FRONTEND / "js" / "main.js").read_text("utf-8")
    for module in re.findall(r"import\('\./views/([a-z-]+)\.js'\)", main):
        assert (FRONTEND / "js" / "views" / f"{module}.js").is_file(), module


# --- device templates -----------------------------------------------------------------------------


def test_device_templates_are_valid_and_disabled() -> None:
    templates = json.loads((FRONTEND / "templates" / "devices.json").read_text("utf-8"))
    assert set(templates) == set(PROTOCOLS)
    for protocol, data in templates.items():
        device = DEVICE_ADAPTER.validate_python(data)
        assert device.protocol == protocol
        assert device.enabled is False       # a new device starts switched off


# --- settings -------------------------------------------------------------------------------------


def test_frontend_dir_setting(tmp_path: Path) -> None:
    assert _frontend_dir(None) == BUNDLED_FRONTEND
    assert _frontend_dir("off") is None
    assert _frontend_dir(str(tmp_path)) == tmp_path
    env = {"HUB_JWT_SECRET": "s" * 40, "HUB_FRONTEND_DIR": "OFF"}
    assert Settings.from_env(env).frontend_dir is None
