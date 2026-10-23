"""Alarm notifications: e-mail, Telegram, SMS (HTTP gateway) and webhooks.

The hub calls :meth:`Notifier.on_alarm` for every raised/cleared alarm. Matching rules
(config/notifications.yaml: severity, sites/areas, offline alarms, events) put one message per
channel on a queue; a worker sends them with up to 3 attempts (back-off 2 s, 8 s) and at most
``max_per_minute`` messages per channel (the rest is logged as ``throttled``). Shelved alarm
sources are logged as ``suppressed`` and not sent. Every attempt lands in ``notification_log``.

Secrets (SMTP password, bot token, API keys, webhook secret) come from environment variables.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import smtplib
import ssl
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from hub.drivers.base import utcnow
from hub.models import (
    AlarmSeverity,
    EmailChannel,
    NotificationRule,
    NotificationsFile,
    SmsChannel,
    TelegramChannel,
    WebhookChannel,
)

log = logging.getLogger(__name__)

_RANK = {AlarmSeverity.LOW: 0, AlarmSeverity.MEDIUM: 1, AlarmSeverity.HIGH: 2,
         AlarmSeverity.CRITICAL: 3}
RETRY_DELAYS_S = (2.0, 8.0)

_TEXT = {
    "fa": {"raised": "آلارم", "cleared": "رفع آلارم", "test": "پیام آزمایشی",
           "severity": {"low": "کم", "medium": "متوسط", "high": "زیاد", "critical": "بحرانی"},
           "value": "مقدار", "time": "زمان", "test_body": "اتصال کانال اطلاع‌رسانی برقرار است."},
    "en": {"raised": "ALARM", "cleared": "Alarm cleared", "test": "Test message",
           "severity": {"low": "low", "medium": "medium", "high": "high",
                        "critical": "CRITICAL"},
           "value": "value", "time": "time", "test_body": "The notification channel works."},
}


def _env(name: str | None) -> str | None:
    return os.environ.get(name) if name else None


def render(event: dict[str, Any], language: str, tz: str) -> tuple[str, str]:
    """(subject, body) of a notification."""
    t = _TEXT[language]
    action = event.get("action", "raised")
    when = datetime.fromisoformat(event["ts"]).astimezone(ZoneInfo(tz)).strftime(
        "%Y-%m-%d %H:%M:%S")
    if action == "test":
        return f"[hub] {t['test']}", f"{t['test_body']}\n{t['time']}: {when}"
    severity = str(event.get("severity") or "")
    sev = t["severity"].get(severity, severity)
    subject = f"[hub] {t[action]} {sev}: {event['source']}".strip()
    lines = [f"{t[action]} ({sev})" if sev else t[action], event["source"]]
    if event.get("message"):
        lines.append(str(event["message"]))
    if event.get("value") is not None:
        lines.append(f"{t['value']}: {event['value']}")
    lines.append(f"{t['time']}: {when}")
    return subject, "\n".join(lines)


@dataclass
class _Job:
    channel: str
    rule: str
    event: dict[str, Any]
    attempt: int = 1


class Notifier:
    def __init__(self, config: NotificationsFile, store: Any, *, timezone: str,
                 scope_of: Callable[[str], tuple[str, str] | None],
                 http: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.store = store
        self.timezone = timezone
        self.scope_of = scope_of             # source -> (site, area), None if unknown
        self._http_given = http
        self._http: httpx.AsyncClient | None = http
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=1000)
        self._sent: dict[str, deque[float]] = {}
        self._raised: dict[str, dict[str, Any]] = {}   # source -> raised event (for clears)
        self._task: asyncio.Task[None] | None = None
        self.counters: dict[tuple[str, str], int] = {}

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15)
        self._task = asyncio.create_task(self._worker(), name="notifier")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._http is not None and self._http_given is None:
            await self._http.aclose()
            self._http = None

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait until the queue is empty (tests, shutdown)."""
        await asyncio.wait_for(self._queue.join(), timeout)

    # --- matching -----------------------------------------------------------------------------

    def _matches(self, rule: NotificationRule, event: dict[str, Any]) -> bool:
        action = event.get("action")
        if action not in rule.events:
            return False
        source = event["source"]
        if source.startswith("device:") and not rule.include_offline:
            return False
        severity = event.get("severity")
        if severity is None or _RANK[AlarmSeverity(severity)] < _RANK[rule.min_severity]:
            return False
        scope = self.scope_of(source)
        if scope is None:
            return rule.sites is None and rule.areas is None
        site, area = scope
        if rule.sites is not None and site not in rule.sites:
            return False
        return rule.areas is None or area in rule.areas

    async def on_alarm(self, event: dict[str, Any]) -> None:
        if event.get("action") == "raised":
            self._raised[event["source"]] = event
        elif event.get("action") == "cleared":
            raised = self._raised.pop(event["source"], None)
            if raised is None:
                return
            event = {**event, "severity": raised.get("severity"),
                     "message": raised.get("message")}
        else:
            return
        shelves = await self.store.shelves(utcnow())
        for rule in self.config.rules:
            if not self._matches(rule, event):
                continue
            for channel in rule.channels:
                if event["source"] in shelves:
                    await self._log(channel, rule.name, event, "suppressed", 1,
                                    "source is shelved")
                    continue
                try:
                    self._queue.put_nowait(_Job(channel, rule.name, event))
                except asyncio.QueueFull:
                    await self._log(channel, rule.name, event, "failed", 1, "queue full")

    async def send_test(self, channel: str) -> None:
        """Send a test message now (raises on failure)."""
        event = {"action": "test", "source": "test", "ts": utcnow().isoformat()}
        try:
            await self._send(channel, event)
        except Exception as exc:
            await self._log(channel, "test", event, "failed", 1, str(exc)[:500])
            raise
        await self._log(channel, "test", event, "sent", 1, None)

    # --- worker -------------------------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            except Exception:  # noqa: BLE001
                log.exception("notification job failed")
            finally:
                self._queue.task_done()

    def _allow(self, channel: str) -> bool:
        now = time.monotonic()
        window = self._sent.setdefault(channel, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.config.max_per_minute:
            return False
        window.append(now)
        return True

    async def _process(self, job: _Job) -> None:
        if job.attempt == 1 and not self._allow(job.channel):
            await self._log(job.channel, job.rule, job.event, "throttled", 1,
                            f"more than {self.config.max_per_minute}/min")
            return
        try:
            await self._send(job.channel, job.event)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:500]
            await self._log(job.channel, job.rule, job.event, "failed", job.attempt, error)
            if job.attempt <= len(RETRY_DELAYS_S):
                delay = RETRY_DELAYS_S[job.attempt - 1]
                job.attempt += 1
                asyncio.get_running_loop().call_later(delay, self._requeue, job)
            else:
                log.error("notification to %s gave up: %s", job.channel, error)
            return
        await self._log(job.channel, job.rule, job.event, "sent", job.attempt, None)

    def _requeue(self, job: _Job) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(job)

    async def _log(self, channel: str, rule: str, event: dict[str, Any], status: str,
                   attempt: int, error: str | None) -> None:
        key = (channel, status)
        self.counters[key] = self.counters.get(key, 0) + 1
        try:
            await self.store.add_notification_log(
                channel=channel, rule=rule, event=str(event.get("action")),
                source=str(event.get("source")), status=status, attempt=attempt, error=error)
        except Exception:  # noqa: BLE001
            log.exception("could not write notification_log")

    # --- channels -----------------------------------------------------------------------------

    async def _send(self, name: str, event: dict[str, Any]) -> None:
        channel = self.config.channels.get(name)
        if channel is None:
            raise ValueError(f"unknown channel {name}")
        subject, body = render(event, self.config.language, self.timezone)
        if isinstance(channel, EmailChannel):
            await asyncio.to_thread(self._send_email, channel, subject, body)
        elif isinstance(channel, TelegramChannel):
            await self._send_telegram(channel, body)
        elif isinstance(channel, SmsChannel):
            await self._send_sms(channel, body)
        elif isinstance(channel, WebhookChannel):
            await self._send_webhook(channel, event, subject, body)

    @staticmethod
    def _send_email(c: EmailChannel, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, c.sender, ", ".join(c.recipients)
        msg.set_content(body, charset="utf-8")
        context = ssl.create_default_context()
        smtp_cls = smtplib.SMTP_SSL if c.security == "ssl" else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": 20}
        if c.security == "ssl":
            kwargs["context"] = context
        with smtp_cls(c.host, c.port, **kwargs) as smtp:
            if c.security == "starttls":
                smtp.starttls(context=context)
            user, password = _env(c.username_env), _env(c.password_env)
            if user:
                smtp.login(user, password or "")
            smtp.send_message(msg)

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15)
        return self._http

    async def _send_telegram(self, c: TelegramChannel, body: str) -> None:
        token = _env(c.bot_token_env)
        if not token:
            raise RuntimeError(f"environment variable {c.bot_token_env} is not set")
        for chat_id in c.chat_ids:
            r = await self.http.post(f"{c.api_base.rstrip('/')}/bot{token}/sendMessage",
                                      json={"chat_id": chat_id, "text": body})
            if r.status_code != 200 or not r.json().get("ok", False):
                raise RuntimeError(f"telegram answered {r.status_code}: {r.text[:200]}")

    async def _send_sms(self, c: SmsChannel, body: str) -> None:
        api_key = _env(c.api_key_env) or ""
        for recipient in c.recipients:
            values = {"message": body, "recipient": recipient, "api_key": api_key}
            url = c.url.format(**{k: quote(v, safe="") for k, v in values.items()})
            params = {k: v.format(**values) for k, v in c.params.items()}
            headers = {k: v.format(**values) for k, v in c.headers.items()}
            if c.method == "GET":
                r = await self.http.get(url, params=params, headers=headers)
            else:
                r = await self.http.post(url, data=params, headers=headers)
            if r.status_code >= 300:
                raise RuntimeError(f"SMS gateway answered {r.status_code}: {r.text[:200]}")

    async def _send_webhook(self, c: WebhookChannel, event: dict[str, Any], subject: str,
                            body: str) -> None:
        payload = json.dumps({"event": event, "subject": subject, "text": body},
                             ensure_ascii=False, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", **c.headers}
        secret = _env(c.secret_env)
        if secret:
            digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={digest}"
        r = await self.http.post(c.url, content=payload, headers=headers)
        if r.status_code >= 300:
            raise RuntimeError(f"webhook answered {r.status_code}: {r.text[:200]}")
