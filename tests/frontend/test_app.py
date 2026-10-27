"""The consumer app (app/, platform phase 5) without a browser: every text key exists in fa and
en, no server text can become HTML, no inline style or script (the CSP forbids them), nothing
loaded from outside, the offline shell lists files that exist."""

from __future__ import annotations

import json
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
JS = sorted((APP / "js").glob("*.js"))


def _keys(d: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for k, v in d.items():
        out |= _keys(v, f"{prefix}{k}.") if isinstance(v, dict) else {f"{prefix}{k}"}
    return out


def _dict(lang: str) -> dict:
    return json.loads((APP / "i18n" / f"{lang}.json").read_text("utf-8"))


def test_languages_have_the_same_keys() -> None:
    assert _keys(_dict("fa")) == _keys(_dict("en"))


def test_every_key_used_exists() -> None:
    keys = _keys(_dict("fa"))
    used = set()
    for f in JS:
        used |= set(re.findall(r"""\bt\(\s*["']([a-z_]+\.[a-z_.]+)["']""", f.read_text("utf-8")))
    assert used, "the pattern finds the calls"
    assert sorted(used - keys) == []
    # keys built at run time: every value they can take is present
    for group, values in (("access", ["owner", "operator", "viewer"]),
                          ("command", ["created", "sent", "delivered", "executed", "confirmed",
                                       "failed", "expired"]),
                          ("roles", ["customer_owner", "customer_member", "customer_viewer"])):
        assert {f"{group}.{v}" for v in values} <= keys


def test_no_html_injection_inline_code_or_outside_resources() -> None:
    for f in [*JS, APP / "index.html", APP / "sw.js"]:
        text = f.read_text("utf-8")
        assert "innerHTML" not in text and "outerHTML" not in text, f
        assert "insertAdjacentHTML" not in text and "eval(" not in text, f
        assert not re.search(r"""\bstyle=|\.style\.""", text), f
        assert not re.search(r"https?://(?!www\.w3\.org)", text), f
    html = (APP / "index.html").read_text("utf-8")
    assert "<script>" not in html and "<style" not in html     # only files of the app


def test_offline_shell_and_manifest() -> None:
    sw = (APP / "sw.js").read_text("utf-8")
    shell = json.loads(re.search(r"const SHELL = (\[.*?\]);", sw, re.S)[1].replace("'", '"'))
    for name in shell:
        if name != "./":
            assert (APP / name).is_file(), name
    manifest = json.loads((APP / "manifest.webmanifest").read_text("utf-8"))
    assert manifest["dir"] == "rtl" and (APP / manifest["icons"][0]["src"]).is_file()
