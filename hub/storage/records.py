"""Records returned by the store (Pydantic, so they can cross the API boundary as-is)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from hub.models import Access, AlarmSeverity, DataType, Quality, QualityReason


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class TagRecord(_Record):
    tag_id: int
    tag_path: str
    device_id: str
    site: str
    area: str
    name: str
    data_type: DataType
    unit: str | None
    access: Access
    min_value: float | None
    max_value: float | None
    description: str | None = None


class LatestValue(_Record):
    tag_path: str
    value: bool | int | float | str | None
    ts: datetime                                  # source time (or hub time)
    quality: Quality
    reason: QualityReason | None = None           # why it is not good
    received_at: datetime | None = None           # when the hub received it


class HistoryPoint(_Record):
    ts: datetime
    value: bool | int | float | str | None = None   # raw rows
    avg: float | None = None                         # bucketed rows
    min: float | None = None
    max: float | None = None
    count: int = 1
    quality: Quality = Quality.GOOD


class DeviceStatusRecord(_Record):
    device_id: str
    state: str
    last_seen: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    updated_at: datetime


class UserRecord(_Record):
    user_id: int
    username: str
    display_name: str
    password_hash: str
    is_active: bool
    failed_logins: int
    locked_until: datetime | None
    must_change_password: bool
    language: str
    created_at: datetime
    last_login_at: datetime | None
    totp_enabled: bool = False
    password_changed_at: datetime | None = None
    auth_source: str = "local"          # local | ldap (Active Directory)
    external_dn: str | None = None
    email: str | None = None             # self-registered accounts (0010)
    email_verified_at: datetime | None = None


class RoleGrant(_Record):
    user_role_id: int
    user_id: int
    role_id: str
    site: str | None
    area: str | None
    granted_by: int | None
    org_id: str | None = None
    granted_at: datetime


class SessionRecord(_Record):
    session_id: UUID
    user_id: int
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_seen_at: datetime | None = None
    mfa: bool = False


class WriteAuditRecord(_Record):
    id: int
    ts: datetime
    request_id: UUID
    phase: str
    user_id: int
    username: str
    client_addr: str | None
    tag_path: str
    requested_value: str
    previous_value: str | None
    readback_value: str | None
    confirmed_by: int | None
    result: str
    reason: str | None


class AuthAuditRecord(_Record):
    id: int
    ts: datetime
    event: str
    username: str
    user_id: int | None
    actor_id: int | None
    client_addr: str | None
    detail: dict[str, Any]


class PendingWrite(_Record):
    request_id: UUID
    tag_id: int
    tag_path: str
    requested_by: int
    value: Any
    reason: str | None
    created_at: datetime
    expires_at: datetime
    status: str
    decided_by: int | None
    decided_at: datetime | None


class AlarmRecord(_Record):
    alarm_id: int
    source: str
    tag_path: str | None
    device_id: str | None
    kind: str
    severity: AlarmSeverity
    message: str
    value: str | None
    raised_at: datetime
    cleared_at: datetime | None
    acked_at: datetime | None
    acked_by: int | None
    ack_comment: str | None

    @property
    def is_active(self) -> bool:
        return self.cleared_at is None or self.acked_at is None


class MaintenanceRecord(_Record):
    record_id: int
    equipment_id: str
    performed_at: datetime
    kind: str
    description: str
    performed_by: str | None = None
    work_order: str | None = None
    created_by: int
    created_by_name: str | None = None
    created_at: datetime


class CollectionError(_Record):
    id: int
    ts: datetime
    device_id: str
    state: str
    message: str

