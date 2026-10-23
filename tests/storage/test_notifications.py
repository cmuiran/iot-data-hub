"""Notifications against a real SMTP server (aiosmtpd) and a local HTTP server."""

from __future__ import annotations

import asyncio
import email
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.policy import default as email_policy
from typing import Any

import pytest
import uvicorn
from aiosmtpd.controller import Controller
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from backend import notifications as notif
from backend.notifications import Notifier, render
from hub.models import DEVICE_ADAPTER, NotificationsFile
from simulators.mqtt_sim import free_port
from tests.hub.builders import modbus_device

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
VOLT = "kish.substation.meter_01.voltage"
TS = datetime(2026, 10, 1, 8, 0, tzinfo=UTC).isoformat()


def raised(source: str = VOLT, severity: str = "critical", **extra: Any) -> dict[str, Any]:
    return {"type": "alarm", "action": "raised", "source": source, "kind": "hi",
            "severity": severity, "message": "Voltage high", "value": 261.5, "ts": TS} | extra


# --- fake services ----------------------------------------------------------------------------


class Capture:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail_next = 0


@pytest.fixture
async def http_server() -> AsyncIterator[tuple[str, Capture]]:
    cap = Capture()

    async def record(request: Request) -> JSONResponse:
        body = await request.body()
        cap.requests.append({"path": request.url.path, "query": dict(request.query_params),
                             "headers": dict(request.headers), "body": body,
                             "method": request.method})
        if cap.fail_next:
            cap.fail_next -= 1
            return JSONResponse({"ok": False}, status_code=500)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/{path:path}", record, methods=["GET", "POST"])])
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="none",
                                           lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", cap
    finally:
        server.should_exit = True
        await task


class Mailbox:
    def __init__(self) -> None:
        self.messages: list[email.message.EmailMessage] = []

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        self.messages.append(email.message_from_bytes(envelope.content, policy=email_policy))
        return "250 OK"


@pytest.fixture
def smtp() -> Any:
    box = Mailbox()
    controller = Controller(box, hostname="127.0.0.1", port=free_port())
    controller.start()
    yield controller.port, box
    controller.stop()


def config(base: str, smtp_port: int, **overrides: Any) -> NotificationsFile:
    data = {
        "language": "fa",
        "max_per_minute": 20,
        "channels": {
            "ops_mail": {"type": "email", "host": "127.0.0.1", "port": smtp_port,
                         "security": "none", "sender": "hub@kish.local",
                         "recipients": ["ops@kish.local", "boss@kish.local"]},
            "ops_tg": {"type": "telegram", "bot_token_env": "TEST_TG_TOKEN",
                       "chat_ids": ["-100123"], "api_base": base},
            "sms": {"type": "sms", "url": f"{base}/sms/send", "method": "GET",
                    "params": {"to": "{recipient}", "text": "{message}", "key": "{api_key}"},
                    "api_key_env": "TEST_SMS_KEY", "recipients": ["09120000001", "09120000002"]},
            "hook": {"type": "webhook", "url": f"{base}/hook", "secret_env": "TEST_HOOK_SECRET"},
        },
        "rules": [{"name": "critical", "channels": ["ops_mail", "ops_tg", "sms", "hook"],
                   "min_severity": "critical", "events": ["raised", "cleared"]}],
    }
    return NotificationsFile.model_validate(data | overrides)


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_TG_TOKEN", "123:ABC")
    monkeypatch.setenv("TEST_SMS_KEY", "smskey")
    monkeypatch.setenv("TEST_HOOK_SECRET", "hooksecret")
    monkeypatch.setattr(notif, "RETRY_DELAYS_S", (0.05, 0.05))


async def notifier(store: Any, cfg: NotificationsFile) -> Notifier:
    await store.sync_config([DEVICE_ADAPTER.validate_python(modbus_device())])
    n = Notifier(cfg, store, timezone="Asia/Tehran",
                 scope_of=lambda s: ("kish", "substation") if "substation" in s or
                 s.startswith("device:") else None)
    await n.start()
    return n


def by_status(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for r in rows:
        out[(r["channel"], r["status"])] = out.get((r["channel"], r["status"]), 0) + 1
    return out


# --- tests ------------------------------------------------------------------------------------


def test_render_persian_and_english() -> None:
    subject, body = render(raised(), "fa", "Asia/Tehran")
    assert "آلارم" in subject and "بحرانی" in subject and VOLT in subject
    assert "261.5" in body and "11:30:00" in body          # 08:00 UTC = 11:30 Tehran
    subject_en, _ = render(raised(), "en", "UTC")
    assert subject_en.startswith("[hub] ALARM CRITICAL")


async def test_all_four_channels(store: Any, http_server: Any, smtp: Any) -> None:
    base, cap = http_server
    port, box = smtp
    n = await notifier(store, config(base, port))
    try:
        await n.on_alarm(raised())
        await n.drain()
    finally:
        await n.stop()
    assert by_status(await store.notification_log()) == {
        ("ops_mail", "sent"): 1, ("ops_tg", "sent"): 1, ("sms", "sent"): 1, ("hook", "sent"): 1}

    [mail] = box.messages
    assert mail["To"] == "ops@kish.local, boss@kish.local" and "بحرانی" in mail["Subject"]
    assert "261.5" in mail.get_content()

    tg = [r for r in cap.requests if r["path"] == "/bot123:ABC/sendMessage"]
    assert len(tg) == 1 and json.loads(tg[0]["body"])["chat_id"] == "-100123"

    sms = [r for r in cap.requests if r["path"] == "/sms/send"]
    assert [r["query"]["to"] for r in sms] == ["09120000001", "09120000002"]
    assert sms[0]["query"]["key"] == "smskey" and VOLT in sms[0]["query"]["text"]

    [hook] = [r for r in cap.requests if r["path"] == "/hook"]
    expected = hmac.new(b"hooksecret", hook["body"], hashlib.sha256).hexdigest()
    assert hook["headers"]["x-hub-signature-256"] == f"sha256={expected}"
    assert json.loads(hook["body"])["event"]["source"] == VOLT


async def test_rules_filter_and_cleared_events(store: Any, http_server: Any, smtp: Any) -> None:
    base, cap = http_server
    port, _ = smtp
    cfg = config(base, port, rules=[
        {"name": "only_hook", "channels": ["hook"], "min_severity": "high",
         "events": ["raised", "cleared"], "sites": ["kish"], "include_offline": False}])
    n = await notifier(store, cfg)
    try:
        await n.on_alarm(raised(severity="medium"))                 # below min_severity
        await n.on_alarm(raised(source="device:meter_01"))          # offline excluded
        await n.on_alarm(raised(source="other.site.dev.tag"))       # unknown scope vs sites
        await n.on_alarm({"type": "alarm", "action": "cleared", "source": "never.raised.x.y",
                          "ts": TS})                                # no matching raise
        await n.on_alarm(raised(severity="high"))
        await n.on_alarm({"type": "alarm", "action": "cleared", "source": VOLT, "ts": TS})
        await n.drain()
    finally:
        await n.stop()
    events = [json.loads(r["body"])["event"] for r in cap.requests]
    assert [(e["action"], e["severity"]) for e in events] == [("raised", "high"),
                                                              ("cleared", "high")]


async def test_shelved_source_is_suppressed(store: Any, http_server: Any, smtp: Any) -> None:
    base, cap = http_server
    port, _ = smtp
    user = await store.create_user("op.one", "Op", "h")
    await store.shelve(VOLT, user.user_id, "maintenance", datetime.now(UTC) + timedelta(hours=1))
    cfg = config(base, port, rules=[{"name": "r", "channels": ["hook"],
                                     "min_severity": "low"}])
    n = await notifier(store, cfg)
    try:
        await n.on_alarm(raised())
        await n.drain()
    finally:
        await n.stop()
    assert cap.requests == []
    assert by_status(await store.notification_log()) == {("hook", "suppressed"): 1}


async def test_throttle_and_retry(store: Any, http_server: Any, smtp: Any) -> None:
    base, cap = http_server
    port, _ = smtp
    cfg = config(base, port, max_per_minute=2,
                 rules=[{"name": "r", "channels": ["hook"], "min_severity": "low"}])
    cap.fail_next = 1
    n = await notifier(store, cfg)
    try:
        for i in range(3):
            await n.on_alarm(raised(source=f"kish.substation.meter_01.t{i}"))
        await n.drain()
        await asyncio.sleep(0.3)   # the retry is scheduled after a short delay
        await n.drain()
    finally:
        await n.stop()
    rows = await store.notification_log()
    assert by_status(rows) == {("hook", "failed"): 1, ("hook", "sent"): 2,
                               ("hook", "throttled"): 1}
    assert any(r["status"] == "sent" and r["attempt"] == 2 for r in rows)


async def test_missing_secret_and_test_message(store: Any, http_server: Any, smtp: Any,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    base, _ = http_server
    port, box = smtp
    n = await notifier(store, config(base, port))
    try:
        await n.send_test("ops_mail")
        assert box.messages and "آزمایشی" in box.messages[0]["Subject"]
        monkeypatch.delenv("TEST_TG_TOKEN")
        with pytest.raises(RuntimeError, match="TEST_TG_TOKEN"):
            await n.send_test("ops_tg")
    finally:
        await n.stop()
    assert by_status(await store.notification_log()) == {("ops_mail", "sent"): 1,
                                                         ("ops_tg", "failed"): 1}
