"""Structured logs (JSON), syslog for a SIEM (RFC 5424 over UDP and TCP), redaction, and the
audit copy in the log."""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from backend.logging_setup import JsonFormatter, Rfc5424Formatter, SiemHandler, configure
from simulators.mqtt_sim import free_port


def record(msg: str, name: str = "hub.test", **extra: Any) -> logging.LogRecord:
    r = logging.LogRecord(name, logging.WARNING, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(r, k, v)
    return r


def test_json_formatter_fields_and_redaction() -> None:
    doc = json.loads(JsonFormatter("node_a").format(record(
        "login with password=hunter2 and Bearer abcdefghijklmnop",
        audit={"event": "login_ok", "user": "op.one"})))
    assert doc["level"] == "warning" and doc["logger"] == "hub.test" and doc["node"] == "node_a"
    assert doc["ts"].endswith("Z") and doc["audit"]["event"] == "login_ok"
    assert "hunter2" not in doc["msg"] and "abcdefghijklmnop" not in doc["msg"]


def test_rfc5424_layout() -> None:
    text = Rfc5424Formatter("node_a").format(record("x", name="hub.audit", audit={"a": 1}))
    version, ts, host, app, pid, msgid, sd, body = text.split(" ", 7)
    assert (version, host, app, msgid, sd) == ("1", "node_a", "iot-data-hub", "audit", "-")
    assert json.loads(body)["audit"] == {"a": 1}


def _udp_server() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(5)
    return s, s.getsockname()[1]


def test_udp_syslog_and_json_file(tmp_path: Path) -> None:
    server, port = _udp_server()
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        configure("info", tmp_path / "hub.log", fmt="json", syslog=f"udp://127.0.0.1:{port}",
                  facility="local3", node="node_a")
        logging.getLogger("hub.audit").warning("login_failed op.one",
                                               extra={"audit": {"event": "login_failed"}})
        data, _ = server.recvfrom(65535)
        text = data.decode()
        # PRI = facility local3 (19) * 8 + warning (4) = 156
        assert text.startswith("<156>1 ") and " audit - " in text
        assert json.loads(text.split(" - ", 1)[1])["audit"]["event"] == "login_failed"
        for h in logging.getLogger().handlers:
            h.flush()
        line = (tmp_path / "hub.log").read_text(encoding="utf-8").strip().splitlines()[-1]
        assert json.loads(line)["audit"]["event"] == "login_failed"
    finally:
        server.close()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved:
            root.addHandler(h)


def test_tcp_syslog_reconnects() -> None:
    port = free_port()
    received: list[bytes] = []

    def serve(stop_after: int) -> threading.Thread:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen()

        def run() -> None:
            conn, _ = srv.accept()
            conn.settimeout(5)
            buf = b""
            while buf.count(b"\n") < stop_after:
                chunk = conn.recv(65535)
                if not chunk:
                    break
                buf += chunk
            received.extend(buf.splitlines())
            conn.close()
            srv.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    first = serve(1)
    handler = SiemHandler(f"tcp://127.0.0.1:{port}", "local0")
    handler.setFormatter(Rfc5424Formatter("node_a"))
    handler.emit(record("before the cut"))
    first.join(5)
    second = serve(1)                         # the SIEM restarts on the same port
    time.sleep(0.2)
    for _ in range(3):                        # the first send after a cut may still "succeed"
        handler.emit(record("after the cut"))
        time.sleep(0.2)
        if any(b"after the cut" in r for r in received):
            break
    second.join(5)
    handler.close()
    assert any(b"before the cut" in r for r in received)
    assert any(b"after the cut" in r for r in received)
    assert all(r.startswith(b"<") for r in received)


def test_unreachable_tcp_syslog_does_not_stop_the_hub(capsys: pytest.CaptureFixture[str]
                                                      ) -> None:
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        configure("info", syslog=f"tcp://127.0.0.1:{free_port()}")
        assert "logging locally only" in capsys.readouterr().out
        logging.getLogger("hub.test").info("still logging")
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved:
            root.addHandler(h)


@pytest.mark.postgres
async def test_audit_rows_are_logged(store: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="hub.audit")
    await store.add_auth_audit("login_failed", "op.one", client_addr="10.0.0.9")
    rec = next(r for r in caplog.records if r.name == "hub.audit")
    assert rec.levelno == logging.WARNING                  # security events stand out
    assert rec.audit["event"] == "login_failed" and rec.audit["client"] == "10.0.0.9"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell parser")
def test_powershell_scripts_parse() -> None:
    import subprocess

    root = Path(__file__).resolve().parents[2] / "scripts" / "windows"
    for script in sorted(root.glob("*.ps1")):
        command = ("$e=$null; [System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{script}', [ref]$null, [ref]$e) | Out-Null; $e.Count")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                             capture_output=True, text=True, timeout=60, check=True)
        assert out.stdout.strip() == "0", (script.name, out.stdout, out.stderr)
