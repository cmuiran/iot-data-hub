"""Daily security checks on the active node: certificate expiry and audit-chain integrity.

* Certificates: the HTTPS certificate, everything in HUB_PKI_DIR (OPC UA server/client) and the
  client certificates referenced by OPC UA devices. One expiring within ``cert_warn_days`` raises
  a system alarm ``system:cert:<file>`` (high; critical once expired), cleared after renewal.
  System alarms show in the alarm list and go through notification rules like any other alarm.
* Audit: ``audit_verify()`` on both audit tables. The chain heads are written to
  ``system_events`` (``audit_anchor``) and the log every day — copies outside the audit tables, so
  a chain recomputed after tampering no longer matches an earlier anchor. A broken chain raises
  the critical system alarm ``system:audit``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hub.alarms import AlarmRaise
from hub.drivers.base import utcnow
from hub.models import AlarmSeverity, OpcUaDevice
from hub.pki import certificate_expiry

log = logging.getLogger(__name__)
INTERVAL_S = 24 * 3600


class SecurityWatch:
    def __init__(self, services: Any) -> None:
        self.services = services
        self._task: asyncio.Task[None] | None = None
        self._open: set[str] = set()

    async def start(self) -> None:
        open_sources = await self.services.store.open_alarm_sources()
        self._open = {s for s in open_sources if s.startswith("system:")}
        self._task = asyncio.create_task(self._loop(), name="security-watch")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("security watch failed")
            await asyncio.sleep(INTERVAL_S)

    async def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        return {"certificates": await self.check_certificates(now),
                "audit": await self.check_audit(now)}

    # --- certificates ---------------------------------------------------------------------------

    def certificate_files(self) -> list[Path]:
        s = self.services.settings
        files: list[Path] = []
        if s.tls_cert_file:
            files.append(Path(s.tls_cert_file))
        if Path(s.pki_dir).is_dir():
            files += sorted(p for p in Path(s.pki_dir).rglob("*")
                            if p.suffix.lower() in (".der", ".crt", ".cer")
                            or p.name.endswith(".crt.pem"))
        for device in self.services.hub.devices.values():
            if isinstance(device, OpcUaDevice) and device.connection.client_certificate_env:
                value = os.environ.get(device.connection.client_certificate_env)
                if value:
                    files.append(Path(value))
        seen: set[Path] = set()
        return [f for f in files if not (f.resolve() in seen or seen.add(f.resolve()))]

    async def check_certificates(self, now: datetime) -> list[dict[str, Any]]:
        warn = timedelta(days=self.services.settings.cert_warn_days)
        report = []
        current: set[str] = set()
        for path in self.certificate_files():
            try:
                expires = certificate_expiry(path)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot read certificate %s: %s", path, exc)
                continue
            left = expires - now
            source = f"system:cert:{path.name}"
            report.append({"file": str(path), "expires": expires.isoformat(),
                           "days_left": left.days})
            if left <= warn:
                current.add(source)
                if source not in self._open:
                    expired = left.total_seconds() <= 0
                    await self._raise(source, AlarmSeverity.CRITICAL if expired
                                      else AlarmSeverity.HIGH,
                                      f"certificate {path.name} "
                                      + ("has expired" if expired else
                                         f"expires in {left.days} days ({expires:%Y-%m-%d})"),
                                      now)
        for source in [s for s in self._open if s.startswith("system:cert:")
                       and s not in current]:
            await self._clear(source, now)
        return report

    # --- audit chain ----------------------------------------------------------------------------

    async def check_audit(self, now: datetime) -> dict[str, Any]:
        store = self.services.store
        results = {t: await store.audit_verify(t) for t in ("write_audit", "auth_audit")}
        anchors = {t: r["head_hash"] for t, r in results.items()}
        await store.add_event("audit_anchor", self.services.settings.node_id,
                              {"heads": anchors, "rows": {t: r["checked"]
                                                          for t, r in results.items()}})
        log.info("audit anchor %s", anchors)
        broken = {t: r for t, r in results.items() if not r["ok"]}
        if broken and "system:audit" not in self._open:
            detail = "; ".join(f"{t} at id {r['first_bad_id']}: {r['problem']}"
                               for t, r in broken.items())
            await self._raise("system:audit", AlarmSeverity.CRITICAL,
                              f"audit log integrity broken: {detail}", now)
        elif not broken and "system:audit" in self._open:
            await self._clear("system:audit", now)
        return results

    # --- alarms -----------------------------------------------------------------------------------

    async def _raise(self, source: str, severity: AlarmSeverity, message: str,
                     now: datetime) -> None:
        self._open.add(source)
        log.error("SECURITY: %s", message)
        await self.services.hub.raise_system_alarm(AlarmRaise(
            source=source, kind="state", severity=severity, message=message, value=None,
            ts=now))

    async def _clear(self, source: str, now: datetime) -> None:
        self._open.discard(source)
        await self.services.hub.clear_system_alarm(source, now)
