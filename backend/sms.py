"""SMS and voice calls from the hub: alerts to a home's contacts (automations' notify action)
and the replies to SMS commands. HUB_SMS_PROVIDER picks the gateway (backend.settings):

* ``none`` — nothing is sent (the log says so);
* ``log`` — kept in memory (development, tests): ``SmsOutbox.sent``;
* ``kavenegar`` — the Kavenegar REST API (SMS and text-to-speech calls). The API key is part
  of the provider's URL, so the request is never logged with it.

Received SMS reach the hub through ``POST /api/sms/inbound`` (backend.api.home), which the
provider calls with HUB_SMS_INBOUND_TOKEN.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from backend.settings import Settings

log = logging.getLogger(__name__)


def normalize_phone(phone: str) -> str:
    """One form for comparing numbers: digits only, Iranian 09.. / +989.. / 989.. -> 09.."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0098"):
        digits = "0" + digits[4:]
    elif digits.startswith("98") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits


def mask_phone(phone: str) -> str:
    d = normalize_phone(phone)
    return f"{d[:4]}***{d[-2:]}" if len(d) > 6 else "***"


class SmsGateway(Protocol):
    enabled: bool

    async def send(self, to: str, text: str) -> bool: ...

    async def call(self, to: str, text: str) -> bool: ...


@dataclass
class NoSms:
    enabled: bool = False

    async def send(self, to: str, text: str) -> bool:
        log.warning("SMS to %s not sent: HUB_SMS_PROVIDER is not set", mask_phone(to))
        return False

    async def call(self, to: str, text: str) -> bool:
        log.warning("call to %s not made: HUB_SMS_PROVIDER is not set", mask_phone(to))
        return False


@dataclass
class SmsOutbox:
    """Development and tests: the messages and calls stay in memory."""

    enabled: bool = True
    sent: list[tuple[str, str, str]] = field(default_factory=list)     # (kind, to, text)

    async def send(self, to: str, text: str) -> bool:
        self.sent.append(("sms", to, text))
        del self.sent[:-200]
        return True

    async def call(self, to: str, text: str) -> bool:
        self.sent.append(("call", to, text))
        del self.sent[:-200]
        return True


@dataclass
class Kavenegar:
    api_key: str
    sender: str | None = None
    enabled: bool = True
    base: str = "https://api.kavenegar.com/v1"
    timeout_s: float = 15

    async def _post(self, path: str, data: dict[str, str]) -> bool:
        url = f"{self.base}/{self.api_key}/{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, data=data)
            ok = r.status_code == 200 and r.json().get("return", {}).get("status") == 200
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("SMS gateway %s failed: %s", path, type(exc).__name__)
            return False
        if not ok:
            log.warning("SMS gateway %s refused (HTTP %s)", path, r.status_code)
        return ok

    async def send(self, to: str, text: str) -> bool:
        data = {"receptor": normalize_phone(to), "message": text}
        if self.sender:
            data["sender"] = self.sender
        ok = await self._post("sms/send.json", data)
        if ok:
            log.info("SMS sent to %s", mask_phone(to))
        return ok

    async def call(self, to: str, text: str) -> bool:
        ok = await self._post("call/maketts.json", {"receptor": normalize_phone(to),
                                                    "message": text})
        if ok:
            log.info("call made to %s", mask_phone(to))
        return ok


def make_sms(settings: Settings) -> SmsGateway:
    if settings.sms_provider == "log":
        return SmsOutbox()
    if settings.sms_provider == "kavenegar":
        if not settings.sms_api_key:
            log.error("HUB_SMS_PROVIDER=kavenegar needs HUB_SMS_API_KEY; SMS is off")
            return NoSms()
        # the key is in the URL: httpx must not log the requests (it does at INFO)
        logging.getLogger("httpx").setLevel(max(logging.WARNING,
                                                logging.getLogger("httpx").level))
        return Kavenegar(settings.sms_api_key, settings.sms_sender)
    return NoSms()
