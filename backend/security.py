"""Passwords (argon2id), access tokens (short HS256 JWT) and refresh tokens (opaque, rotated)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
_ISSUER = "iot-data-hub"
# Used to spend the same time on unknown usernames as on wrong passwords.
_DUMMY_HASH = _hasher.hash("not-a-real-password-" + secrets.token_hex(8))


class TokenError(Exception):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problems(password: str, username: str, min_length: int) -> list[str]:
    """Simple, explainable policy: length, not the username, not one repeated character."""
    problems = []
    if len(password) < min_length:
        problems.append(f"at least {min_length} characters")
    if username and username.lower() in password.lower():
        problems.append("must not contain the username")
    if len(set(password)) < 4:
        problems.append("too few different characters")
    return problems


def make_access_token(user_id: int, session_id: UUID, secret: str, now: datetime,
                      minutes: int) -> str:
    payload = {"sub": str(user_id), "sid": str(session_id), "iss": _ISSUER,
               "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=minutes))
                                                       .timestamp()), "typ": "access"}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> tuple[int, UUID]:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], issuer=_ISSUER,
                             options={"require": ["exp", "sub", "sid", "iss"]})
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("typ") != "access":
        raise TokenError("not an access token")
    try:
        return int(payload["sub"]), UUID(payload["sid"])
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed token") from exc


def new_refresh_token() -> tuple[str, str]:
    """(token for the client, SHA-256 hex stored in the database)."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
