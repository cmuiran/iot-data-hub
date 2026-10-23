"""Authentication: login with lockout, rotating refresh tokens, logout, password changes.

* Access token: JWT, ``access_token_minutes`` long, bound to a session id. Every request checks
  that the session is still valid and the user still active, so logout / disabling a user /
  changing a password takes effect at once.
* Refresh token: opaque random string, stored as SHA-256 only, rotated on every refresh. Using a
  refresh token that was already rotated (= copied by someone) revokes all of the user's sessions.
* Lockout: ``login_max_failures`` wrong passwords lock the account for ``lockout_minutes``.
  Unknown users and locked users get the same answer as a wrong password.
* Throttle per client address: more than ``login_ip_max_failures`` failed logins within
  ``login_ip_window_s`` -> 429 until the window has passed (guessing across many usernames).
  Kept in memory: it resets on restart and is per node.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.errors import Invalid, TooManyRequests, Unauthorized
from backend.rbac import Principal
from backend.security import (
    TokenError,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    make_access_token,
    needs_rehash,
    new_refresh_token,
    password_problems,
    verify_password,
)
from backend.settings import Settings
from hub.drivers.base import utcnow
from hub.storage.postgres import PostgresStore
from hub.storage.records import UserRecord

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    must_change_password: bool


class AuthService:
    def __init__(self, store: PostgresStore, settings: Settings,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self._failures_by_addr: dict[str, deque[datetime]] = {}
        self.counters = {"login_ok": 0, "login_failed": 0, "login_throttled": 0}

    def _recent_failures(self, addr: str | None, now: datetime) -> deque[datetime] | None:
        if not addr:
            return None
        window = timedelta(seconds=self.settings.login_ip_window_s)
        failures = self._failures_by_addr.setdefault(addr, deque())
        while failures and now - failures[0] > window:
            failures.popleft()
        return failures

    def _note_failure(self, addr: str | None, now: datetime) -> None:
        self.counters["login_failed"] += 1
        failures = self._recent_failures(addr, now)
        if failures is not None:
            failures.append(now)

    # --- login --------------------------------------------------------------------------------

    async def login(self, username: str, password: str, client_addr: str | None,
                    user_agent: str | None) -> TokenPair:
        now = self.clock()
        username = username.strip().lower()
        recent = self._recent_failures(client_addr, now)
        if recent is not None and len(recent) >= self.settings.login_ip_max_failures:
            retry = int((recent[0] + timedelta(seconds=self.settings.login_ip_window_s)
                         - now).total_seconds()) + 1
            self.counters["login_throttled"] += 1
            await self.store.add_auth_audit("login_throttled", username, client_addr=client_addr,
                                            detail={"failures": len(recent)})
            raise TooManyRequests("too many failed logins from this address",
                                  code="login_throttled", extra={"retry_after_s": retry})
        user = await self.store.get_user_by_username(username)
        ok = verify_password(user.password_hash if user else None, password)
        if not ok or user is None or not user.is_active or (
                user.locked_until and user.locked_until > now):
            self._note_failure(client_addr, now)
        if user is None:
            await self.store.add_auth_audit("login_failed", username, client_addr=client_addr,
                                            detail={"reason": "unknown_user"})
            raise Unauthorized("invalid username or password", code="invalid_credentials")
        if not user.is_active:
            await self._failed(user, client_addr, "disabled")
            raise Unauthorized("invalid username or password", code="invalid_credentials")
        if user.locked_until and user.locked_until > now:
            await self._failed(user, client_addr, "locked")
            raise Unauthorized("account temporarily locked", code="account_locked",
                               extra={"locked_until": user.locked_until.isoformat()})
        if not ok:
            failures = user.failed_logins + 1
            locked_until = None
            if failures >= self.settings.login_max_failures:
                locked_until = now + timedelta(minutes=self.settings.lockout_minutes)
                failures = 0
            await self.store.update_user(user.user_id, failed_logins=failures,
                                         locked_until=locked_until)
            await self._failed(user, client_addr, "wrong_password")
            if locked_until:
                await self.store.add_auth_audit("locked", user.username, user_id=user.user_id,
                                                client_addr=client_addr,
                                                detail={"until": locked_until.isoformat()})
            raise Unauthorized("invalid username or password", code="invalid_credentials")

        fields: dict[str, object] = {"failed_logins": 0, "locked_until": None,
                                     "last_login_at": now}
        if needs_rehash(user.password_hash):
            fields["password_hash"] = hash_password(password)
        await self.store.update_user(user.user_id, **fields)
        self.counters["login_ok"] += 1
        await self.store.add_auth_audit("login_ok", user.username, user_id=user.user_id,
                                        client_addr=client_addr)
        return await self._new_session(user, client_addr, user_agent)

    async def _failed(self, user: UserRecord, client_addr: str | None, reason: str) -> None:
        await self.store.add_auth_audit("login_failed", user.username, user_id=user.user_id,
                                        client_addr=client_addr, detail={"reason": reason})

    async def _new_session(self, user: UserRecord, client_addr: str | None,
                           user_agent: str | None) -> TokenPair:
        now = self.clock()
        session_id = uuid.uuid4()
        refresh, refresh_hash = new_refresh_token()
        await self.store.create_session(
            session_id, user.user_id, refresh_hash,
            now + timedelta(hours=self.settings.refresh_token_hours), client_addr,
            (user_agent or "")[:300] or None)
        access = make_access_token(user.user_id, session_id, self.settings.jwt_secret, now,
                                   self.settings.access_token_minutes)
        return TokenPair(access, refresh, self.settings.access_token_minutes * 60,
                         user.must_change_password)

    # --- refresh / logout ---------------------------------------------------------------------

    async def refresh(self, refresh_token: str, client_addr: str | None,
                      user_agent: str | None) -> TokenPair:
        session = await self.store.get_session_by_hash(hash_refresh_token(refresh_token))
        if session is None:
            raise Unauthorized("invalid refresh token", code="invalid_token")
        if session.revoked_at is not None:
            # A rotated token came back: someone else has a copy. Kill every session.
            await self.store.revoke_user_sessions(session.user_id)
            log.warning("refresh token reuse for user %s; all sessions revoked", session.user_id)
            raise Unauthorized("refresh token already used", code="invalid_token")
        if session.expires_at <= self.clock():
            raise Unauthorized("session expired", code="session_expired")
        user = await self.store.get_user(session.user_id)
        if user is None or not user.is_active:
            raise Unauthorized("user disabled", code="invalid_token")
        await self.store.revoke_session(session.session_id)
        return await self._new_session(user, client_addr, user_agent)

    async def logout(self, principal: Principal, client_addr: str | None) -> None:
        await self.store.revoke_session(principal.session_id)  # type: ignore[arg-type]
        await self.store.add_auth_audit("logout", principal.username, user_id=principal.user_id,
                                        client_addr=client_addr)

    # --- per request --------------------------------------------------------------------------

    async def principal(self, access_token: str) -> Principal:
        try:
            user_id, session_id = decode_access_token(access_token, self.settings.jwt_secret)
        except TokenError as exc:
            raise Unauthorized(f"invalid token: {exc}", code="invalid_token") from exc
        session = await self.store.get_session(session_id)
        if session is None or session.revoked_at is not None or session.user_id != user_id:
            raise Unauthorized("session ended", code="invalid_token")
        user = await self.store.get_user(user_id)
        if user is None or not user.is_active:
            raise Unauthorized("user disabled", code="invalid_token")
        grants = tuple(await self.store.grants(user_id))
        return Principal(user=user, session_id=session_id, grants=grants,
                         role_permissions=await self.store.role_permissions())

    # --- passwords ----------------------------------------------------------------------------

    def check_password(self, password: str, username: str) -> None:
        problems = password_problems(password, username, self.settings.password_min_length)
        if problems:
            raise Invalid("password: " + "; ".join(problems), code="weak_password",
                          extra={"problems": problems})

    async def change_password(self, principal: Principal, current: str, new: str,
                              client_addr: str | None) -> None:
        if not verify_password(principal.user.password_hash, current):
            raise Unauthorized("current password is wrong", code="invalid_credentials")
        if current == new:
            raise Invalid("new password must differ", code="weak_password")
        self.check_password(new, principal.username)
        await self.store.update_user(principal.user_id, password_hash=hash_password(new),
                                     must_change_password=False)
        await self.store.revoke_user_sessions(principal.user_id)
        await self.store.add_auth_audit("password_changed", principal.username,
                                        user_id=principal.user_id, actor_id=principal.user_id,
                                        client_addr=client_addr)
