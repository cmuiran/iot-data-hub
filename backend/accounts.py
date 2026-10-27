"""Self-service accounts: sign-up with e-mail, verification, password reset, invitations to an
organization (family members, staff) and sharing a device with another organization.

Rules:

* public sign-up only when HUB_SIGNUP_ENABLED; an invitation lets its invitee sign up anyway
  (the invitation link proves the e-mail address, so that account is verified at once);
* the answers of sign-up, resend and forgot-password do not tell whether an address has an
  account (no enumeration); the owner of an existing address gets an e-mail instead;
* sign-in with an e-mail address needs a verified address;
* every token is random (256 bit), stored as SHA-256, used once, and expires;
* per client address and per e-mail address the actions are rate limited;
* a verified account gets a personal organization (kind customer) it owns; members join
  organizations by invitation; a device is shared with another organization by its owner.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from backend.errors import Conflict, Forbidden, Invalid, NotFound, TooManyRequests
from backend.mailer import Mailer, label, render
from backend.rbac import Principal
from backend.security import hash_password
from backend.settings import Settings
from hub.drivers.base import utcnow
from hub.storage.accounts import InvitationRecord, MembershipRecord
from hub.storage.platform import PlatformError
from hub.storage.records import UserRecord

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+")
# which roles an organization can give, by its kind
INVITABLE_ROLES = {
    "customer": ("customer_owner", "customer_member", "customer_viewer"),
    "manufacturer": ("manufacturer_admin", "production_operator", "manufacturer_support"),
}
SHARE_LEVELS = ("operator", "viewer")


def normalize_email(value: str) -> str:
    value = value.strip()
    if len(value) > 254 or not EMAIL_RE.fullmatch(value):
        raise Invalid("not an e-mail address", code="invalid_email")
    local, _, domain = value.partition("@")
    return f"{local}@{domain.lower()}"


def same_email(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.casefold() == b.casefold()


def new_token() -> tuple[str, str]:
    """(token for the link, its SHA-256 for the database)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_username() -> str:
    """Self-registered users sign in with their e-mail; the username is internal."""
    return "u" + base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()


class AccountService:
    def __init__(self, store: Any, settings: Settings, auth: Any, mailer: Mailer,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.accounts = store.accounts
        self.settings = settings
        self.auth = auth
        self.mailer = mailer
        self.clock = clock
        self._hits: dict[tuple[str, str], deque[datetime]] = {}

    # --- helpers ------------------------------------------------------------------------------

    def link(self, route: str, token: str) -> str:
        """Links carry the token in the fragment: it never reaches server or proxy logs."""
        return f"{self.settings.public_url or ''}/#/account/{route}/{token}"

    async def _limit(self, action: str, key: str | None, limit: int,
                     client_addr: str | None) -> None:
        if not key:
            return
        now = self.clock()
        hits = self._hits.setdefault((action, key), deque())
        while hits and now - hits[0] > timedelta(hours=1):
            hits.popleft()
        if len(hits) >= limit:
            await self.store.add_auth_audit("account_throttled", "-", client_addr=client_addr,
                                            detail={"action": action})
            raise TooManyRequests("too many requests; try again later",
                                  code="account_throttled")
        hits.append(now)
        if len(self._hits) > 50_000:            # bounded memory: forget the oldest keys
            for old in list(self._hits)[:10_000]:
                del self._hits[old]

    async def _ip_limit(self, action: str, client_addr: str | None) -> None:
        await self._limit(action, client_addr, self.settings.account_ip_per_hour, client_addr)

    async def _mail(self, email: str, template: str, language: str, **values: Any) -> bool:
        """Send, at most account_email_per_hour per address; a failure is logged, not raised
        (the caller's answer must not depend on it)."""
        try:
            await self._limit("mail", email.casefold(), self.settings.account_email_per_hour,
                              None)
        except TooManyRequests:
            log.warning("account e-mail %s not sent: limit per address reached", template)
            return False
        subject, body = render(template, language, **values)
        try:
            await self.mailer.send(email, subject, body)
        except Exception:  # noqa: BLE001 - mail trouble must not change the answer
            log.exception("account e-mail %s could not be sent", template)
            return False
        return True

    # --- sign-up and verification -------------------------------------------------------------

    async def signup(self, *, email: str, password: str, display_name: str, language: str,
                     invitation_token: str | None, client_addr: str | None) -> dict[str, Any]:
        await self._ip_limit("signup", client_addr)
        email = normalize_email(email)
        invitation = None
        if invitation_token:
            invitation = await self.accounts.invitation_by_token(token_hash(invitation_token),
                                                                 self.clock())
            if invitation is None:
                raise NotFound("invitation not found, used or expired",
                               code="invalid_invitation")
            if not same_email(invitation.email, email):
                raise Forbidden("the invitation is for another e-mail address",
                                code="invitation_other_email")
        elif not self.settings.signup_enabled:
            raise Forbidden("sign-up is not open on this hub; ask for an invitation",
                            code="signup_disabled")
        self.auth.check_password(password, email.partition("@")[0])

        existing = await self.store.get_user_by_email(email)
        if existing is not None:
            if invitation is not None:   # the invitee proved the address: say it plainly
                raise Conflict("this e-mail address has an account: sign in and accept the "
                               "invitation", code="email_taken")
            await self._mail(email, "account_exists", existing.language,
                             link=self.link("forgot", ""))
            return {"status": "check_email"}

        now = self.clock()
        try:
            user = await self.accounts.create_user(
                username=new_username(), display_name=display_name, email=email,
                password_hash=hash_password(password), language=language,
                verified_at=now if invitation is not None else None)
        except PlatformError:            # same address registered meanwhile
            return {"status": "check_email"}
        await self.store.add_auth_audit("account_signup", user.username, user_id=user.user_id,
                                        client_addr=client_addr,
                                        detail={"invited": invitation is not None})
        if invitation is not None:
            await self._activate(user, client_addr)
            await self._accept(user, invitation, None, client_addr)
            return {"status": "active"}
        await self._send_verification(user)
        return {"status": "check_email"}

    async def _send_verification(self, user: UserRecord) -> None:
        token, hashed = new_token()
        await self.accounts.add_token(user.user_id, "verify_email", hashed,
                                      self.clock() + timedelta(hours=self.settings.verify_hours))
        if user.email:
            await self._mail(user.email, "verify", user.language, name=user.display_name,
                             link=self.link("verify", token), hours=self.settings.verify_hours)

    async def _activate(self, user: UserRecord, client_addr: str | None) -> str:
        await self.accounts.mark_verified(user.user_id, self.clock())
        org_id = await self.accounts.ensure_personal_org(user, user.display_name)
        await self.store.add_auth_audit("email_verified", user.username, user_id=user.user_id,
                                        client_addr=client_addr, detail={"org_id": org_id})
        return org_id

    async def verify(self, token: str, client_addr: str | None) -> dict[str, Any]:
        await self._ip_limit("verify", client_addr)
        user_id = await self.accounts.use_token(token_hash(token), "verify_email", self.clock())
        user = await self.store.get_user(user_id) if user_id is not None else None
        if user is None:
            raise NotFound("link not found, used or expired", code="invalid_token")
        org_id = await self._activate(user, client_addr)
        return {"status": "verified", "organization": org_id}

    async def resend(self, email: str, client_addr: str | None) -> dict[str, Any]:
        await self._ip_limit("resend", client_addr)
        user = await self.store.get_user_by_email(normalize_email(email))
        if user is not None and user.email_verified_at is None and user.is_active:
            await self._send_verification(user)
        return {"status": "check_email"}

    # --- password reset -----------------------------------------------------------------------

    async def forgot(self, email: str, client_addr: str | None) -> dict[str, Any]:
        await self._ip_limit("forgot", client_addr)
        user = await self.store.get_user_by_email(normalize_email(email))
        if user is not None and user.is_active and user.auth_source == "local" and user.email:
            token, hashed = new_token()
            await self.accounts.add_token(
                user.user_id, "reset_password", hashed,
                self.clock() + timedelta(minutes=self.settings.reset_minutes))
            await self.store.add_auth_audit("password_reset_requested", user.username,
                                            user_id=user.user_id, client_addr=client_addr)
            await self._mail(user.email, "reset", user.language, name=user.display_name,
                             link=self.link("reset", token), minutes=self.settings.reset_minutes)
        return {"status": "check_email"}

    async def reset(self, token: str, new_password: str, client_addr: str | None
                    ) -> dict[str, Any]:
        await self._ip_limit("reset", client_addr)
        hashed = token_hash(token)
        user_id = await self.accounts.use_token(hashed, "reset_password", self.clock())
        user = await self.store.get_user(user_id) if user_id is not None else None
        if user is None or not user.is_active:
            raise NotFound("link not found, used or expired", code="invalid_token")
        try:
            await self.auth.set_password(user, new_password, must_change=False)
        except Exception:
            await self.accounts.restore_token(hashed)   # a refused password: try again
            raise
        await self.store.revoke_user_sessions(user.user_id)
        if user.email_verified_at is None:        # the link proves the address too
            await self._activate(user, client_addr)
        await self.store.add_auth_audit("password_reset", user.username, user_id=user.user_id,
                                        client_addr=client_addr)
        return {"status": "password_changed"}

    # --- organizations: members and invitations -----------------------------------------------

    async def memberships(self, principal: Principal) -> list[MembershipRecord]:
        return await self.accounts.memberships(principal.user_id)

    async def _org(self, principal: Principal, org_id: str, permission: str) -> dict[str, Any]:
        if not principal.can_in_org(permission, org_id):
            raise NotFound("organization not found")
        org = await self.accounts.organization(org_id)
        if org is None:
            raise NotFound("organization not found")
        return org

    async def invite_member(self, principal: Principal, org_id: str, email: str, role: str,
                            client_addr: str | None) -> InvitationRecord:
        org = await self._org(principal, org_id, "org.members")
        allowed = INVITABLE_ROLES.get(org["kind"], ())
        if role not in allowed:
            raise Invalid(f"role {role} cannot be given in a {org['kind']} organization "
                          f"({', '.join(allowed) or 'none'})", code="invalid_role")
        email = normalize_email(email)
        await self._limit("invite", str(principal.user_id), 50, client_addr)
        token, hashed = new_token()
        expires = self.clock() + timedelta(days=self.settings.invitation_days)
        try:
            invitation = await self.accounts.create_invitation(
                principal.orgs_with("org.members"), kind="organization", org_id=org_id,
                email=email, token_hash=hashed, expires_at=expires,
                invited_by=principal.user_id, role_id=role)
        except PlatformError as exc:
            raise NotFound(str(exc)) from None
        await self.store.add_auth_audit(
            "invitation_created", principal.username, user_id=principal.user_id,
            client_addr=client_addr, detail={"org_id": org_id, "role": role,
                                             "invitation_id": invitation.invitation_id})
        await self._mail(email, "invite_organization", principal.user.language,
                         inviter=principal.user.display_name, org=org["name"],
                         role=label("roles", role, principal.user.language),
                         link=self.link("invitation", token), days=self.settings.invitation_days)
        return invitation

    async def invitations(self, principal: Principal, org_id: str) -> list[InvitationRecord]:
        await self._org(principal, org_id, "org.members")
        return await self.accounts.invitations(principal.orgs_with("org.members"), org_id)

    async def revoke_invitation(self, principal: Principal, org_id: str, invitation_id: int,
                                client_addr: str | None) -> None:
        orgs = principal.orgs_with("org.members") if principal.can_in_org(
            "org.members", org_id) else principal.orgs_with("device.share")
        if orgs is not None and org_id not in orgs:
            raise NotFound("invitation not found")
        if not await self.accounts.revoke_invitation(orgs, org_id, invitation_id):
            raise NotFound("invitation not found")
        await self.store.add_auth_audit("invitation_revoked", principal.username,
                                        user_id=principal.user_id, client_addr=client_addr,
                                        detail={"org_id": org_id,
                                                "invitation_id": invitation_id})

    async def members(self, principal: Principal, org_id: str) -> list[Any]:
        await self._org(principal, org_id, "org.members")
        return await self.accounts.members(org_id)

    async def remove_member(self, principal: Principal, org_id: str, user_role_id: int,
                            client_addr: str | None) -> None:
        await self._org(principal, org_id, "org.members")
        await self._remove(principal, org_id, user_role_id, client_addr)

    async def leave(self, principal: Principal, org_id: str, client_addr: str | None) -> None:
        mine = [m for m in await self.accounts.memberships(principal.user_id)
                if m.org_id == org_id]
        if not mine:
            raise NotFound("organization not found")
        for m in mine:
            await self._remove(principal, org_id, m.user_role_id, client_addr)

    async def _remove(self, principal: Principal, org_id: str, user_role_id: int,
                      client_addr: str | None) -> None:
        try:
            member = await self.accounts.remove_member(org_id, user_role_id)
        except PlatformError as exc:
            if exc.code == "not_found":
                raise NotFound(str(exc)) from None
            raise Conflict(str(exc), code=exc.code) from None
        await self.store.revoke_user_sessions(member.user_id)   # rights change now
        await self.store.add_auth_audit(
            "member_removed", principal.username, user_id=member.user_id,
            actor_id=principal.user_id, client_addr=client_addr,
            detail={"org_id": org_id, "role": member.role_id})

    # --- devices: shares ----------------------------------------------------------------------

    async def _owned(self, principal: Principal, device_id: str
                     ) -> tuple[set[str] | None, str]:
        """(the caller's share organizations, the owner org) of a device the caller owns."""
        orgs = principal.orgs_with("device.share")     # None: platform administrators
        owner = await self.accounts.owner_org(orgs, device_id) if orgs != set() else None
        if owner is None:
            raise NotFound("device not found")
        return orgs, owner

    async def share_device(self, principal: Principal, device_id: str, email: str, level: str,
                           client_addr: str | None) -> InvitationRecord:
        if level not in SHARE_LEVELS:
            raise Invalid(f"level must be one of {', '.join(SHARE_LEVELS)}",
                          code="invalid_level")
        orgs, owner = await self._owned(principal, device_id)
        email = normalize_email(email)
        await self._limit("invite", str(principal.user_id), 50, client_addr)
        device = (await self.store.platform.devices_by_id(orgs, [device_id]))[0]
        token, hashed = new_token()
        expires = self.clock() + timedelta(days=self.settings.invitation_days)
        try:
            invitation = await self.accounts.create_invitation(
                orgs, kind="device", org_id=owner, email=email, token_hash=hashed,
                expires_at=expires, invited_by=principal.user_id, device_id=device_id,
                level=level)
        except PlatformError as exc:
            raise NotFound(str(exc)) from None
        await self.store.add_auth_audit(
            "invitation_created", principal.username, user_id=principal.user_id,
            client_addr=client_addr, detail={"device_id": device_id, "level": level,
                                             "invitation_id": invitation.invitation_id})
        await self._mail(email, "invite_device", principal.user.language,
                         inviter=principal.user.display_name, device=device.name,
                         level=label("levels", level, principal.user.language),
                         link=self.link("invitation", token), days=self.settings.invitation_days)
        return invitation

    async def shares(self, principal: Principal, device_id: str) -> dict[str, Any]:
        orgs, owner = await self._owned(principal, device_id)
        return {"shares": await self.accounts.shares(orgs, device_id),
                "invitations": await self.accounts.invitations(orgs, owner,
                                                               device_id=device_id)}

    async def revoke_share(self, principal: Principal, device_id: str, access_id: int,
                           client_addr: str | None) -> None:
        """The owner revokes a share, or a member of the receiving org gives it up."""
        orgs = principal.orgs_with("device.share")
        received = principal.orgs_with("org.members")
        caller = None if orgs is None or received is None else orgs | received
        share = None
        if caller != set():
            share = await self.accounts.revoke_share(caller, device_id, access_id)
        if share is None:
            raise NotFound("share not found")
        await self.store.add_auth_audit("device_share_revoked", principal.username,
                                        user_id=principal.user_id, client_addr=client_addr,
                                        detail={"device_id": device_id, "org_id": share.org_id})

    # --- accepting invitations ----------------------------------------------------------------

    async def my_invitations(self, principal: Principal) -> list[InvitationRecord]:
        user = principal.user
        if not user.email or user.email_verified_at is None:
            return []
        return await self.accounts.pending_for(user.email, self.clock())

    async def accept(self, principal: Principal, token: str, target_org: str | None,
                     client_addr: str | None) -> dict[str, Any]:
        await self._ip_limit("accept", client_addr)
        invitation = await self.accounts.invitation_by_token(token_hash(token), self.clock())
        if invitation is None:
            raise NotFound("invitation not found, used or expired", code="invalid_invitation")
        user = principal.user
        if user.email_verified_at is None or not same_email(user.email, invitation.email):
            raise Forbidden("the invitation is for another e-mail address",
                            code="invitation_other_email")
        if invitation.kind == "device" and target_org is not None and \
                not principal.can_in_org("org.members", target_org):
            raise NotFound("organization not found")
        return await self._accept(user, invitation, target_org, client_addr)

    async def _accept(self, user: UserRecord, invitation: InvitationRecord,
                      target_org: str | None, client_addr: str | None) -> dict[str, Any]:
        if invitation.kind == "organization":
            target = invitation.org_id
        else:
            target = target_org or await self.accounts.ensure_personal_org(
                user, user.display_name)
        try:
            ok = await self.accounts.accept_invitation(invitation, user.user_id, target,
                                                       self.clock())
        except PlatformError as exc:
            raise Conflict(str(exc), code=exc.code) from None
        if not ok:
            raise NotFound("invitation not found, used or expired", code="invalid_invitation")
        event = "invitation_accepted" if invitation.kind == "organization" else "device_shared"
        await self.store.add_auth_audit(
            event, user.username, user_id=user.user_id, actor_id=invitation.invited_by,
            client_addr=client_addr,
            detail={"invitation_id": invitation.invitation_id, "org_id": target,
                    "device_id": invitation.device_id, "role": invitation.role_id,
                    "level": invitation.level})
        return {"status": "accepted", "kind": invitation.kind, "organization": target,
                "device_id": invitation.device_id}
