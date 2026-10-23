"""Run the whole backend in one process: drivers, storage, alarms, control and the HTTP API.

    hub-api                                   # uses HUB_* environment variables / .env
    hub-api --dev --embedded-db .dev/db       # local development: throw-away PostgreSQL
    hub-api --config-dir .sim/config --dev --embedded-db .sim/db   # with hub-sim

Production needs HUB_DATABASE_URL and HUB_JWT_SECRET (>= 32 characters). ``--dev`` generates a
JWT secret per run when none is set and allows ``--embedded-db``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from backend.app import create_app
from backend.logging_setup import configure as configure_logging
from backend.services import Services
from backend.settings import Settings, load_dotenv
from hub.config_loader import load_config
from hub.runtime import run
from hub.storage.migrate import migrate
from hub.storage.postgres import PostgresStore
from hub.validate_config import print_report

log = logging.getLogger("hub.server")


def embedded_database(path: Path) -> str:
    """Start a local PostgreSQL in ``path`` (dev only; needs the pgserver dev dependency)."""
    try:
        import pgserver
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("--embedded-db needs the dev dependencies: pip install -e .[dev]") \
            from exc
    path.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(path.resolve()), cleanup_mode="stop")
    return server.get_uri()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hub-api", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--dev", action="store_true", help="development mode (see above)")
    parser.add_argument("--embedded-db", type=Path, metavar="DIR",
                        help="dev only: run a local PostgreSQL in DIR")
    parser.add_argument("--log-level", default="info")
    return parser.parse_args(argv)


async def serve(settings: Settings) -> int:
    import uvicorn

    report = load_config(settings.config_dir)
    if report.has_errors:
        print_report(report, sys.stderr)
        return 1
    assert settings.database_url
    await asyncio.to_thread(migrate, settings.database_url, settings.db_timescale)  # type: ignore[arg-type]
    store = PostgresStore(settings.database_url)
    await store.open()
    services = Services.build(settings, report, store)
    ha = None
    try:
        if settings.ha == "on":
            from backend.ha import HaCoordinator

            ha = HaCoordinator(services, settings.database_url)
            await ha.start()  # standby until it holds the lock
        else:
            await services.start()
        if await store.count_users() == 0:
            log.warning("no users yet: create the first administrator with "
                        "'hub-admin create-admin --username admin'")
        if not settings.control_enabled:
            log.info("control is disabled (HUB_CONTROL_ENABLED=false): writes are refused")
        tls: dict[str, str] = {}
        if settings.tls:
            tls = {"ssl_certfile": str(settings.tls_cert_file),
                   "ssl_keyfile": str(settings.tls_key_file)}
            log.info("HTTPS enabled (%s)", settings.tls_cert_file)
        elif settings.host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("hub-api listens on %s without TLS: passwords and tokens cross the "
                        "network unencrypted. Set HUB_TLS_CERT/HUB_TLS_KEY or put a TLS reverse "
                        "proxy in front", settings.host)
        config = uvicorn.Config(create_app(services), host=settings.host, port=settings.port,
                                loop="none", lifespan="off", log_level="info",
                                proxy_headers=True, ws="websockets-sansio", **tls)
        await uvicorn.Server(config).serve()
    finally:
        if ha is not None:
            await ha.stop()
        else:
            await services.stop()
        await store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    load_dotenv()
    try:
        settings = Settings.from_env(dev=args.dev)
    except ValueError as exc:
        print(f"invalid settings: {exc}\nHUB_JWT_SECRET must be set (>= 32 characters); "
              "use --dev for local development", file=sys.stderr)
        return 2
    updates: dict[str, object] = {}
    if args.config_dir:
        updates["config_dir"] = args.config_dir
    if args.host:
        updates["host"] = args.host
    if args.port:
        updates["port"] = args.port
    if args.embedded_db:
        if not args.dev:
            print("--embedded-db is for development only; add --dev", file=sys.stderr)
            return 2
        updates["database_url"] = embedded_database(args.embedded_db)
    settings = settings.model_copy(update=updates)
    configure_logging(args.log_level, settings.log_file, settings.log_max_mb, settings.log_backups)
    if not settings.database_url:
        print("set HUB_DATABASE_URL (or use --dev --embedded-db DIR)", file=sys.stderr)
        return 2
    return run(serve(settings))


if __name__ == "__main__":
    sys.exit(main())
