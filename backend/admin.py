"""Administration from the command line (works when nobody can log in yet).

    hub-admin migrate
    hub-admin create-admin --username admin [--display-name "Administrator"]
    hub-admin reset-password <username>
    hub-admin unlock <username>
    hub-admin list-users
    hub-admin backup --out backups [--keep 14]
    hub-admin make-cert --role client|server --name hub-client [--out pki] [--uri ...] [--host ...]
    hub-admin service-config --out scripts/windows/hub-api-service.xml [--account DOMAIN\\user]

Passwords are asked interactively (twice) or read from HUB_ADMIN_PASSWORD for automation.
Database: HUB_DATABASE_URL, or ``--embedded-db DIR`` for the development database.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from backend.security import hash_password, password_problems
from backend.settings import load_dotenv
from hub.runtime import run
from hub.storage.migrate import migrate
from hub.storage.postgres import PostgresStore


def _password(username: str, min_length: int) -> str:
    password = os.environ.get("HUB_ADMIN_PASSWORD")
    if not password:
        password = getpass.getpass(f"new password for {username}: ")
        if getpass.getpass("repeat: ") != password:
            raise SystemExit("passwords do not match")
    problems = password_problems(password, username, min_length)
    if problems:
        raise SystemExit("password rejected: " + "; ".join(problems))
    return password


async def _create_admin(url: str, username: str, display_name: str, min_length: int) -> int:
    async with PostgresStore(url, max_size=2) as store:
        if await store.get_user_by_username(username):
            print(f"user {username} already exists", file=sys.stderr)
            return 1
        user = await store.create_user(username, display_name,
                                       hash_password(_password(username, min_length)),
                                       must_change_password=False)
        await store.grant_role(user.user_id, "admin", None, None, None)
        await store.add_auth_audit("user_created", username, user_id=user.user_id,
                                   detail={"via": "hub-admin", "roles": ["admin"]})
    print(f"administrator {username} created")
    return 0


async def _reset(url: str, username: str, min_length: int, unlock_only: bool) -> int:
    async with PostgresStore(url, max_size=2) as store:
        user = await store.get_user_by_username(username)
        if user is None:
            print(f"no user {username}", file=sys.stderr)
            return 1
        if unlock_only:
            await store.update_user(user.user_id, failed_logins=0, locked_until=None)
            await store.add_auth_audit("unlocked", username, user_id=user.user_id,
                                       detail={"via": "hub-admin"})
            print(f"{username} unlocked")
            return 0
        await store.update_user(user.user_id,
                                password_hash=hash_password(_password(username, min_length)),
                                must_change_password=True, failed_logins=0, locked_until=None)
        await store.revoke_user_sessions(user.user_id)
        await store.add_auth_audit("password_changed", username, user_id=user.user_id,
                                   detail={"via": "hub-admin"})
    print(f"password of {username} reset; it must be changed at the next login")
    return 0


async def _list(url: str) -> int:
    async with PostgresStore(url, max_size=2) as store:
        for user in await store.list_users():
            roles = ", ".join(f"{g.role_id}@{g.site or '*'}{'/' + g.area if g.area else ''}"
                              for g in await store.grants(user.user_id))
            state = "active" if user.is_active else "disabled"
            print(f"{user.username:<20} {state:<8} {roles}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="hub-admin")
    parser.add_argument("--embedded-db", type=Path, metavar="DIR")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    create = sub.add_parser("create-admin")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", default="Administrator")
    for name in ("reset-password", "unlock"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list-users")
    backup = sub.add_parser("backup")
    backup.add_argument("--out", type=Path, default=Path(os.environ.get("HUB_BACKUP_DIR",
                                                                         "backups")))
    backup.add_argument("--keep", type=int, default=int(os.environ.get("HUB_BACKUP_KEEP", 14)))
    cert = sub.add_parser("make-cert")
    cert.add_argument("--role", choices=["client", "server"], required=True)
    cert.add_argument("--name", required=True)
    cert.add_argument("--out", type=Path, default=Path(os.environ.get("HUB_PKI_DIR", "pki")))
    cert.add_argument("--uri")
    cert.add_argument("--host")
    service = sub.add_parser("service-config")
    service.add_argument("--out", type=Path, default=Path("scripts/windows/hub-api-service.xml"))
    service.add_argument("--account", help="run as this Windows account (default LocalSystem)")
    args = parser.parse_args(argv)

    if args.command == "make-cert":  # needs no database
        from hub.pki import application_uri, make_certificate

        cert_path, key_path = run(make_certificate(args.out, args.name, args.role, uri=args.uri,
                                                   hostname=args.host))
        print(f"certificate: {cert_path}")
        print(f"private key: {key_path} (keep it secret)")
        print(f"application URI: {application_uri(cert_path)}")
        return 0
    if args.command == "service-config":  # needs no database
        from backend.service import render_winsw_xml

        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render_winsw_xml(Path.cwd(), account=args.account), encoding="utf-8")
        print(f"service definition written: {args.out}")
        return 0

    url = os.environ.get("HUB_DATABASE_URL")
    if args.embedded_db:
        from backend.server import embedded_database

        url = embedded_database(args.embedded_db)
    if not url:
        print("set HUB_DATABASE_URL or pass --embedded-db DIR", file=sys.stderr)
        return 2
    min_length = int(os.environ.get("HUB_PASSWORD_MIN_LENGTH", 10))
    migrate(url, os.environ.get("HUB_DB_TIMESCALE", "auto"))  # type: ignore[arg-type]
    if args.command == "migrate":
        print("database is up to date")
        return 0
    if args.command == "create-admin":
        return run(_create_admin(url, args.username.lower(), args.display_name, min_length))
    if args.command == "backup":
        from hub.maintenance import backup_database, prune_backups

        path = backup_database(url, args.out, os.environ.get("HUB_PG_DUMP"))
        removed = prune_backups(args.out, args.keep)
        print(f"backup written: {path} ({path.stat().st_size} bytes)"
              + (f"; removed {len(removed)} old file(s)" if removed else ""))
        return 0
    if args.command in ("reset-password", "unlock"):
        return run(_reset(url, args.username.lower(), min_length, args.command == "unlock"))
    return run(_list(url))


if __name__ == "__main__":
    sys.exit(main())
