"""/api/health, /api/system, /api/audit."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from backend.deps import ServicesDep, require
from backend.rbac import Principal
from hub.drivers.base import utcnow
from hub.storage.records import AuthAuditRecord, WriteAuditRecord

router = APIRouter(tags=["system"])
Auditor = Annotated[Principal, Depends(require("audit.read"))]
Engineer = Annotated[Principal, Depends(require("config.edit"))]

VERSION = "0.6.0"


@router.get("/api/health")
async def health(svc: ServicesDep) -> JSONResponse:
    """No authentication: for load balancers and monitoring. Reveals no tag data.
    HTTP 503 on an HA standby node (or without database), so traffic goes to the active node."""
    try:
        db_ok = await svc.store.ping()
    except Exception:  # noqa: BLE001
        db_ok = False
    states = [str(s.state) for s in svc.hub.statuses.values()] if svc.role == "active" else []
    body = {"status": "ok" if db_ok and svc.role == "active" else
            ("standby" if svc.role != "active" else "degraded"),
            "role": svc.role, "node": svc.settings.node_id, "version": VERSION,
            "database": db_ok,
            "devices": {"total": len(states), "online": states.count("online"),
                        "offline": states.count("offline"), "error": states.count("error")}}
    return JSONResponse(body, status_code=200 if svc.role == "active" and db_ok else 503)


@router.get("/api/versions")
async def versions() -> dict[str, Any]:
    """API versions this hub serves (no authentication; for integrations and upgrades)."""
    from backend.app import API_VERSION, SUPPORTED_VERSIONS

    return {"current": API_VERSION, "supported": list(SUPPORTED_VERSIONS), "hub": VERSION,
            "base": f"/api/v{API_VERSION}"}


@router.get("/api/system/info")
async def info(svc: ServicesDep, _: Annotated[Principal, Depends(require("self"))]
               ) -> dict[str, Any]:
    """What the dashboard needs to know about this hub (any signed-in user)."""
    return {"version": VERSION, "node": svc.settings.node_id, "role": svc.role,
            "timezone": svc.settings.timezone, "control_enabled": svc.settings.control_enabled,
            "session_idle_minutes": svc.settings.session_idle_minutes,
            "password_min_length": svc.settings.password_min_length}


@router.get("/api/system/health-checks")
async def health_checks(svc: ServicesDep, _: Engineer, run: bool = False) -> dict[str, Any]:
    """Results of the hub's own health checks (``run=true`` checks again now)."""
    if svc.health_watch is None:
        return {"last_run": None, "checks": [], "open_alarms": []}
    if run:
        await svc.health_watch.run_once()
    return svc.health_watch.describe()


@router.get("/api/system/outputs")
async def outputs(svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    """Data the hub passes on: northbound MQTT targets, Modbus server, OPC UA server."""
    opcua = svc.opcua_server
    return {
        "northbound": svc.northbound.describe() if svc.northbound is not None else [],
        "modbus_server": svc.modbus_server.describe() if svc.modbus_server is not None else None,
        "opcua_server": None if opcua is None else {
            "enabled": opcua.server is not None, "endpoint": getattr(opcua, "endpoint", None),
            "writes": dict(opcua.writes)},
    }


@router.get("/api/system/nodes")
async def nodes(svc: ServicesDep, _: Engineer) -> list[dict[str, Any]]:
    """HA nodes with role and heartbeat."""
    return await svc.store.nodes()


@router.get("/api/system/stats")
async def stats(svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    s = svc.hub.stats
    return {"version": VERSION, "started_at": svc.started_at.isoformat(),
            "uptime_s": int((utcnow() - svc.started_at).total_seconds()),
            "control_enabled": svc.settings.control_enabled,
            "samples_received": s.samples_received, "samples_written": s.samples_written,
            "samples_dropped": s.samples_dropped, "buffered": s.buffered,
            "spool_batches": s.spool_batches, "spool_dropped_batches": s.spool_dropped_batches,
            "write_errors": s.write_errors, "last_write_error": s.last_write_error,
            "live_subscribers": svc.hub.bus.subscriber_count,
            "devices": {d: str(st.state) for d, st in sorted(svc.hub.statuses.items())}}


@router.get("/api/system/config")
async def config_report(svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    """The configuration the hub runs with (validated at start; secrets are env var names)."""
    return {"devices": [d.model_dump(mode="json") for d in svc.hub.devices.values()],
            "brokers": [b.model_dump(mode="json") for b in svc.report.brokers.values()],
            "alarms": svc.report.alarms.model_dump(mode="json"),
            "control": svc.report.control.model_dump(mode="json"),
            # channel names and types only: addresses and recipients stay in the file
            "notifications": {
                "channels": {name: c.type for name, c in
                             svc.report.notifications.channels.items()},
                "rules": [r.model_dump(mode="json") for r in svc.report.notifications.rules]},
            "issues": [i.model_dump(mode="json") for i in svc.report.issues]}


@router.get("/api/audit/writes", response_model=list[WriteAuditRecord])
async def write_audit(svc: ServicesDep, _: Auditor, tag: str | None = None,
                      user_id: int | None = None, request_id: UUID | None = None,
                      since: datetime | None = None,
                      limit: Annotated[int, Query(ge=1, le=5000)] = 200) -> list[WriteAuditRecord]:
    return await svc.store.list_write_audit(tag_path=tag, user_id=user_id, request_id=request_id,
                                            since=since, limit=limit)


@router.get("/api/audit/verify")
async def audit_verify(svc: ServicesDep, _: Auditor) -> dict[str, Any]:
    """Check the hash chain of both audit tables now."""
    return {t: await svc.store.audit_verify(t) for t in ("write_audit", "auth_audit")}


@router.get("/api/system/certificates")
async def certificates(svc: ServicesDep, _: Engineer) -> list[dict[str, Any]]:
    """Certificates the hub uses, with their expiry."""
    if svc.security_watch is None:
        return []
    return await svc.security_watch.check_certificates(utcnow())


@router.get("/api/audit/auth", response_model=list[AuthAuditRecord])
async def auth_audit(svc: ServicesDep, _: Auditor, event: str | None = None,
                     limit: Annotated[int, Query(ge=1, le=5000)] = 200) -> list[AuthAuditRecord]:
    return await svc.store.list_auth_audit(event=event, limit=limit)


@router.get("/api/system/events")
async def system_events(svc: ServicesDep, _: Engineer, kind: str | None = None,
                        limit: Annotated[int, Query(ge=1, le=1000)] = 100
                        ) -> list[dict[str, Any]]:
    """Backups, retention runs, failovers."""
    return await svc.store.events(kind, limit)


@router.post("/api/system/backup")
async def backup_now(svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    from backend.errors import Unavailable

    if svc.maintenance is None or not svc.settings.backup_dir:
        raise Unavailable("backups are not configured (HUB_BACKUP_DIR)", code="no_backup_dir")
    path = await svc.maintenance.run_backup()
    if path is None:
        raise Unavailable("backup failed; see /api/system/events", code="backup_failed")
    return {"file": str(path), "bytes": path.stat().st_size}


@router.get("/api/notifications/log")
async def notification_log(svc: ServicesDep, _: Auditor,
                           limit: Annotated[int, Query(ge=1, le=5000)] = 200
                           ) -> list[dict[str, Any]]:
    return await svc.store.notification_log(limit)


@router.post("/api/notifications/test/{channel}")
async def notification_test(channel: str, svc: ServicesDep, _: Engineer) -> dict[str, Any]:
    from backend.errors import NotFound, Unavailable

    if svc.notifier is None or channel not in svc.report.notifications.channels:
        raise NotFound("no such notification channel")
    try:
        await svc.notifier.send_test(channel)
    except Exception as exc:  # noqa: BLE001
        raise Unavailable(f"sending failed: {exc}", code="notification_failed") from exc
    return {"channel": channel, "status": "sent"}
