"""Logging: console + optional rotating file + optional syslog (SIEM), secrets always redacted.

* ``HUB_LOG_FORMAT=json``: one JSON object per line in the log file (``ts``, ``level``,
  ``logger``, ``msg``, ``node`` and structured fields such as ``audit``), for log shippers.
* ``HUB_SYSLOG=udp://host:514`` or ``tcp://host:601``: RFC 5424 messages with a JSON body to a
  syslog server / SIEM (facility ``HUB_SYSLOG_FACILITY``, default local0). TCP uses
  newline framing (RFC 6587) and reconnects after a network cut. An unreachable syslog server
  never stops the hub: it is reported on the console and logging goes on locally.
* The logger ``hub.audit`` carries every audit row (sign-ins, administration, commands) with
  its fields, so the SIEM has a copy outside the database.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
APP_NAME = "iot-data-hub"

# Things that must never reach a log file even if some library logs them.
_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (re.compile(r'(?i)("?(?:password|passwd|secret|token|refresh_token|access_token)"?\s*[:=]\s*)'
                r'("[^"]*"|\'[^\']*\'|[^\s,;}&]+)'), r"\1***"),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:/@\s]+:)[^@\s]+@"), r"\1***@"),
]

# LogRecord attributes that are not "extra" fields
_STANDARD = set(vars(logging.LogRecord("x", 0, "", 0, "", None, None))) | {"message", "asctime"}


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    """One JSON object per record; extra fields (``extra={"audit": {...}}``) are kept."""

    def __init__(self, node: str | None = None) -> None:
        super().__init__()
        self.node = node or socket.gethostname().lower()

    def document(self, record: logging.LogRecord) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(), "logger": record.name,
            "msg": redact(record.getMessage()), "node": self.node, "pid": record.process,
        }
        for key, value in _extras(record).items():
            doc[key] = value
        if record.exc_info:
            doc["exception"] = redact(self.formatException(record.exc_info))
        return doc

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.document(record), ensure_ascii=False, default=str)


class Rfc5424Formatter(JsonFormatter):
    """``1 TIMESTAMP HOST APP PROCID MSGID - {json}`` (the handler adds ``<PRI>``)."""

    def format(self, record: logging.LogRecord) -> str:
        doc = self.document(record)
        msgid = "audit" if record.name == "hub.audit" else "-"
        return (f"1 {doc['ts']} {self.node} {APP_NAME} {record.process} {msgid} - "
                + json.dumps(doc, ensure_ascii=False, default=str))


class SiemHandler(logging.handlers.SysLogHandler):
    """SysLogHandler that frames TCP messages with a newline and reconnects after a cut."""

    def __init__(self, url: str, facility: str) -> None:
        scheme, rest = url.split("://", 1)
        host, port = rest.rsplit(":", 1)
        self.tcp = scheme == "tcp"
        super().__init__(address=(host, int(port)),
                         facility=logging.handlers.SysLogHandler.facility_names[facility],
                         socktype=socket.SOCK_STREAM if self.tcp else socket.SOCK_DGRAM)
        self.append_nul = False

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text + "\n" if self.tcp else text

    def emit(self, record: logging.LogRecord) -> None:
        if not self.tcp:
            super().emit(record)
            return
        try:
            msg = self.format(record)
            pri = self.encodePriority(self.facility, self.mapPriority(record.levelname))
            data = f"<{pri}>{msg}"
            self.socket.sendall(data.encode("utf-8"))
        except OSError:
            try:  # one reconnect, then give up on this record
                self.socket.close()
                self.socket = None  # type: ignore[assignment]
                self.createSocket()
                self.socket.sendall(data.encode("utf-8"))
            except Exception:  # noqa: BLE001
                self.handleError(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)


def configure(level: str = "info", log_file: Path | None = None, max_mb: int = 20,
              backups: int = 10, *, fmt: str = "text", syslog: str | None = None,
              facility: str = "local0", node: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FORMAT))
    handlers: list[logging.Handler] = [console]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_mb * 1024 * 1024, backupCount=backups, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter(node) if fmt == "json"
                                  else logging.Formatter(FORMAT))
        handlers.append(file_handler)
    if syslog:
        try:
            siem = SiemHandler(syslog, facility)
            siem.setFormatter(Rfc5424Formatter(node))
            handlers.append(siem)
        except OSError as exc:  # the SIEM is down: keep running with local logs
            print(f"syslog {syslog} unreachable ({exc}); logging locally only", flush=True)
    for handler in handlers:
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
