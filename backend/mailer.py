"""Outgoing account e-mail (verification, password reset, invitations).

Texts come from backend/messages/emails.json (fa, en), never from code. SMTP settings are
HUB_SMTP_* (backend.settings); without HUB_SMTP_HOST nothing is sent and the log says so
(the message itself is not logged: it contains a one-time link). Sending runs in a worker
thread so the event loop never blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from backend.settings import Settings

log = logging.getLogger(__name__)
MESSAGES = Path(__file__).with_name("messages") / "emails.json"


@cache
def messages() -> dict[str, Any]:
    return json.loads(MESSAGES.read_text(encoding="utf-8"))


def render(template: str, language: str, **values: Any) -> tuple[str, str]:
    lang = messages().get(language) or messages()["en"]
    text = lang[template]
    return text["subject"].format(**values), text["body"].format(**values)


def label(kind: str, key: str, language: str) -> str:
    """A role or access level in the reader's language."""
    lang = messages().get(language) or messages()["en"]
    return str(lang.get(kind, {}).get(key, key))


def mask(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


class Mailer(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


@dataclass
class Outbox:
    """No SMTP configured (or tests): keeps the messages in memory."""

    sent: list[tuple[str, str, str]] = field(default_factory=list)
    warn: bool = True

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))
        del self.sent[:-200]
        if self.warn:
            log.warning("e-mail to %s not sent: HUB_SMTP_HOST is not set", mask(to))


@dataclass
class SmtpMailer:
    settings: Settings

    async def send(self, to: str, subject: str, body: str) -> None:
        await asyncio.to_thread(self._send, to, subject, body)
        log.info("e-mail sent to %s", mask(to))

    def _send(self, to: str, subject: str, body: str) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, s.mail_from, to
        msg.set_content(body, charset="utf-8")
        context = ssl.create_default_context()
        smtp_cls = smtplib.SMTP_SSL if s.smtp_security == "ssl" else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": 20}
        if s.smtp_security == "ssl":
            kwargs["context"] = context
        if s.smtp_host is None:                 # make_mailer only builds this with a host
            raise RuntimeError("HUB_SMTP_HOST is not set")
        with smtp_cls(s.smtp_host, s.smtp_port, **kwargs) as smtp:
            if s.smtp_security == "starttls":
                smtp.starttls(context=context)
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password or "")
            smtp.send_message(msg)


def make_mailer(settings: Settings) -> Mailer:
    return SmtpMailer(settings) if settings.smtp_host else Outbox()
