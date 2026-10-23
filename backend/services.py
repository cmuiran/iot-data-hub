"""Everything the API needs at run time, built once by backend.server (or by tests)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.auth import AuthService
from backend.control import ControlService
from backend.notifications import Notifier
from backend.schedules import Scheduler
from backend.settings import Settings
from hub.config_loader import ConfigReport
from hub.drivers.base import utcnow
from hub.maintenance import Maintenance
from hub.runtime import Hub
from hub.storage.postgres import PostgresStore


@dataclass
class Services:
    settings: Settings
    report: ConfigReport
    store: PostgresStore
    hub: Hub
    auth: AuthService
    control: ControlService
    started_at: datetime = field(default_factory=utcnow)
    config_admin: Any = None
    maintenance: Maintenance | None = None
    notifier: Notifier | None = None
    scheduler: Scheduler | None = None
    opcua_server: Any = None
    role: str = "active"     # "standby" on the passive node of an HA pair
    security_watch: Any = None

    def __post_init__(self) -> None:
        from backend.config_admin import ConfigAdmin

        if self.config_admin is None:
            self.config_admin = ConfigAdmin(self)
        if self.notifier is None:
            self.notifier = Notifier(self.report.notifications, self.store,
                                     timezone=self.settings.timezone, scope_of=self.scope_of)
        self.hub.alarm_listeners.append(self.notifier.on_alarm)
        if self.scheduler is None:
            self.scheduler = Scheduler(self)
        if self.security_watch is None:
            from backend.security_watch import SecurityWatch

            self.security_watch = SecurityWatch(self)
        if self.opcua_server is None and self.settings.opcua_server:
            from backend.opcua_server import HubOpcUaServer

            self.opcua_server = HubOpcUaServer(self)
        if self.maintenance is None:
            self.maintenance = Maintenance(self.store, self.settings.maintenance(),
                                           database_url=self.settings.database_url,
                                           node_id=self.settings.node_id)

    @classmethod
    def build(cls, settings: Settings, report: ConfigReport, store: PostgresStore,
              hub: Hub | None = None) -> Services:
        hub = hub or Hub(report, store=store)
        return cls(settings=settings, report=report, store=store, hub=hub,
                   auth=AuthService(store, settings),
                   control=ControlService(hub, store, report.control, settings))

    async def start(self) -> None:
        """Become the working node: drivers, control, notifications, schedules, housekeeping."""
        self.role = "active"
        await self.hub.start()
        await self.control.start()
        if self.notifier is not None:
            await self.notifier.start()
        if self.scheduler is not None:
            await self.scheduler.start()
        if self.opcua_server is not None:
            await self.opcua_server.start()
        if self.security_watch is not None:
            await self.security_watch.start()
        if self.maintenance is not None:
            await self.maintenance.start()

    def scope_of(self, source: str) -> tuple[str, str] | None:
        """(site, area) of an alarm source: a tag path or ``device:<id>``."""
        if source.startswith("device:"):
            d = self.hub.devices.get(source.removeprefix("device:"))
            return (d.site, d.area) if d else None
        parts = source.split(".")
        return (parts[0], parts[1]) if len(parts) == 4 else None

    def extra_metrics(self) -> list[tuple[str, str, str, list[tuple[dict[str, str], float]]]]:
        """Metrics of optional components (notifications, HA, OPC UA server)."""
        out: list[tuple[str, str, str, list[tuple[dict[str, str], float]]]] = [
            ("hub_ha_active", "gauge", "1 on the active node, 0 on a standby",
             [({"node": self.settings.node_id}, 1 if self.role == "active" else 0)])]
        if self.notifier is not None:
            out.append(("hub_notifications_total", "counter", "Notification attempts",
                        [({"channel": c, "status": s}, n)
                         for (c, s), n in sorted(self.notifier.counters.items())]))
        if self.opcua_server is not None:
            out.append(("hub_opcua_server_writes_total", "counter",
                        "Writes received by the OPC UA server",
                        [({"outcome": k}, v) for k, v in self.opcua_server.writes.items()]))
            out.append(("hub_opcua_server_login_failures_total", "counter",
                        "Refused OPC UA logins", [({}, self.opcua_server.users.failures)]))
        return out

    async def reload(self, report: ConfigReport) -> dict[str, list[str]]:
        """Apply a validated configuration live (used by backend.config_admin)."""
        changes = await self.hub.reload(report)
        self.report = report
        self.control.control = report.control
        if self.notifier is not None:
            self.notifier.config = report.notifications
        if self.opcua_server is not None and self.opcua_server.server is not None:
            await self.opcua_server.restart()  # the address space follows the devices
        return changes

    async def stop(self) -> None:
        """Stop everything that talks to devices (also used to demote an HA node)."""
        if self.security_watch is not None:
            await self.security_watch.stop()
        if self.opcua_server is not None:
            await self.opcua_server.stop()
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.maintenance is not None:
            await self.maintenance.stop()
        if self.notifier is not None:
            await self.notifier.stop()
        await self.control.stop()
        await self.hub.stop()
