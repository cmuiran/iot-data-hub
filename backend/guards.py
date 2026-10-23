"""Request guards: network allowlist, per-address rate limit, WebSocket origin check.

* ``HUB_ALLOWED_NETWORKS`` (e.g. ``10.0.0.0/8,192.168.10.0/24``): only these client addresses
  reach the API, the WebSocket and /metrics. Empty = any address. /api/health stays open for
  load balancers.
* ``HUB_RATE_LIMIT_PER_MIN`` requests per client address and minute (sliding window, in memory,
  per node); more -> 429 with Retry-After.
* WebSocket origin: a browser always sends ``Origin``; it must be the hub's own host or listed in
  HUB_ALLOWED_ORIGINS / HUB_CORS_ORIGINS, so a foreign web page cannot open a live stream with a
  user's token (cross-site WebSocket hijacking). Non-browser clients send no Origin.

Behind a reverse proxy the client address is the proxy's unless uvicorn trusts the proxy's
X-Forwarded-For (``--forwarded-allow-ips``); configure that, or the allowlist and rate limit
see one address for everybody.
"""

from __future__ import annotations

import ipaddress
import time
from collections import deque
from urllib.parse import urlsplit

from backend.settings import Settings


class NetworkAllowlist:
    def __init__(self, networks: list[str]) -> None:
        self.networks = [ipaddress.ip_network(n, strict=False) for n in networks]

    def allows(self, address: str | None) -> bool:
        if not self.networks:
            return True
        if not address:
            return False
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in net for net in self.networks)


class RateLimiter:
    """Sliding one-minute window per key. ``check`` returns seconds to wait, 0 if allowed."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}
        self._clock = time.monotonic
        self._last_sweep = self._clock()

    def check(self, key: str) -> int:
        now = self._clock()
        if now - self._last_sweep > 300:  # forget idle clients
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
            self._last_sweep = now
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return int(60 - (now - hits[0])) + 1
        hits.append(now)
        return 0


def origin_allowed(origin: str | None, host: str | None, settings: Settings) -> bool:
    if not origin:
        return True  # not a browser
    allowed = {o.rstrip("/").lower() for o in [*settings.allowed_origins,
                                                *settings.cors_origins]}
    if origin.rstrip("/").lower() in allowed:
        return True
    return bool(host) and urlsplit(origin).netloc.lower() == host.lower()
