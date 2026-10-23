"""TOTP (RFC 6238: HMAC-SHA1, 30 s steps, 6 digits) and recovery codes.

Works with Google Authenticator, Microsoft Authenticator, FreeOTP, Aegis, 1Password, ...
A code is accepted for the current step and one step either side (clock drift), and never twice
(the last accepted step is stored per user, so a code seen over a shoulder cannot be replayed).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

STEP_S = 30
DIGITS = 6
ISSUER = "IoT Data Hub"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def step_of(unix_time: float) -> int:
    return int(unix_time // STEP_S)


def verify(secret: str, code: str, unix_time: float, last_step: int | None,
           window: int = 1) -> int | None:
    """The matching step, or None. Steps at or before ``last_step`` are refused (replay)."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    now = step_of(unix_time)
    for step in range(now - window, now + window + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, username: str, issuer: str = ISSUER) -> str:
    label = quote(f"{issuer}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_S}")


def new_recovery_codes(count: int = 10) -> list[str]:
    """Human-friendly one-time codes like ``k7q2-9fzt``."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" +
            "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(count)]


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
