"""Backend settings, read from environment variables (and .env via the server entry point)."""

from __future__ import annotations

import os
import secrets
import socket
from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from hub.maintenance import MaintenanceSettings


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    database_url: str | None = None
    config_dir: Path = Path("config")
    jwt_secret: str = Field(min_length=32)
    access_token_minutes: int = Field(default=15, ge=1, le=240)
    refresh_token_hours: int = Field(default=12, ge=1, le=24 * 30)
    control_enabled: bool = False
    login_max_failures: int = Field(default=5, ge=1, le=100)
    lockout_minutes: int = Field(default=15, ge=1, le=24 * 60)
    password_min_length: int = Field(default=10, ge=8, le=128)
    pending_write_minutes: int = Field(default=5, ge=1, le=60)
    cors_origins: list[str] = Field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    db_timescale: str = "auto"
    timezone: str = "Asia/Tehran"
    # storage housekeeping (hub/maintenance.py)
    retention_raw_days: int = Field(default=365, ge=1, le=3650)
    retention_hourly_days: int = Field(default=3650, ge=1, le=36500)
    backup_dir: Path | None = None
    backup_time: time = time(2, 30)
    backup_keep: int = Field(default=14, ge=1, le=1000)
    pg_dump: str | None = None
    # login throttling per client address (in addition to the per-user lockout)
    login_ip_max_failures: int = Field(default=20, ge=1, le=10_000)
    login_ip_window_s: int = Field(default=300, ge=10, le=86_400)
    # OPC UA server output (backend/opcua_server.py)
    opcua_server: bool = False
    opcua_server_host: str = "127.0.0.1"
    opcua_server_port: int = Field(default=4840, ge=1, le=65535)
    opcua_server_writes: bool = False
    opcua_server_allow_insecure: bool = False
    pki_dir: Path = Path("pki")
    # monitoring / high availability / logging
    metrics_token: str | None = None
    ha: Literal["off", "on"] = "off"
    node_id: str = Field(default_factory=lambda: socket.gethostname().lower())
    log_file: Path | None = None
    log_max_mb: int = Field(default=20, ge=1, le=1000)
    log_backups: int = Field(default=10, ge=1, le=100)
    # security hardening (phase 1)
    data_key: str | None = None                 # encrypts TOTP secrets and backups
    tls_cert_file: Path | None = None           # HTTPS directly from hub-api
    tls_key_file: Path | None = None
    session_idle_minutes: int = Field(default=30, ge=1, le=24 * 60)
    mfa_required_roles: list[str] = Field(default_factory=list)
    password_max_age_days: int = Field(default=0, ge=0, le=3650)    # 0 = never expires
    password_history: int = Field(default=3, ge=0, le=24)
    rate_limit_per_min: int = Field(default=600, ge=10, le=100_000)  # per client address
    allowed_networks: list[str] = Field(default_factory=list)       # empty = any address
    allowed_origins: list[str] = Field(default_factory=list)        # browsers for /api/ws
    cert_warn_days: int = Field(default=30, ge=1, le=365)

    @field_validator("allowed_networks")
    @classmethod
    def _check_networks(cls, value: list[str]) -> list[str]:
        import ipaddress

        for net in value:
            try:
                ipaddress.ip_network(net, strict=False)
            except ValueError as exc:
                raise ValueError(f"HUB_ALLOWED_NETWORKS: {net!r} is not an address/network")                     from exc
        return value

    @field_validator("data_key")
    @classmethod
    def _check_data_key(cls, value: str | None) -> str | None:
        if value is not None and len(value) < 32:
            raise ValueError("HUB_DATA_KEY must be at least 32 characters")
        return value

    @property
    def tls(self) -> bool:
        return self.tls_cert_file is not None and self.tls_key_file is not None

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @field_validator("metrics_token")
    @classmethod
    def _check_metrics_token(cls, value: str | None) -> str | None:
        if value is not None and len(value) < 16:
            raise ValueError("HUB_METRICS_TOKEN must be at least 16 characters")
        return value

    def maintenance(self) -> MaintenanceSettings:
        return MaintenanceSettings(
            retention_raw_days=self.retention_raw_days,
            retention_hourly_days=self.retention_hourly_days, backup_dir=self.backup_dir,
            backup_time=self.backup_time, backup_keep=self.backup_keep, timezone=self.timezone,
            pg_dump=self.pg_dump, data_key=self.data_key)

    @field_validator("db_timescale")
    @classmethod
    def _check_timescale(cls, value: str) -> str:
        if value not in ("auto", "on", "off"):
            raise ValueError("HUB_DB_TIMESCALE must be auto, on or off")
        return value

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, dev: bool = False) -> Settings:
        e = dict(os.environ if env is None else env)
        secret = e.get("HUB_JWT_SECRET", "")
        if not secret and dev:
            secret = secrets.token_urlsafe(48)  # dev only: tokens die with the process
        return cls(
            database_url=e.get("HUB_DATABASE_URL") or None,
            config_dir=Path(e.get("HUB_CONFIG_DIR", "config")),
            jwt_secret=secret,
            access_token_minutes=int(e.get("HUB_ACCESS_TOKEN_MINUTES", 15)),
            refresh_token_hours=int(e.get("HUB_REFRESH_TOKEN_HOURS", 12)),
            control_enabled=_bool(e.get("HUB_CONTROL_ENABLED"), False),
            login_max_failures=int(e.get("HUB_LOGIN_MAX_FAILURES", 5)),
            lockout_minutes=int(e.get("HUB_LOCKOUT_MINUTES", 15)),
            password_min_length=int(e.get("HUB_PASSWORD_MIN_LENGTH", 10)),
            pending_write_minutes=int(e.get("HUB_PENDING_WRITE_MINUTES", 5)),
            cors_origins=_list(e.get("HUB_CORS_ORIGINS")),
            host=e.get("HUB_HOST", "127.0.0.1"),
            port=int(e.get("HUB_PORT", 8000)),
            db_timescale=e.get("HUB_DB_TIMESCALE", "auto"),
            timezone=e.get("HUB_TIMEZONE", "Asia/Tehran"),
            retention_raw_days=int(e.get("HUB_RETENTION_RAW_DAYS", 365)),
            retention_hourly_days=int(e.get("HUB_RETENTION_HOURLY_DAYS", 3650)),
            backup_dir=Path(e["HUB_BACKUP_DIR"]) if e.get("HUB_BACKUP_DIR") else None,
            backup_time=time.fromisoformat(e.get("HUB_BACKUP_TIME", "02:30")),
            backup_keep=int(e.get("HUB_BACKUP_KEEP", 14)),
            pg_dump=e.get("HUB_PG_DUMP") or None,
            login_ip_max_failures=int(e.get("HUB_LOGIN_IP_MAX_FAILURES", 20)),
            login_ip_window_s=int(e.get("HUB_LOGIN_IP_WINDOW_S", 300)),
            opcua_server=_bool(e.get("HUB_OPCUA_SERVER"), False),
            opcua_server_host=e.get("HUB_OPCUA_SERVER_HOST", "127.0.0.1"),
            opcua_server_port=int(e.get("HUB_OPCUA_SERVER_PORT", 4840)),
            opcua_server_writes=_bool(e.get("HUB_OPCUA_SERVER_WRITES"), False),
            opcua_server_allow_insecure=_bool(e.get("HUB_OPCUA_SERVER_ALLOW_INSECURE"), False),
            pki_dir=Path(e.get("HUB_PKI_DIR", "pki")),
            metrics_token=e.get("HUB_METRICS_TOKEN") or None,
            ha=e.get("HUB_HA", "off"),  # type: ignore[arg-type]
            node_id=e.get("HUB_NODE_ID") or socket.gethostname().lower(),
            log_file=Path(e["HUB_LOG_FILE"]) if e.get("HUB_LOG_FILE") else None,
            log_max_mb=int(e.get("HUB_LOG_MAX_MB", 20)),
            log_backups=int(e.get("HUB_LOG_BACKUPS", 10)),
            data_key=e.get("HUB_DATA_KEY") or (secret if dev else None),
            tls_cert_file=Path(e["HUB_TLS_CERT"]) if e.get("HUB_TLS_CERT") else None,
            tls_key_file=Path(e["HUB_TLS_KEY"]) if e.get("HUB_TLS_KEY") else None,
            session_idle_minutes=int(e.get("HUB_SESSION_IDLE_MINUTES", 30)),
            mfa_required_roles=_list(e.get("HUB_MFA_REQUIRED_ROLES")),
            password_max_age_days=int(e.get("HUB_PASSWORD_MAX_AGE_DAYS", 0)),
            password_history=int(e.get("HUB_PASSWORD_HISTORY", 3)),
            rate_limit_per_min=int(e.get("HUB_RATE_LIMIT_PER_MIN", 600)),
            allowed_networks=_list(e.get("HUB_ALLOWED_NETWORKS")),
            allowed_origins=_list(e.get("HUB_ALLOWED_ORIGINS")),
            cert_warn_days=int(e.get("HUB_CERT_WARN_DAYS", 30)),
        )


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env reader (KEY=value lines); real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
