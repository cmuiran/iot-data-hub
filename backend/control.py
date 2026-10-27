"""Control-safety layer: the only code that may write to a device (CLAUDE.md section 5.1).

A write request passes these gates, in order; the first failure rejects it:

1. the tag exists and is active
2. RBAC for the tag's site/area: ``tag.write``, or ``control.setpoint`` / ``control.command``
   matching the tag's write_class (unset = command)
3. control is enabled: ``HUB_CONTROL_ENABLED``, the device is enabled, the tag is read_write
4. value: right type, inside the tag's min/max, representable in its raw type
5. ``max_step``: change versus the current value (needs a current good value)
6. ``min_interval_s`` since the last write to the tag
7. interlocks: each referenced tag must have a fresh, good value satisfying the condition
8. ``confirm``: the write waits for a second user with ``write.confirm`` (not the requester),
   who triggers gates 3-7 again before anything is sent

Then: read the previous value, write with a :class:`WriteGrant`, read back and compare.
Every request leaves a ``requested`` audit row before anything is sent and exactly one outcome
row (applied / readback_mismatch / failed / timeout / rejected_*), so the audit log tells
"refused", "sent and confirmed" and "sent, state unknown" apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import operator
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from backend.errors import Conflict, Forbidden, NotFound
from backend.rbac import Principal, split_path, write_permission
from backend.settings import Settings
from hub.codec import CodecError, to_raw, values_equal
from hub.drivers.base import DriverError, WriteGrant, utcnow
from hub.models import Access, ControlFile, ControlPolicy, DataType, Interlock, Quality, TagBase
from hub.runtime import Hub
from hub.storage.postgres import PostgresStore

log = logging.getLogger(__name__)

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le,
    "gt": operator.gt, "ge": operator.ge,
}

Status = Literal["applied", "pending_confirmation", "rejected", "failed", "unverified"]


class WriteResult(BaseModel):
    request_id: UUID
    tag: str
    status: Status
    result: str                     # the audit result code
    message: str
    requested_value: Any
    previous_value: Any = None
    readback_value: Any = None
    expires_at: datetime | None = None


class _Rejected(Exception):
    def __init__(self, result: str, message: str) -> None:
        super().__init__(message)
        self.result = result
        self.message = message


class ControlService:
    def __init__(self, hub: Hub, store: PostgresStore, control: ControlFile, settings: Settings,
                 clock: Callable[[], datetime] = utcnow, io_timeout_s: float = 5.0) -> None:
        self.hub = hub
        self.store = store
        self.control = control
        self.settings = settings
        self.clock = clock
        self.io_timeout_s = io_timeout_s
        self._locks: dict[str, asyncio.Lock] = {}
        self.results: dict[str, int] = {}   # audit result -> count (metrics)
        self._last_write: dict[str, datetime] = {}
        self._expiry_task: asyncio.Task[None] | None = None

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        await self.expire_pending()  # leftovers from before a restart
        self._expiry_task = asyncio.create_task(self._expiry_loop(), name="control:expiry")

    async def stop(self) -> None:
        if self._expiry_task:
            self._expiry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._expiry_task

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.expire_pending()
            except Exception:  # noqa: BLE001
                log.exception("expiring pending writes failed")

    async def expire_pending(self) -> None:
        for pending in await self.store.expire_pending(self.clock()):
            user = await self.store.get_user(pending.requested_by)
            await self._audit_outcome(pending.request_id, pending.requested_by,
                                      user.username if user else "?", None, pending.tag_path,
                                      pending.value, "rejected_unconfirmed",
                                      reason="not confirmed in time")

    # --- requests -----------------------------------------------------------------------------

    async def request_write(self, principal: Principal, tag_path: str, value: Any,
                            reason: str | None, client_addr: str | None) -> WriteResult:
        tag = self.tag_for(tag_path)
        request_id = uuid.uuid4()
        policy = self.control.policy_for(tag_path)
        await self.store.add_write_audit(
            request_id=request_id, phase="requested", user_id=principal.user_id,
            username=principal.username, client_addr=client_addr, tag_path=tag_path,
            requested_value=value, result="requested", reason=reason)
        try:
            site, area, _, _ = split_path(tag_path)
            if not principal.can_write(tag.write_class, site, area):
                raise _Rejected("rejected_rbac", f"no {write_permission(tag.write_class)} "
                                "permission for this area")
            raw = self._check(tag_path, tag, value, policy)
        except _Rejected as rej:
            return await self._reject(request_id, principal, client_addr, tag_path, value, rej)

        if policy.confirm:
            expires = self.clock() + timedelta(minutes=self.settings.pending_write_minutes)
            await self.store.add_pending(request_id=request_id, tag_path=tag_path,
                                         requested_by=principal.user_id, value=value,
                                         reason=reason, expires_at=expires)
            self.hub.bus.publish({"type": "write", "action": "pending", "tag": tag_path,
                                  "request_id": str(request_id), "by": principal.username})
            return WriteResult(request_id=request_id, tag=tag_path, status="pending_confirmation",
                               result="requested", requested_value=value, expires_at=expires,
                               message="waiting for confirmation by a second user")
        return await self._execute(request_id, principal.user_id, principal.username,
                                   client_addr, tag_path, tag, value, raw, policy, None)

    async def confirm(self, principal: Principal, request_id: UUID,
                      client_addr: str | None) -> WriteResult:
        pending = await self.store.get_pending(request_id)
        if pending is None:
            raise NotFound("no such pending write")
        if pending.status != "pending":
            raise Conflict(f"write is already {pending.status}", code="not_pending")
        if pending.expires_at <= self.clock():
            await self.expire_pending()
            raise Conflict("confirmation window has passed", code="expired")
        if pending.requested_by == principal.user_id:
            raise Forbidden("you cannot confirm your own write", code="self_confirmation")
        site, area, _, _ = split_path(pending.tag_path)
        if not principal.can("write.confirm", site, area):
            raise Forbidden("no write.confirm permission for this area")
        decided = await self.store.decide_pending(request_id, "confirmed", principal.user_id)
        if decided is None:
            raise Conflict("write was decided by someone else", code="not_pending")
        await self.store.add_auth_audit("write_confirmed", principal.username,
                                        user_id=principal.user_id, client_addr=client_addr,
                                        detail={"request_id": str(request_id),
                                                "tag": pending.tag_path})
        requester = await self.store.get_user(pending.requested_by)
        requester_name = requester.username if requester else "?"
        tag = self.tag_for(pending.tag_path)
        policy = self.control.policy_for(pending.tag_path)
        try:  # the plant may have changed while the request waited
            raw = self._check(pending.tag_path, tag, pending.value, policy)
        except _Rejected as rej:
            await self._audit_outcome(request_id, pending.requested_by, requester_name,
                                      client_addr, pending.tag_path, pending.value, rej.result,
                                      reason=rej.message, confirmed_by=principal.user_id)
            return WriteResult(request_id=request_id, tag=pending.tag_path, status="rejected",
                               result=rej.result, message=rej.message,
                               requested_value=pending.value)
        return await self._execute(request_id, pending.requested_by, requester_name, client_addr,
                                   pending.tag_path, tag, pending.value, raw, policy,
                                   principal.user_id)

    async def reject(self, principal: Principal, request_id: UUID, reason: str | None,
                     client_addr: str | None) -> WriteResult:
        pending = await self.store.get_pending(request_id)
        if pending is None:
            raise NotFound("no such pending write")
        site, area, _, _ = split_path(pending.tag_path)
        own = pending.requested_by == principal.user_id
        if not own and not principal.can("write.confirm", site, area):
            raise Forbidden("no write.confirm permission for this area")
        decided = await self.store.decide_pending(request_id, "rejected", principal.user_id
                                                  if not own else None)
        if decided is None:
            raise Conflict(f"write is already {pending.status}", code="not_pending")
        requester = await self.store.get_user(pending.requested_by)
        text = ("withdrawn by requester" if own else f"rejected by {principal.username}") + (
            f": {reason}" if reason else "")
        await self._audit_outcome(request_id, pending.requested_by,
                                  requester.username if requester else "?", client_addr,
                                  pending.tag_path, pending.value, "rejected_unconfirmed",
                                  reason=text)
        await self.store.add_auth_audit("write_rejected", principal.username,
                                        user_id=principal.user_id, client_addr=client_addr,
                                        detail={"request_id": str(request_id)})
        return WriteResult(request_id=request_id, tag=pending.tag_path, status="rejected",
                           result="rejected_unconfirmed", message=text,
                           requested_value=pending.value)

    # --- gates --------------------------------------------------------------------------------

    def tag_for(self, tag_path: str) -> TagBase:
        found = self.hub.tags.get(tag_path)
        if found is None:
            raise NotFound(f"unknown tag {tag_path}")
        return found[1]

    def _check(self, tag_path: str, tag: TagBase, value: Any, policy: ControlPolicy) -> Any:
        device, _ = self.hub.tags[tag_path]
        if not self.settings.control_enabled:
            raise _Rejected("rejected_disabled", "control is disabled on this hub "
                                                 "(HUB_CONTROL_ENABLED)")
        if not device.enabled:
            raise _Rejected("rejected_disabled", f"device {device.device_id} is disabled")
        if tag.access is not Access.READ_WRITE:
            raise _Rejected("rejected_disabled", "tag is read-only")
        if tag.data_type is DataType.BOOL and not isinstance(value, bool):
            raise _Rejected("rejected_range", "value must be true or false")
        if tag.data_type is not DataType.BOOL:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise _Rejected("rejected_range", "value must be a number")
            if (tag.min is not None and value < tag.min) or (tag.max is not None
                                                             and value > tag.max):
                raise _Rejected("rejected_range", f"value must be within [{tag.min}, {tag.max}]")
        try:
            raw = to_raw(tag, value)
        except CodecError as exc:
            raise _Rejected("rejected_range", str(exc)) from exc
        if policy.max_step is not None:
            current = self.hub.latest.get(tag_path)
            if current is None or current.quality is Quality.BAD or current.value is None:
                raise _Rejected("rejected_range", "max_step needs a current value of the tag")
            if abs(value - current.value) > policy.max_step:  # type: ignore[operator]
                raise _Rejected("rejected_range", f"change larger than max_step "
                                                  f"{policy.max_step:g}")
        last = self._last_write.get(tag_path)
        if last is not None and self.clock() - last < timedelta(seconds=policy.min_interval_s):
            raise _Rejected("rejected_rate", f"wait {policy.min_interval_s:g}s between writes")
        for lock in policy.interlocks:
            self._check_interlock(lock)
        return raw

    def _check_interlock(self, lock: Interlock) -> None:
        sample = self.hub.latest.get(lock.tag)
        if sample is None or sample.value is None or sample.quality is not Quality.GOOD:
            raise _Rejected("rejected_interlock", f"{lock.message} (no good value for "
                                                  f"{lock.tag})")
        if self.clock() - sample.timestamp > timedelta(seconds=lock.max_age_s):
            raise _Rejected("rejected_interlock", f"{lock.message} ({lock.tag} is stale)")
        try:
            ok = _OPS[lock.op](sample.value, lock.value)
        except TypeError:
            ok = False
        if not ok:
            raise _Rejected("rejected_interlock", lock.message)

    # --- execution ----------------------------------------------------------------------------

    async def _execute(self, request_id: UUID, user_id: int, username: str,
                       client_addr: str | None, tag_path: str, tag: TagBase, value: Any,
                       raw: Any, policy: ControlPolicy, confirmed_by: int | None) -> WriteResult:
        lock = self._locks.setdefault(tag_path, asyncio.Lock())
        async with lock:
            try:
                driver, _ = self.hub.driver_for_tag(tag_path)
            except DriverError as exc:
                return await self._finish(request_id, user_id, username, client_addr, tag_path,
                                          value, "failed", str(exc), confirmed_by=confirmed_by)
            previous = None
            with contextlib.suppress(Exception):
                previous = await asyncio.wait_for(driver.read_tag(tag.name), self.io_timeout_s)
            readback_task = None
            if hasattr(driver, "read_back") and driver.device.protocol == "mqtt":
                # push protocol: start waiting for the next message before sending
                readback_task = asyncio.create_task(driver.read_back(tag.name,
                                                                     policy.readback_timeout_s))
                await asyncio.sleep(0)
            self._last_write[tag_path] = self.clock()
            grant = WriteGrant(request_id=str(request_id), tag_path=tag_path, raw_value=raw)
            try:
                await asyncio.wait_for(driver.write_tag(tag.name, grant), self.io_timeout_s)
            except TimeoutError:
                if readback_task:
                    readback_task.cancel()
                return await self._finish(request_id, user_id, username, client_addr, tag_path,
                                          value, "timeout", "no answer from the device; state "
                                          "unknown", previous=previous, confirmed_by=confirmed_by)
            except Exception as exc:  # noqa: BLE001
                if readback_task:
                    readback_task.cancel()
                return await self._finish(request_id, user_id, username, client_addr, tag_path,
                                          value, "failed", f"{type(exc).__name__}: {exc}",
                                          previous=previous, confirmed_by=confirmed_by)
            try:
                if readback_task is not None:
                    readback = await readback_task
                else:
                    readback = await driver.read_back(tag.name, policy.readback_timeout_s)
            except Exception as exc:  # noqa: BLE001
                return await self._finish(request_id, user_id, username, client_addr, tag_path,
                                          value, "readback_mismatch",
                                          f"written, but read-back failed: {exc}",
                                          previous=previous, confirmed_by=confirmed_by)
            if values_equal(tag, readback, value):
                return await self._finish(request_id, user_id, username, client_addr, tag_path,
                                          value, "applied", "applied and confirmed by read-back",
                                          previous=previous, readback=readback,
                                          confirmed_by=confirmed_by)
            return await self._finish(request_id, user_id, username, client_addr, tag_path,
                                      value, "readback_mismatch",
                                      f"device reports {readback!r} after the write",
                                      previous=previous, readback=readback,
                                      confirmed_by=confirmed_by)

    async def _finish(self, request_id: UUID, user_id: int, username: str,
                      client_addr: str | None, tag_path: str, value: Any, result: str,
                      message: str, *, previous: Any = None, readback: Any = None,
                      confirmed_by: int | None = None) -> WriteResult:
        await self._audit_outcome(request_id, user_id, username, client_addr, tag_path, value,
                                  result, reason=message, previous=previous, readback=readback,
                                  confirmed_by=confirmed_by)
        status: Status = {"applied": "applied", "readback_mismatch": "unverified",
                          "timeout": "unverified"}.get(result, "failed")  # type: ignore[assignment]
        level = logging.INFO if result == "applied" else logging.WARNING
        log.log(level, "write %s %s=%r by %s: %s (%s)", request_id, tag_path, value, username,
                result, message)
        self.hub.bus.publish({"type": "write", "action": result, "tag": tag_path,
                              "request_id": str(request_id), "by": username})
        return WriteResult(request_id=request_id, tag=tag_path, status=status, result=result,
                           message=message, requested_value=value, previous_value=previous,
                           readback_value=readback)

    async def _reject(self, request_id: UUID, principal: Principal, client_addr: str | None,
                      tag_path: str, value: Any, rej: _Rejected) -> WriteResult:
        await self._audit_outcome(request_id, principal.user_id, principal.username, client_addr,
                                  tag_path, value, rej.result, reason=rej.message)
        log.info("write to %s by %s rejected: %s", tag_path, principal.username, rej.message)
        return WriteResult(request_id=request_id, tag=tag_path, status="rejected",
                           result=rej.result, message=rej.message, requested_value=value)

    async def _audit_outcome(self, request_id: UUID, user_id: int, username: str,
                             client_addr: str | None, tag_path: str, value: Any, result: str, *,
                             reason: str | None = None, previous: Any = None,
                             readback: Any = None, confirmed_by: int | None = None) -> None:
        self.results[result] = self.results.get(result, 0) + 1
        await self.store.add_write_audit(
            request_id=request_id, phase="outcome", user_id=user_id, username=username,
            client_addr=client_addr, tag_path=tag_path, requested_value=value, result=result,
            previous_value=previous, readback_value=readback, confirmed_by=confirmed_by,
            reason=reason)
