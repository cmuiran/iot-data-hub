"""Administration from the command line (works when nobody can log in yet).

    hub-admin migrate [--dry-run]
    hub-admin upgrade-check [--config-dir config]
    hub-admin verify-backup <file> [--full]
    hub-admin restore-backup <file> --yes
    hub-admin create-admin --username admin [--display-name "Administrator"]
    hub-admin reset-password <username>
    hub-admin unlock <username>
    hub-admin list-users
    hub-admin backup --out backups [--keep 14]
    hub-admin make-cert --role client|server --name hub-client [--out pki] [--uri ...] [--host ...]
    hub-admin service-config --out scripts/windows/hub-api-service.xml [--account DOMAIN\\user]
    hub-admin make-tls-cert --name hub.kish.local --name 192.168.10.5 [--out pki]
    hub-admin reset-mfa <username>
    hub-admin verify-audit
    hub-admin decrypt-backup <file.dump.enc> <file.dump>
    hub-admin notify-test [channel ...] [--config-dir config]   (no database needed)

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
        if user.auth_source == "ldap":
            print(f"{username} signs in with Active Directory: change the password there",
                  file=sys.stderr)
            return 1
        await store.update_user(user.user_id,
                                password_hash=hash_password(_password(username, min_length)),
                                must_change_password=True, failed_logins=0, locked_until=None)
        await store.revoke_user_sessions(user.user_id)
        await store.add_auth_audit("password_changed", username, user_id=user.user_id,
                                   detail={"via": "hub-admin"})
    print(f"password of {username} reset; it must be changed at the next login")
    return 0


async def _reset_mfa(url: str, username: str) -> int:
    async with PostgresStore(url, max_size=2) as store:
        user = await store.get_user_by_username(username)
        if user is None:
            print(f"no user {username}", file=sys.stderr)
            return 1
        await store.update_user(user.user_id, totp_secret_enc=None, totp_pending_enc=None,
                                totp_enabled=False, totp_last_step=None, recovery_codes=[])
        await store.revoke_user_sessions(user.user_id)
        await store.add_auth_audit("mfa_reset", username, user_id=user.user_id,
                                   detail={"via": "hub-admin"})
    print(f"MFA of {username} removed; they can enrol again after logging in")
    return 0


async def _verify_audit(url: str) -> int:
    bad = 0
    async with PostgresStore(url, max_size=2) as store:
        for table in ("write_audit", "auth_audit"):
            r = await store.audit_verify(table)
            if r["ok"]:
                print(f"{table}: OK, {r['checked']} rows, head {r['head_hash']}")
            else:
                bad += 1
                print(f"{table}: BROKEN at id {r['first_bad_id']}: {r['problem']} "
                      f"({r['checked']} rows verified before it)")
    return 1 if bad else 0


class _NoLog:
    """notify-test runs without a database: the result is printed instead of logged."""

    async def add_notification_log(self, **_: object) -> None:
        return None


async def _notify_test(config_dir: Path, channels: list[str], timezone: str) -> int:
    from backend.notifications import Notifier
    from hub.config_loader import load_config
    from hub.validate_config import print_report

    report = load_config(config_dir)
    if report.has_errors:
        print_report(report, sys.stderr)
        return 2
    known = report.notifications.channels
    if not known:
        print(f"no channels in {config_dir / 'notifications.yaml'}", file=sys.stderr)
        return 2
    names = channels or sorted(known)
    unknown = [n for n in names if n not in known]
    if unknown:
        print(f"unknown channel(s): {', '.join(unknown)}; configured: {', '.join(sorted(known))}",
              file=sys.stderr)
        return 2
    notifier = Notifier(report.notifications, _NoLog(), timezone=timezone,
                        scope_of=lambda _: None)
    await notifier.start()
    failed = 0
    try:
        for name in names:
            try:
                await notifier.send_test(name)
                print(f"{name} ({known[name].type}): sent - check that it arrived")
            except Exception as exc:  # noqa: BLE001 - reported per channel
                failed += 1
                print(f"{name} ({known[name].type}): FAILED - {exc}")
    finally:
        await notifier.stop()
    return 1 if failed else 0


async def _list(url: str) -> int:
    async with PostgresStore(url, max_size=2) as store:
        for user in await store.list_users():
            roles = ", ".join(f"{g.role_id}@{g.site or '*'}{'/' + g.area if g.area else ''}"
                              for g in await store.grants(user.user_id))
            state = "active" if user.is_active else "disabled"
            print(f"{user.username:<20} {state:<8} {roles}")
    return 0


async def _last_backup(url: str) -> dict[str, object] | None:
    async with PostgresStore(url, max_size=1) as store:
        events = await store.events("backup_ok", 1)
    return events[0] if events else None


def _upgrade_check(url: str, config_dir: Path) -> int:
    """Everything that must be true before installing a new version (see docs/backend.md)."""
    import shutil
    from datetime import UTC, datetime

    from backend.api.system import VERSION
    from hub.config_loader import load_config
    from hub.maintenance import find_pg_tool
    from hub.storage.migrate import status

    problems: list[str] = []
    print(f"hub version installed: {VERSION}")
    state = status(url)
    print(f"database schema: {len(state['applied'])} migration(s) applied"
          + (f", pending: {', '.join(state['pending'])}" if state["pending"] else "")
          + (f", optional not applied: {', '.join(state['skipped'])}" if state["skipped"] else ""))
    if state["unknown"]:
        problems.append(f"the database is NEWER than this version ({', '.join(state['unknown'])})")
    report = load_config(config_dir)
    errors = [i for i in report.issues if i.severity == "ERROR"]
    print(f"configuration {config_dir}: {len(report.devices)} device(s), {len(errors)} error(s)")
    if errors:
        problems.append("the configuration has errors (python -m hub.validate_config)")
    try:
        for tool in ("pg_dump", "pg_restore"):
            print(f"{tool}: {find_pg_tool(tool, os.environ.get('HUB_PG_DUMP'))}")
    except FileNotFoundError as exc:
        problems.append(str(exc))
    if state["applied"]:
        last = run(_last_backup(url))
        if last is None:
            problems.append("no backup recorded: run hub-admin backup before upgrading")
        else:
            age = datetime.now(UTC) - last["ts"]  # type: ignore[operator]
            print(f"last backup: {last['detail'].get('file')} "  # type: ignore[union-attr]
                  f"({age.total_seconds() / 3600:.1f} h ago)")
            if age.total_seconds() > 24 * 3600:
                problems.append("the last backup is older than 24 h: run hub-admin backup first")
    usage = shutil.disk_usage(Path.cwd())
    print(f"free disk space here: {usage.free / 1e9:.1f} GB")
    if usage.free < 2e9:
        problems.append("less than 2 GB free for the new version, backup and logs")
    if problems:
        print("\nNOT READY:")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\nready to upgrade (stop the service, install, start: see scripts/windows/upgrade.ps1)")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="hub-admin")
    parser.add_argument("--embedded-db", type=Path, metavar="DIR")
    sub = parser.add_subparsers(dest="command", required=True)
    mig = sub.add_parser("migrate")
    mig.add_argument("--dry-run", action="store_true", help="only list pending migrations")
    check = sub.add_parser("upgrade-check")
    check.add_argument("--config-dir", type=Path,
                       default=Path(os.environ.get("HUB_CONFIG_DIR", "config")))
    verify = sub.add_parser("verify-backup")
    verify.add_argument("file", type=Path)
    verify.add_argument("--full", action="store_true",
                        help="restore into a temporary database (needs CREATEDB) and check it")
    restore = sub.add_parser("restore-backup")
    restore.add_argument("file", type=Path)
    restore.add_argument("--yes", action="store_true",
                         help="confirm: the current database contents are replaced")
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
    tls = sub.add_parser("make-tls-cert")
    tls.add_argument("--name", action="append", required=True,
                     help="host name or IP the browsers use (repeat for several)")
    tls.add_argument("--out", type=Path, default=Path(os.environ.get("HUB_PKI_DIR", "pki")))
    sub.add_parser("reset-mfa").add_argument("username")
    sub.add_parser("verify-audit")
    dec = sub.add_parser("decrypt-backup")
    dec.add_argument("src", type=Path)
    dec.add_argument("dst", type=Path)
    cert = sub.add_parser("make-cert")
    cert.add_argument("--role", choices=["client", "server"], required=True)
    cert.add_argument("--name", required=True)
    cert.add_argument("--out", type=Path, default=Path(os.environ.get("HUB_PKI_DIR", "pki")))
    cert.add_argument("--uri")
    cert.add_argument("--host")
    notify = sub.add_parser("notify-test", help="send a test message on notification channels")
    notify.add_argument("channel", nargs="*", help="channel names (default: all)")
    notify.add_argument("--config-dir", type=Path,
                        default=Path(os.environ.get("HUB_CONFIG_DIR", "config")))
    service = sub.add_parser("service-config")
    service.add_argument("--out", type=Path, default=Path("scripts/windows/hub-api-service.xml"))
    service.add_argument("--account", help="run as this Windows account (default LocalSystem)")
    args = parser.parse_args(argv)

    if args.command == "make-tls-cert":  # needs no database
        from hub.pki import make_tls_certificate

        cert_path, key_path = make_tls_certificate(args.out, args.name)
        print(f"HTTPS certificate: {cert_path}")
        print(f"private key: {key_path} (keep it secret)")
        print(f"set HUB_TLS_CERT={cert_path} and HUB_TLS_KEY={key_path}")
        return 0
    if args.command == "decrypt-backup":  # needs no database
        from backend.crypto import decrypt_file

        key = os.environ.get("HUB_DATA_KEY")
        if not key:
            print("set HUB_DATA_KEY (the key the backup was made with)", file=sys.stderr)
            return 2
        decrypt_file(key, args.src, args.dst)
        print(f"decrypted: {args.dst} (restore with pg_restore)")
        return 0
    if args.command == "make-cert":  # needs no database
        from hub.pki import application_uri, make_certificate

        cert_path, key_path = run(make_certificate(args.out, args.name, args.role, uri=args.uri,
                                                   hostname=args.host))
        print(f"certificate: {cert_path}")
        print(f"private key: {key_path} (keep it secret)")
        print(f"application URI: {application_uri(cert_path)}")
        return 0
    if args.command == "notify-test":  # needs no database
        return run(_notify_test(args.config_dir, args.channel,
                                os.environ.get("HUB_TIMEZONE", "Asia/Tehran")))
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
    # commands that must not change the schema
    if args.command == "upgrade-check":
        return _upgrade_check(url, args.config_dir)
    if args.command == "migrate" and args.dry_run:
        from hub.storage.migrate import status

        state = status(url)
        print(f"applied: {len(state['applied'])}; pending: {', '.join(state['pending']) or 'none'}")
        if state["unknown"]:
            print(f"database is NEWER than this version: {', '.join(state['unknown'])}",
                  file=sys.stderr)
            return 1
        return 0
    if args.command == "verify-backup":
        from hub.maintenance import check_backup_archive, restore_test

        key = os.environ.get("HUB_DATA_KEY")
        result = (restore_test(url, args.file, key, os.environ.get("HUB_PG_DUMP")) if args.full
                  else check_backup_archive(args.file, key, os.environ.get("HUB_PG_DUMP")))
        print(f"backup OK: {result}")
        return 0
    if args.command == "restore-backup":
        from hub.maintenance import restore_backup

        if not args.yes:
            print("restore-backup replaces the whole database with the backup. Stop hub-api "
                  "(the Windows service) first, then repeat with --yes.", file=sys.stderr)
            return 2
        restore_backup(url, args.file, os.environ.get("HUB_DATA_KEY"),
                       os.environ.get("HUB_PG_DUMP"))
        print(f"restored {args.file.name}; start hub-api (it applies newer migrations, if any)")
        return 0
    from hub.storage.migrate import SchemaTooNew

    try:
        migrate(url, os.environ.get("HUB_DB_TIMESCALE", "auto"))  # type: ignore[arg-type]
    except SchemaTooNew as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.command == "migrate":
        print("database is up to date")
        return 0
    if args.command == "create-admin":
        return run(_create_admin(url, args.username.lower(), args.display_name, min_length))
    if args.command == "backup":
        from hub.maintenance import backup_database, prune_backups

        path = backup_database(url, args.out, os.environ.get("HUB_PG_DUMP"))
        if os.environ.get("HUB_DATA_KEY"):
            from hub.maintenance import encrypt_backup

            path = encrypt_backup(path, os.environ["HUB_DATA_KEY"])
        removed = prune_backups(args.out, args.keep)
        print(f"backup written: {path} ({path.stat().st_size} bytes)"
              + (f"; removed {len(removed)} old file(s)" if removed else ""))
        return 0
    if args.command == "reset-mfa":
        return run(_reset_mfa(url, args.username.lower()))
    if args.command == "verify-audit":
        return run(_verify_audit(url))
    if args.command in ("reset-password", "unlock"):
        return run(_reset(url, args.username.lower(), min_length, args.command == "unlock"))
    return run(_list(url))


if __name__ == "__main__":
    sys.exit(main())
