"""/metrics: Prometheus text exposition (format 0.0.4).

Off unless HUB_METRICS_TOKEN is set; then a scrape needs ``Authorization: Bearer <token>``
(Prometheus: ``authorization: {credentials: ...}`` in the scrape config). The endpoint shows no
tag values, only counters and states. Counters are per process and start at 0 on restart.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from backend.api.system import VERSION
from backend.deps import ServicesDep
from backend.errors import NotFound, Unauthorized
from hub.drivers.base import utcnow

router = APIRouter(tags=["system"])

_STATES = ("unknown", "online", "offline", "error", "disabled")
_SEVERITIES = ("low", "medium", "high", "critical")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: str) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(str(v))}"' for k, v in labels.items()) + "}"


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_text: str,
               samples: Iterable[tuple[dict[str, str], float]]) -> None:
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            self.lines.append(f"{name}{_labels(**labels)} {value:g}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, svc: ServicesDep) -> PlainTextResponse:
    token = svc.settings.metrics_token
    if not token:
        raise NotFound("metrics are disabled (HUB_METRICS_TOKEN)")
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip(), token):
        raise Unauthorized("metrics token required", code="not_authenticated")

    hub, out = svc.hub, _Out()
    s = hub.stats
    out.metric("hub_info", "gauge", "Hub version and node",
               [({"version": VERSION, "node": svc.settings.node_id}, 1)])
    out.metric("hub_uptime_seconds", "gauge", "Seconds since start",
               [({}, (utcnow() - svc.started_at).total_seconds())])
    for name, value, help_text in (
            ("hub_samples_received_total", s.samples_received, "Samples from drivers"),
            ("hub_samples_filtered_total", s.samples_filtered, "Samples not stored (deadband)"),
            ("hub_samples_written_total", s.samples_written, "Samples written to the database"),
            ("hub_samples_dropped_total", s.samples_dropped, "Samples lost: buffer overflow"),
            ("hub_storage_write_errors_total", s.write_errors, "Failed database writes")):
        out.metric(name, "counter", help_text, [({}, value)])
    out.metric("hub_storage_buffered", "gauge", "Samples waiting for the database",
               [({}, s.buffered)])
    out.metric("hub_live_subscribers", "gauge", "Open live (WebSocket) streams",
               [({}, hub.bus.subscriber_count)])

    up, states = [], []
    for device_id, status in sorted(hub.statuses.items()):
        d = hub.devices.get(device_id)
        if d is None:
            continue
        labels = {"device_id": device_id, "protocol": d.protocol, "site": d.site, "area": d.area}
        up.append((labels, 1 if str(status.state) == "online" else 0))
        states += [({"device_id": device_id, "state": st}, 1 if str(status.state) == st else 0)
                   for st in _STATES]
    out.metric("hub_device_up", "gauge", "1 if the device is online", up)
    out.metric("hub_device_state", "gauge", "Connection state per device", states)

    active = await svc.store.active_alarms()
    out.metric("hub_alarms_active", "gauge", "Active alarms (uncleared or unacknowledged)",
               [({"severity": sev}, sum(1 for a in active if a.severity == sev))
                for sev in _SEVERITIES])
    out.metric("hub_control_writes_total", "counter", "Write attempts by result",
               [({"result": r}, n) for r, n in sorted(svc.control.results.items())])
    out.metric("hub_logins_total", "counter", "Login attempts by outcome",
               [({"outcome": k}, v) for k, v in sorted(svc.auth.counters.items())])
    for name, kind, help_text, samples in svc.extra_metrics():
        out.metric(name, kind, help_text, samples)
    return PlainTextResponse(out.text(), media_type="text/plain; version=0.0.4; charset=utf-8")
