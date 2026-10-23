"""Authentication: login with lockout, MFA (TOTP), rotating refresh tokens, idle timeout,
password history and expiry.

* Access token: JWT, ``access_token_minutes`` long, bound to a session id. Every request checks
  that the session is still valid, not idle for longer than ``session_idle_minutes``, and that
  the user is still active, so logout / disabling a user / changing a password acts at once.
* Refresh token: opaque random string, stored as SHA-256 only, rotated on every refresh. Using a
  refresh token that was already rotated (= copied by someone) revokes all of the user's sessions.
* Lockout: ``login_max_failures`` wrong passwords *or wrong MFA codes* lock the account for
  ``lockout_minutes``. Unknown users and locked users get the same answer as a wrong password.
* Throttle per client address: more than ``login_ip_max_failures`` failed logins within
  ``login_ip_window_s`` -> 429 until the window has passed (guessing across many usernames).
  Kept in memory: it resets on restart and is per node.
* MFA: a user with TOTP enabled gets an ``mfa_token`` (5 min) after the password and exchanges it
  with a 6-digit code or a one-time recovery code. Users holding a role listed in
  HUB_MFA_REQUIRED_ROLES who have not enrolled can only reach the MFA setup routes.
  TOTP secrets are encrypted with HUB_DATA_KEY.
* Passwords: the last ``password_history`` passwords cannot be reused; after
  ``password_max_age_days`` (0 = never) the user must change the password at the next login.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from backend import totp
from backend.crypto import CryptoError, seal, unseal
from backend.errors import Conflict, Invalid, TooManyRequests, Unauthorized, Unavailable
from backend.rbac import Principal
from backend.security import (
    TokenError,
    decode_access_token,
    decode_mfa_token,
    hash_password,
    hash_refresh_token,
    make_access_token,
    make_mfa_token,
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


@dataclass(frozen=True)
class MfaChallenge:
    mfa_token: str
    expires_in: int = 300


class AuthService:
    def __init__(self, store: PostgresStore, settings: Settings,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self._failures_by_addr: dict[str, deque[datetime]] = {}
        self.counters = {"login_ok": 0, "login_failed": 0, "login_throttled": 0,
                         "mfa_failed": 0}

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

    async def _check_throttle(self, username: str, client_addr: str | None,
                              now: datetime) -> None:
        recent = self._recent_failures(client_addr, now)
        if recent is not None and len(recent) >= self.settings.login_ip_max_failures:
            retry = int((recent[0] + timedelta(seconds=self.settings.login_ip_window_s)
                         - now).total_seconds()) + 1
            self.counters["login_throttled"] += 1
            await self.store.add_auth_audit("login_throttled", username, client_addr=client_addr,
                                            detail={"failures": len(recent)})
            raise TooManyRequests("too many failed logins from this address",
                                  code="login_throttled", extra={"retry_after_s": retry})

    async def _count_failure(self, user: UserRecord, client_addr: str | None, now: datetime,
                             event: str, reason: str) -> None:
        """A wrong password or MFA code: lockout bookkeeping + audit."""
        self._note_failure(client_addr, now)
        failures = user.failed_logins + 1
        locked_until = None
        if failures >= self.settings.login_max_failures:
            locked_until = now + timedelta(minutes=self.settings.lockout_minutes)
            failures = 0
        await self.store.update_user(user.user_id, failed_logins=failures,
                                     locked_until=locked_until)
        await self.store.add_auth_audit(event, user.username, user_id=user.user_id,
                                        client_addr=client_addr, detail={"reason": reason})
        if locked_until:
            await self.store.add_auth_audit("locked", user.username, user_id=user.user_id,
                                            client_addr=client_addr,
                                            detail={"until": locked_until.isoformat()})

    # --- login --------------------------------------------------------------------------------

    async def login(self, username: str, password: str, client_addr: str | None,
                    user_agent: str | None) -> TokenPair | MfaChallenge:
        now = self.clock()
        username = username.strip().lower()
        await self._check_throttle(username, client_addr, now)
        user = await self.store.get_user_by_username(username)
        ok = verify_password(user.password_hash if user else None, password)
        if user is None:
            self._note_failure(client_addr, now)
            await self.store.add_auth_audit("login_failed", username, client_addr=client_addr,
                                            detail={"reason": "unknown_user"})
            raise Unauthorized("invalid username or password", code="invalid_credentials")
        if not user.is_active:
            self._note_failure(client_addr, now)
            await self._failed(user, client_addr, "disabled")
            raise Unauthorized("invalid username or password", code="invalid_credentials")
        if user.locked_until and user.locked_until > now:
            self._note_failure(client_addr, now)
            await self._failed(user, client_addr, "locked")
            raise Unauthorized("account temporarily locked", code="account_locked",
                               extra={"locked_until": user.locked_until.isoformat()})
        if not ok:
            await self._count_failure(user, client_addr, now, "login_failed", "wrong_password")
            raise Unauthorized("invalid username or password", code="invalid_credentials")

        fields: dict[str, object] = {}
        if needs_rehash(user.password_hash):
            fields["password_hash"] = hash_password(password)
        max_age = self.settings.password_max_age_days
        if (max_age and user.password_changed_at is not None and not user.must_change_password
                and now - user.password_changed_at > timedelta(days=max_age)):
            fields["must_change_password"] = True
            await self.store.add_auth_audit("password_expired", user.username,
                                            user_id=user.user_id, client_addr=client_addr)
        if fields:
            user = await self.store.update_user(user.user_id, **fields) or user
        if user.totp_enabled:
            return MfaChallenge(make_mfa_token(user.user_id, self.settings.jwt_secret, now))
        return await self._complete_login(user, client_addr, user_agent, mfa=False)

    async def verify_mfa(self, mfa_token: str, code: str | None, recovery_code: str | None,
                         client_addr: str | None, user_agent: str | None) -> TokenPair:
        now = self.clock()
        try:
            user_id = decode_mfa_token(mfa_token, self.settings.jwt_secret)
        except TokenError as exc:
            raise Unauthorized(f"invalid MFA token: {exc}", code="invalid_token") from exc
        user = await self.store.get_user(user_id)
        if user is None or not user.is_active or not user.totp_enabled:
            raise Unauthorized("invalid MFA token", code="invalid_token")
        await self._check_throttle(user.username, client_addr, now)
        if user.locked_until and user.locked_until > now:
            raise Unauthorized("account temporarily locked", code="account_locked",
                               extra={"locked_until": user.locked_until.isoformat()})
        mfa = await self.store.get_mfa(user_id)
        if code:
            secret = self._unseal(mfa["totp_secret_enc"])
            step = totp.verify(secret, code, now.timestamp(), mfa["totp_last_step"])
            if step is None:
                self.counters["mfa_failed"] += 1
                await self._count_failure(user, client_addr, now, "mfa_failed", "wrong_code")
                raise Unauthorized("invalid code", code="invalid_mfa_code")
            await self.store.update_user(user_id, totp_last_step=step)
        elif recovery_code:
            wanted = totp.hash_recovery_code(recovery_code)
            codes = list(mfa["recovery_codes"])
            if wanted not in codes:
                self.counters["mfa_failed"] += 1
                await self._count_failure(user, client_addr, now, "mfa_failed",
                                          "wrong_recovery_code")
                raise Unauthorized("invalid code", code="invalid_mfa_code")
            codes.remove(wanted)
            await self.store.update_user(user_id, recovery_codes=codes)
            log.warning("%s logged in with a recovery code (%d left)", user.username, len(codes))
        else:
            raise Invalid("code or recovery_code is required", code="invalid")
        return await self._complete_login(user, client_addr, user_agent, mfa=True)

    async def _complete_login(self, user: UserRecord, client_addr: str | None,
                              user_agent: str | None, *, mfa: bool) -> TokenPair:
        now = self.clock()
        await self.store.update_user(user.user_id, failed_logins=0, locked_until=None,
                                     last_login_at=now)
        self.counters["login_ok"] += 1
        await self.store.add_auth_audit("login_ok", user.username, user_id=user.user_id,
                                        client_addr=client_addr, detail={"mfa": mfa})
        return await self._new_session(user, client_addr, user_agent, mfa=mfa)

    async def _failed(self, user: UserRecord, client_addr: str | None, reason: str) -> None:
        await self.store.add_auth_audit("login_failed", user.username, user_id=user.user_id,
                                        client_addr=client_addr, detail={"reason": reason})

    async def _new_session(self, user: UserRecord, client_addr: str | None,
                           user_agent: str | None, *, mfa: bool) -> TokenPair:
        now = self.clock()
        session_id = uuid.uuid4()
        refresh, refresh_hash = new_refresh_token()
        await self.store.create_session(
            session_id, user.user_id, refresh_hash,
            now + timedelta(hours=self.settings.refresh_token_hours), client_addr,
            (user_agent or "")[:300] or None, mfa=mfa)
        access = make_access_token(user.user_id, session_id, self.settings.jwt_secret, now,
                                   self.settings.access_token_minutes)
        return TokenPair(access, refresh, self.settings.access_token_minutes * 60,
                         user.must_change_password)

    # --- refresh / logout ---------------------------------------------------------------------

    def _idle(self, last_seen: datetime | None, now: datetime) -> bool:
        return last_seen is not None and now - last_seen > timedelta(
            minutes=self.settings.session_idle_minutes)

    async def refresh(self, refresh_token: str, client_addr: str | None,
                      user_agent: str | None) -> TokenPair:
        now = self.clock()
        session = await self.store.get_session_by_hash(hash_refresh_token(refresh_token))
        if session is None:
            raise Unauthorized("invalid refresh token", code="invalid_token")
        if session.revoked_at is not None:
            # A rotated token came back: someone else has a copy. Kill every session.
            await self.store.revoke_user_sessions(session.user_id)
            log.warning("refresh token reuse for user %s; all sessions revoked", session.user_id)
            raise Unauthorized("refresh token already used", code="invalid_token")
        if session.expires_at <= now:
            raise Unauthorized("session expired", code="session_expired")
        user = await self.store.get_user(session.user_id)
        if user is None or not user.is_active:
            raise Unauthorized("user disabled", code="invalid_token")
        if self._idle(session.last_seen_at, now):
            await self._expire_idle(session.session_id, user.username, user.user_id)
        await self.store.revoke_session(session.session_id)
        return await self._new_session(user, client_addr, user_agent, mfa=session.mfa)

    async def _expire_idle(self, session_id: Any, username: str, user_id: int) -> None:
        await self.store.revoke_session(session_id)
        await self.store.add_auth_audit("session_expired", username, user_id=user_id,
                                        detail={"reason": "idle"})
        raise Unauthorized("session ended after inactivity", code="session_idle")

    async def logout(self, principal: Principal, client_addr: str | None) -> None:
        await self.store.revoke_session(principal.session_id)  # type: ignore[arg-type]
        await self.store.add_auth_audit("logout", principal.username, user_id=principal.user_id,
                                        client_addr=client_addr)

    # --- per request --------------------------------------------------------------------------

    async def principal(self, access_token: str, *, touch: bool = True) -> Principal:
        now = self.clock()
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
        if self._idle(session.last_seen_at, now):
            await self._expire_idle(session_id, user.username, user_id)
        if touch:
            await self.store.touch_session(session_id, now)
        grants = tuple(await self.store.grants(user_id))
        required = set(self.settings.mfa_required_roles)
        mfa_missing = bool(required) and not user.totp_enabled and any(
            g.role_id in required for g in grants)
        return Principal(user=user, session_id=session_id, grants=grants,
                         role_permissions=await self.store.role_permissions(),
                         mfa_missing=mfa_missing)

    # --- MFA enrolment ------------------------------------------------------------------------

    def _key(self) -> str:
        if not self.settings.data_key:
            raise Unavailable("MFA needs HUB_DATA_KEY (it encrypts the TOTP secrets)",
                              code="no_data_key")
        return self.settings.data_key

    def _unseal(self, token: str | None) -> str:
        if not token:
            raise Unauthorized("MFA is not set up", code="invalid_token")
        try:
            return unseal(self._key(), token, "totp")
        except CryptoError as exc:
            log.error("cannot decrypt a TOTP secret: %s (HUB_DATA_KEY changed?)", exc)
            raise Unavailable("MFA secret cannot be read; an administrator must reset MFA",
                              code="mfa_unreadable") from exc

    async def mfa_setup(self, principal: Principal) -> dict[str, str]:
        if principal.user.totp_enabled:
            raise Conflict("MFA is already enabled; disable it first", code="mfa_enabled")
        secret = totp.new_secret()
        await self.store.update_user(principal.user_id,
                                     totp_pending_enc=seal(self._key(), secret, "totp"))
        return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret,
                                                                       principal.username)}

    async def mfa_enable(self, principal: Principal, code: str,
                         client_addr: str | None) -> list[str]:
        mfa = await self.store.get_mfa(principal.user_id)
        if not mfa.get("totp_pending_enc"):
            raise Conflict("start with /api/auth/mfa/setup", code="mfa_not_started")
        secret = self._unseal(mfa["totp_pending_enc"])
        step = totp.verify(secret, code, self.clock().timestamp(), None)
        if step is None:
            raise Invalid("the code does not match; check the phone's clock",
                          code="invalid_mfa_code")
        codes = totp.new_recovery_codes()
        await self.store.update_user(principal.user_id, totp_secret_enc=mfa["totp_pending_enc"],
                                     totp_pending_enc=None, totp_enabled=True,
                                     totp_last_step=step,
                                     recovery_codes=[totp.hash_recovery_code(c) for c in codes])
        await self.store.add_auth_audit("mfa_enabled", principal.username,
                                        user_id=principal.user_id, actor_id=principal.user_id,
                                        client_addr=client_addr)
        return codes

    async def mfa_disable(self, principal: Principal, password: str, code: str,
                          client_addr: str | None) -> None:
        if not principal.user.totp_enabled:
            raise Conflict("MFA is not enabled", code="mfa_not_enabled")
        if not verify_password(principal.user.password_hash, password):
            raise Unauthorized("password is wrong", code="invalid_credentials")
        mfa = await self.store.get_mfa(principal.user_id)
        if totp.verify(self._unseal(mfa["totp_secret_enc"]), code, self.clock().timestamp(),
                       mfa["totp_last_step"]) is None:
            raise Unauthorized("invalid code", code="invalid_mfa_code")
        await self._clear_mfa(principal.user_id)
        await self.store.add_auth_audit("mfa_disabled", principal.username,
                                        user_id=principal.user_id, actor_id=principal.user_id,
                                        client_addr=client_addr)

    async def mfa_reset(self, user: UserRecord, actor: Principal | None,
                        client_addr: str | None) -> None:
        """Administrator action: the user lost the phone (they enrol again at next login)."""
        await self._clear_mfa(user.user_id)
        await self.store.revoke_user_sessions(user.user_id)
        await self.store.add_auth_audit("mfa_reset", user.username, user_id=user.user_id,
                                        actor_id=actor.user_id if actor else None,
                                        client_addr=client_addr)

    async def _clear_mfa(self, user_id: int) -> None:
        await self.store.update_user(user_id, totp_secret_enc=None, totp_pending_enc=None,
                                     totp_enabled=False, totp_last_step=None, recovery_codes=[])

    # --- passwords ----------------------------------------------------------------------------

    def check_password(self, password: str, username: str) -> None:
        problems = password_problems(password, username, self.settings.password_min_length)
        if problems:
            raise Invalid("password: " + "; ".join(problems), code="weak_password",
                          extra={"problems": problems})

    async def set_password(self, user: UserRecord, new: str, *, must_change: bool) -> None:
        """Store a new password: policy, history check, history update, timestamps."""
        self.check_password(new, user.username)
        keep = self.settings.password_history
        history = list((await self.store.get_mfa(user.user_id)).get("password_history") or [])
        if keep and any(verify_password(h, new) for h in [user.password_hash, *history[:keep]]):
            raise Invalid(f"choose a password you have not used for the last {keep} changes",
                          code="password_reused")
        history = [user.password_hash, *history][:keep] if keep else []
        await self.store.update_user(user.user_id, password_hash=hash_password(new),
                                     must_change_password=must_change, password_history=history,
                                     password_changed_at=self.clock(), failed_logins=0,
                                     locked_until=None)

    async def change_password(self, principal: Principal, current: str, new: str,
                              client_addr: str | None) -> None:
        if not verify_password(principal.user.password_hash, current):
            raise Unauthorized("current password is wrong", code="invalid_credentials")
        if current == new:
            raise Invalid("new password must differ", code="weak_password")
        await self.set_password(principal.user, new, must_change=False)
        await self.store.revoke_user_sessions(principal.user_id)
        await self.store.add_auth_audit("password_changed", principal.username,
                                        user_id=principal.user_id, actor_id=principal.user_id,
                                        client_addr=client_addr)
