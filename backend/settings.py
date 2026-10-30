"""Backend settings, read from environment variables (and .env via the server entry point)."""

from __future__ import annotations

import os
import secrets
import socket
from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from hub.maintenance import MaintenanceSettings
from hub.paths import program_path

BUNDLED_FRONTEND = program_path("frontend")
BUNDLED_APP = program_path("app")
BUNDLED_MAKER = program_path("maker")


def _frontend_dir(value: str | None) -> Path | None:
    """HUB_FRONTEND_DIR: unset = the bundled dashboard, "off" = API only, else a directory."""
    if value is None or value == "":
        return BUNDLED_FRONTEND if (BUNDLED_FRONTEND / "index.html").is_file() else None
    return None if value.strip().lower() == "off" else Path(value)


def _app_dir(value: str | None) -> Path | None:
    """HUB_APP_DIR: unset = the bundled consumer app, "off" = not served, else a directory."""
    if value is None or value == "":
        return BUNDLED_APP if (BUNDLED_APP / "index.html").is_file() else None
    return None if value.strip().lower() == "off" else Path(value)


def _maker_dir(value: str | None) -> Path | None:
    """HUB_MAKER_DIR: unset = the bundled maker panel, "off" = not served, else a directory."""
    if value is None or value == "":
        return BUNDLED_MAKER if (BUNDLED_MAKER / "index.html").is_file() else None
    return None if value.strip().lower() == "off" else Path(value)


def _spool_dir(value: str | None) -> Path | None:
    """HUB_SPOOL_DIR: unset = ./spool, "off" = no store & forward, else a directory."""
    if value is None or value.strip() == "":
        return Path("spool")
    return None if value.strip().lower() == "off" else Path(value)


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
    # interactive API docs + OpenAPI schema without sign-in: on in --dev, off in production
    api_docs: bool = True
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
    # web dashboard (frontend/), served at / by hub-api
    frontend_dir: Path | None = Field(default_factory=lambda: _frontend_dir(None))
    # consumer app (app/, platform phase 5), served at /app; "off" = not served
    app_dir: Path | None = Field(default_factory=lambda: _app_dir(None))
    maker_dir: Path | None = Field(default_factory=lambda: _maker_dir(None))
    # operations (phase 4): structured logs, syslog, health checks, restore tests
    log_format: Literal["text", "json"] = "text"
    syslog: str | None = None  # udp://host:514 | tcp://host:601
    syslog_facility: str = "local0"
    health_interval_s: int = Field(default=30, ge=5, le=3600)
    disk_warn_percent: float = Field(default=10.0, gt=0, lt=100)
    disk_critical_percent: float = Field(default=5.0, gt=0, lt=100)
    clock_max_skew_s: float = Field(default=30.0, gt=0, le=3600)
    ntp_server: str | None = None
    backup_max_age_hours: int = Field(default=26, ge=1, le=24 * 30)
    restore_test: Literal["off", "archive", "full"] = "archive"
    restore_test_days: int = Field(default=7, ge=1, le=365)
    # store & forward (phase 7): data that cannot be delivered waits on disk, not in memory
    spool_dir: Path | None = None
    spool_max_mb: int = Field(default=1024, ge=10, le=1_000_000)
    # self-service accounts (platform phase 2): off by default; invitations work regardless
    signup_enabled: bool = False
    public_url: str | None = None               # base of the links in e-mails
    smtp_host: str | None = None                # unset: e-mails are not sent (logged only)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_user: str | None = None
    smtp_password: str | None = None
    mail_from: str = "no-reply@localhost"
    # SMS and voice calls (alerts to a home's contacts, scenes by an SMS keyword):
    # "none" = off, "log" = kept in memory (development), "kavenegar" = the Kavenegar API
    sms_provider: Literal["none", "log", "kavenegar"] = "none"
    sms_api_key: str | None = None              # the provider's API key (HUB_SMS_API_KEY)
    sms_sender: str | None = None               # the line the SMS comes from
    sms_inbound_token: str | None = None        # the provider calls /api/sms/inbound with it
    verify_hours: int = Field(default=48, ge=1, le=24 * 14)
    reset_minutes: int = Field(default=60, ge=5, le=24 * 60)
    invitation_days: int = Field(default=7, ge=1, le=90)
    account_ip_per_hour: int = Field(default=20, ge=1, le=10_000)     # per address and action
    account_email_per_hour: int = Field(default=3, ge=1, le=100)      # e-mails per address
    # platform device contract (phase 4): TCP listener, platform MQTT broker, broker hooks
    tcp_ingest_host: str = "0.0.0.0"  # nosec B104 - devices connect from the network
    tcp_ingest_port: int | None = Field(default=None, ge=0, le=65535)   # None: off
    tcp_ingest_insecure: bool = False           # plain TCP without TLS (development only)
    platform_broker: str | None = None          # broker_id in brokers.yaml for native devices
    platform_topic_prefix: str = ""
    platform_shared_group: str | None = None    # $share/<group>/ for several hub nodes
    broker_hook_token: str | None = None        # header X-Hub-Hook-Token of the broker hooks

    @field_validator("syslog")
    @classmethod
    def _check_syslog(cls, value: str | None) -> str | None:
        import re

        if value is not None and not re.fullmatch(r"(udp|tcp)://[^\s/:]+:\d{1,5}", value):
            raise ValueError("HUB_SYSLOG must be udp://host:port or tcp://host:port")
        return value

    @field_validator("syslog_facility")
    @classmethod
    def _check_facility(cls, value: str) -> str:
        import logging.handlers

        if value not in logging.handlers.SysLogHandler.facility_names:
            raise ValueError(f"HUB_SYSLOG_FACILITY {value!r} is not a syslog facility "
                             "(e.g. local0..local7, daemon, user)")
        return value

    @model_validator(mode="after")
    def _check_disk_levels(self) -> Settings:
        if self.disk_critical_percent >= self.disk_warn_percent:
            raise ValueError("HUB_DISK_CRITICAL_PERCENT must be below HUB_DISK_WARN_PERCENT")
        return self

    @field_validator("allowed_networks")
    @classmethod
    def _check_networks(cls, value: list[str]) -> list[str]:
        import ipaddress

        for net in value:
            try:
                ipaddress.ip_network(net, strict=False)
            except ValueError as exc:
                raise ValueError(f"HUB_ALLOWED_NETWORKS: {net!r} is not an address/network") \
                    from exc
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

    @field_validator("broker_hook_token")
    @classmethod
    def _check_hook_token(cls, value: str | None) -> str | None:
        if value is not None and len(value) < 24:
            raise ValueError("HUB_BROKER_HOOK_TOKEN must be at least 24 characters")
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
            pg_dump=self.pg_dump, data_key=self.data_key, restore_test=self.restore_test,
            restore_test_days=self.restore_test_days)

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
            api_docs=_bool(e.get("HUB_API_DOCS"), dev),
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
            frontend_dir=_frontend_dir(e.get("HUB_FRONTEND_DIR")),
            app_dir=_app_dir(e.get("HUB_APP_DIR")),
            maker_dir=_maker_dir(e.get("HUB_MAKER_DIR")),
            log_format=e.get("HUB_LOG_FORMAT", "text"),  # type: ignore[arg-type]
            syslog=e.get("HUB_SYSLOG") or None,
            syslog_facility=e.get("HUB_SYSLOG_FACILITY", "local0"),
            health_interval_s=int(e.get("HUB_HEALTH_INTERVAL_S", 30)),
            disk_warn_percent=float(e.get("HUB_DISK_WARN_PERCENT", 10)),
            disk_critical_percent=float(e.get("HUB_DISK_CRITICAL_PERCENT", 5)),
            clock_max_skew_s=float(e.get("HUB_CLOCK_MAX_SKEW_S", 30)),
            ntp_server=e.get("HUB_NTP_SERVER") or None,
            backup_max_age_hours=int(e.get("HUB_BACKUP_MAX_AGE_HOURS", 26)),
            restore_test=e.get("HUB_RESTORE_TEST", "archive"),  # type: ignore[arg-type]
            restore_test_days=int(e.get("HUB_RESTORE_TEST_DAYS", 7)),
            spool_dir=_spool_dir(e.get("HUB_SPOOL_DIR")),
            spool_max_mb=int(e.get("HUB_SPOOL_MAX_MB", 1024)),
            signup_enabled=_bool(e.get("HUB_SIGNUP_ENABLED"), False),
            public_url=(e.get("HUB_PUBLIC_URL") or "").rstrip("/") or None,
            smtp_host=e.get("HUB_SMTP_HOST") or None,
            smtp_port=int(e.get("HUB_SMTP_PORT", 587)),
            smtp_security=e.get("HUB_SMTP_SECURITY", "starttls"),  # type: ignore[arg-type]
            smtp_user=e.get("HUB_SMTP_USERNAME") or None,
            smtp_password=e.get("HUB_SMTP_PASSWORD") or None,
            mail_from=e.get("HUB_MAIL_FROM", "no-reply@localhost"),
            sms_provider=e.get("HUB_SMS_PROVIDER", "none"),  # type: ignore[arg-type]
            sms_api_key=e.get("HUB_SMS_API_KEY") or None,
            sms_sender=e.get("HUB_SMS_SENDER") or None,
            sms_inbound_token=e.get("HUB_SMS_INBOUND_TOKEN") or None,
            verify_hours=int(e.get("HUB_VERIFY_HOURS", 48)),
            reset_minutes=int(e.get("HUB_RESET_MINUTES", 60)),
            invitation_days=int(e.get("HUB_INVITATION_DAYS", 7)),
            account_ip_per_hour=int(e.get("HUB_ACCOUNT_IP_PER_HOUR", 20)),
            account_email_per_hour=int(e.get("HUB_ACCOUNT_EMAIL_PER_HOUR", 3)),
            tcp_ingest_host=e.get("HUB_TCP_INGEST_HOST", "0.0.0.0"),  # nosec B104
            tcp_ingest_port=int(e["HUB_TCP_INGEST_PORT"]) if e.get("HUB_TCP_INGEST_PORT")
            else None,
            tcp_ingest_insecure=_bool(e.get("HUB_TCP_INGEST_INSECURE"), False),
            platform_broker=e.get("HUB_PLATFORM_BROKER") or None,
            platform_topic_prefix=e.get("HUB_PLATFORM_TOPIC_PREFIX", ""),
            platform_shared_group=e.get("HUB_PLATFORM_SHARED_GROUP") or None,
            broker_hook_token=e.get("HUB_BROKER_HOOK_TOKEN") or None,
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
