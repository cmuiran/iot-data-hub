"""Write docs/api-reference.md from the running application's own routes (OpenAPI + the
permission each route requires), so the reference never drifts from the code.

    python scripts/api_reference.py            # writes docs/api-reference.md
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GROUPS = [
    ("Sign-in and sessions", ("/api/auth",)),
    ("Self-service accounts, homes", ("/api/account",)),
    ("Platform: organizations, products, versions, devices, labels", (
        "/api/platform/organizations", "/api/platform/products", "/api/platform/devices",
        "/api/platform/stats", "/api/platform/protocols", "/api/platform/catalog",
        "/api/platform/claim", "/api/platform/firmware", "/api/platform/trend",
        "/api/platform/tickets", "/api/platform/invoices", "/api/platform/calendar")),
    ("Platform: the buyer's devices (app)", ("/api/platform/my",)),
    ("Public", ("/api/public",)),
    ("Control (the control-safety layer)", ("/api/control",)),
    ("Live data, history, tags, devices (engineering dashboard)", (
        "/api/tags", "/api/devices", "/api/data", "/api/history", "/api/alarms", "/api/samples")),
    ("Configuration, building model, calculated tags", (
        "/api/config", "/api/model", "/api/building", "/api/calc", "/api/locations",
        "/api/equipment")),
    ("Dashboards, reports, schedules", ("/api/dashboards", "/api/reports", "/api/schedules")),
    ("Users and roles", ("/api/users", "/api/roles")),
    ("System, health, metrics", ("/api/system", "/api/health", "/api/metrics", "/api/versions",
                                 "/api/audit")),
    ("Devices of the platform (device credential)", ("/ingest",)),
]


def permissions(app: Any) -> dict[tuple[str, str], str]:
    """(method, path) -> the permission of its require(...) dependency."""
    from fastapi.routing import APIRoute

    out: dict[tuple[str, str], str] = {}

    def walk(dependant: Any) -> list[str]:
        found = []
        for dep in dependant.dependencies:
            call = dep.call
            closure = inspect.getclosurevars(call).nonlocals if callable(call) else {}
            if "permission" in closure:
                found.append(str(closure["permission"]))
            elif "any_write" in closure:
                found.append("any write permission (tag.write / control.* / device.operate)")
            found += walk(dep)
        return found

    def visit(routes: list[Any], prefix: str) -> None:
        for route in routes:
            nested = getattr(route, "original_router", None)     # FastAPI >= 0.16x nests them
            if nested is not None:
                context = getattr(route, "include_context", None)
                visit(nested.routes, prefix + (getattr(context, "prefix", "") or ""))
            elif isinstance(route, APIRoute):
                perms = walk(route.dependant)
                for method in route.methods:
                    out[(method, prefix + route.path)] = perms[0] if perms else ""

    visit(app.routes, "")
    return out


def main() -> int:
    import os

    os.environ.setdefault("HUB_JWT_SECRET", "x" * 40)
    from backend.app import create_app
    from backend.services import Services
    from backend.settings import Settings
    from hub.config_loader import load_config

    settings = Settings.model_validate({"jwt_secret": "x" * 40, "config_dir": ROOT / "config"})
    report = load_config(ROOT / "config", env={})
    from unittest.mock import MagicMock

    services = Services.build(settings, report, MagicMock())   # only the routes are read
    app = create_app(services)
    spec = app.openapi()
    perms = permissions(app)
    rows: dict[str, list[str]] = {g: [] for g, _ in GROUPS}
    other: list[str] = []
    count = 0
    for path, ops in sorted(spec["paths"].items()):
        for method, op in ops.items():
            if method.upper() in ("HEAD", "OPTIONS"):
                continue
            count += 1
            summary = (op.get("description") or op.get("summary") or "").strip().split("\n\n")[0]
            summary = " ".join(summary.split())
            if len(summary) > 220:
                summary = summary[:217] + "…"
            perm = perms.get((method.upper(), path), "")
            if path.startswith("/ingest/v1/broker"):
                perm = "X-Hub-Hook-Token"
            elif path.startswith("/ingest"):
                perm = perm or "device credential (Bearer)"
            elif perm == "self":
                perm = "signed in"
            elif not perm:
                perm = "public"
            line = f"| `{method.upper()}` | `{path}` | {perm} | {summary} |"
            for group, prefixes in GROUPS:
                if path.startswith(prefixes):
                    rows[group].append(line)
                    break
            else:
                other.append(line)
    out = ["# API reference", "",
           f"Generated from the application by `scripts/api_reference.py` ({count} operations). "
           "Interactive version with request and response schemas: **`/api/docs`** (Swagger) "
           "and `/api/openapi.json` on a running hub. How to call it, the WebSocket, the event "
           "stream, MQTT and TCP: `docs/fa/api-guide.md`.", "",
           "Permission: the role permission the route needs (organization permissions such as "
           "`product.read` are checked again against the organization in the path); "
           "`signed in` = any user; `public` = no session.", ""]
    for group, _ in GROUPS:
        if rows[group]:
            out += [f"## {group}", "", "| Method | Path | Permission | What it does |",
                    "|---|---|---|---|", *rows[group], ""]
    if other:
        out += ["## Other", "", "| Method | Path | Permission | What it does |",
                "|---|---|---|---|", *other, ""]
    out += ["## WebSocket", "", "| | Path | Permission | What it does |", "|---|---|---|---|",
            "| `WS` | `/api/ws` | data.read | live samples, device status, alarms and writes "
            "(the engineering dashboard) |", ""]
    target = ROOT / "docs" / "api-reference.md"
    target.write_text("\n".join(out), encoding="utf-8")
    print(f"{count} operations -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
