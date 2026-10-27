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
from hub.spool import DiskQueue
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
    northbound: Any = None      # backend.northbound.Northbound (config/northbound.yaml)
    health_watch: Any = None    # backend.health_watch.HealthWatch (system health alarms)
    modbus_server: Any = None   # backend.modbus_server.HubModbusServer
    accounts: Any = None        # backend.accounts.AccountService (sign-up, invitations, shares)
    claims: Any = None          # backend.claims.ClaimService (labels, claims, edge hubs)
    ingest: Any = None          # backend.ingest.Ingest (native devices: MQTT / HTTP / TCP)
    tcp_ingest: Any = None      # backend.tcp_ingest.TcpIngest (HUB_TCP_INGEST_PORT)
    automations: Any = None     # backend.automations.AutomationEngine (scenes, automations)
    versions: Any = None        # backend.versions.VersionService (upgrades, firmware)

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
        if self.health_watch is None:
            from backend.health_watch import HealthWatch

            self.health_watch = HealthWatch(self)
        if self.northbound is None:
            from backend.northbound import Northbound

            self.northbound = Northbound(self)
        if self.modbus_server is None:
            from backend.modbus_server import HubModbusServer

            self.modbus_server = HubModbusServer(self)
        if self.accounts is None:
            from backend.accounts import AccountService
            from backend.mailer import make_mailer
            self.accounts = AccountService(self.store, self.settings, self.auth,
                                           make_mailer(self.settings))
        if self.claims is None:
            from backend.claims import ClaimService
            self.claims = ClaimService(self)
        if self.ingest is None:
            from backend.ingest import Ingest
            self.ingest = Ingest(self)
        self.hub.native_channel = self.ingest
        if self.tcp_ingest is None:
            from backend.tcp_ingest import TcpIngest
            self.tcp_ingest = TcpIngest.from_settings(self.ingest, self.settings)
        if self.ingest.mqtt is None and self.settings.platform_broker:
            from backend.mqtt_ingest import MqttIngest
            broker = self.report.brokers.get(self.settings.platform_broker)
            if broker is None:
                raise RuntimeError(f"HUB_PLATFORM_BROKER={self.settings.platform_broker}: no "
                                   "such broker in brokers.yaml")
            self.ingest.mqtt = MqttIngest(self.ingest, broker,
                                          prefix=self.settings.platform_topic_prefix,
                                          shared_group=self.settings.platform_shared_group,
                                          node_id=self.settings.node_id)
        if self.automations is None:
            from backend.automations import AutomationEngine
            self.automations = AutomationEngine(self)
        if self.versions is None:
            from backend.versions import VersionService
            self.versions = VersionService(self)
        if self.maintenance is None:
            self.maintenance = Maintenance(self.store, self.settings.maintenance(),
                                           database_url=self.settings.database_url,
                                           node_id=self.settings.node_id)

    @classmethod
    def build(cls, settings: Settings, report: ConfigReport, store: PostgresStore,
              hub: Hub | None = None) -> Services:
        hub = hub or Hub(report, store=store)
        if hub.spool is None and settings.spool_dir is not None:
            hub.spool = DiskQueue(settings.spool_dir / "samples.db",
                                  settings.spool_max_mb * 2**20)
        auth = AuthService(store, settings)
        auth.use_directory(report.ldap)
        auth.site_orgs = lambda: hub.report.building_model.site_orgs
        return cls(settings=settings, report=report, store=store, hub=hub, auth=auth,
                   control=ControlService(hub, store, report.control, settings))

    async def start(self) -> None:
        """Become the working node: drivers, control, notifications, schedules, housekeeping."""
        self.role = "active"
        await self.hub.start()
        await self.control.start()
        if self.tcp_ingest is not None:
            await self.tcp_ingest.start()
        if self.ingest.mqtt is not None:
            await self.ingest.mqtt.start()
        if self.notifier is not None:
            await self.notifier.start()
        if self.scheduler is not None:
            await self.scheduler.start()
        if self.automations is not None:
            await self.automations.start()
        if self.opcua_server is not None:
            await self.opcua_server.start()
        if self.northbound is not None:
            await self.northbound.start()
        if self.modbus_server is not None:
            await self.modbus_server.start()
        if self.security_watch is not None:
            await self.security_watch.start()
        if self.health_watch is not None:
            await self.health_watch.start()
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
        if self.northbound is not None and self.northbound.publishers:
            rows = self.northbound.describe()
            out.append(("hub_northbound_published_total", "counter",
                        "Messages published to northbound brokers",
                        [({"target": r["name"]}, r["published"]) for r in rows]))
            out.append(("hub_northbound_dropped_total", "counter",
                        "Messages dropped because the northbound queue was full",
                        [({"target": r["name"]}, r["dropped"]) for r in rows]))
            out.append(("hub_northbound_queued", "gauge", "Messages waiting for the broker",
                        [({"target": r["name"]}, r["queued"]) for r in rows]))
            out.append(("hub_northbound_connected", "gauge", "1 while connected to the broker",
                        [({"target": r["name"]}, 1 if r["connected"] else 0) for r in rows]))
        if self.health_watch is not None and self.health_watch.results:
            level = {"ok": 0, "unknown": 0, "warning": 1, "critical": 2}
            out.append(("hub_health_check", "gauge",
                        "Health checks of the hub: 0 ok, 1 warning, 2 critical",
                        [({"check": r.name}, level.get(r.status, 0))
                         for r in self.health_watch.results.values()]))
            disks = [r for r in self.health_watch.results.values() if r.name.startswith("disk:")]
            if disks:
                out.append(("hub_disk_free_percent", "gauge", "Free space of the hub's volumes",
                            [({"volume": r.details["volume"]}, r.details["free_percent"])
                             for r in disks]))
        if self.modbus_server is not None and self.modbus_server.server is not None:
            out.append(("hub_modbus_server_events_total", "counter",
                        "Modbus server requests, exceptions and writes",
                        [({"event": k}, v) for k, v in sorted(
                            self.modbus_server.counters.items())]))
        return out

    async def reload(self, report: ConfigReport) -> dict[str, list[str]]:
        """Apply a validated configuration live (used by backend.config_admin)."""
        changes = await self.hub.reload(report)
        self.report = report
        self.control.control = report.control
        self.auth.use_directory(report.ldap)
        if self.notifier is not None:
            self.notifier.config = report.notifications
        if self.opcua_server is not None and self.opcua_server.server is not None:
            await self.opcua_server.restart()  # the address space follows the devices
        if self.northbound is not None:
            await self.northbound.reload()
        if self.modbus_server is not None and self.role == "active":
            await self.modbus_server.restart()
        return changes

    async def stop(self) -> None:
        """Stop everything that talks to devices (also used to demote an HA node)."""
        if self.health_watch is not None:
            await self.health_watch.stop()
        if self.security_watch is not None:
            await self.security_watch.stop()
        if self.opcua_server is not None:
            await self.opcua_server.stop()
        if self.modbus_server is not None:
            await self.modbus_server.stop()
        if self.northbound is not None:
            await self.northbound.stop()
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.automations is not None:
            await self.automations.stop()
        if self.maintenance is not None:
            await self.maintenance.stop()
        if self.notifier is not None:
            await self.notifier.stop()
        if self.tcp_ingest is not None:
            await self.tcp_ingest.stop()
        if self.ingest is not None:
            await self.ingest.close()
        await self.control.stop()
        await self.hub.stop()
