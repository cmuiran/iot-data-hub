from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from backend.logging_setup import configure, redact
from backend.service import render_winsw_xml


@pytest.mark.parametrize(
    "text,secret",
    [("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def", "eyJhbGciOiJIUzI1NiJ9"),
     ('login {"username": "a", "password": "hunter2!"}', "hunter2!"),
     ("password=Sup3rSecret&x=1", "Sup3rSecret"),
     ("postgresql://hub:s3cret@db:5432/hub", "s3cret"),
     ("refresh_token: abcdefgh12345678", "abcdefgh12345678")],
)
def test_redact(text: str, secret: str) -> None:
    cleaned = redact(text)
    assert secret not in cleaned and "***" in cleaned


def test_rotating_log_file_without_secrets(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "hub.log"
    configure("info", log_file, max_mb=1, backups=2)
    try:
        logging.getLogger("hub.test").info("connecting with password=%s", "TopSecret99")
        logging.getLogger("hub.test").info("plain message")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
        assert "TopSecret99" not in text and "password=***" in text and "plain message" in text
        handlers = [type(h).__name__ for h in logging.getLogger().handlers]
        assert "RotatingFileHandler" in handlers
    finally:
        configure("warning")   # drop the file handler again


def test_winsw_definition(tmp_path: Path) -> None:
    xml = render_winsw_xml(tmp_path, account="KISH\\svc-hub")
    root = ET.fromstring(xml)
    assert root.findtext("id") == "iot-data-hub"
    assert root.findtext("executable").endswith(str(Path(".venv/Scripts/hub-api.exe")))
    assert root.findtext("workingdirectory") == str(tmp_path.resolve())
    assert [f.get("action") for f in root.findall("onfailure")] == ["restart"] * 3
    assert root.find("env").get("name") == "HUB_LOG_FILE"
    assert root.findtext("serviceaccount/username") == "KISH\\svc-hub"


def test_hub_admin_service_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import admin

    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
    out = tmp_path / "svc.xml"
    assert admin.main(["service-config", "--out", str(out)]) == 0
    ET.fromstring(out.read_text(encoding="utf-8"))
