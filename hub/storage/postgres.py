"""PostgreSQL store (psycopg 3, async pool). The only persistence layer of the hub.

Schema: db/migrations (applied with :func:`hub.storage.migrate.migrate`). Works on plain
PostgreSQL; with TimescaleDB, ``samples`` is a hypertable and nothing here changes.

Values: bool -> value_bool, str -> value_text, numbers -> value_num. Integer tags come back as
int. Audit values are stored as JSON text so ``true``/``1``/``"1"`` stay distinguishable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from hub.drivers.base import AnyDevice, DeviceStatus
from hub.models import (
    INTEGER_RANGES,
    QUALITY_CODES,
    REASON_CODES,
    BuildingFile,
    CalcTag,
    DataType,
    EnipTag,
    FinsTag,
    HttpTag,
    Iec104Tag,
    McTag,
    ModbusTag,
    MqttTag,
    OpcUaTag,
    S7Tag,
    Sample,
    SnmpTag,
    TagBase,
)
from hub.storage.records import (
    AlarmRecord,
    AuthAuditRecord,
    CollectionError,
    DeviceStatusRecord,
    HistoryPoint,
    LatestValue,
    MaintenanceRecord,
    PendingWrite,
    RoleGrant,
    SessionRecord,
    TagRecord,
    UserRecord,
    WriteAuditRecord,
)

log = logging.getLogger(__name__)
audit_log = logging.getLogger("hub.audit")  # copy of every audit row for the SIEM
_SECURITY_EVENTS = frozenset({"login_failed", "login_throttled", "locked", "mfa_failed",
                              "network_denied", "user_disabled", "mfa_reset",
                              "directory_unavailable"})

_QUALITY_BY_CODE = {v: k for k, v in QUALITY_CODES.items()}
_REASON_BY_CODE = {v: k for k, v in REASON_CODES.items()}
_INTEGER_TYPES = frozenset(INTEGER_RANGES)

_USER_FIELDS = {"display_name", "password_hash", "is_active", "failed_logins", "locked_until",
                "must_change_password", "language", "last_login_at", "totp_secret_enc",
                "totp_pending_enc", "totp_enabled", "totp_last_step", "recovery_codes",
                "password_changed_at", "password_history", "external_dn"}


def hour_origin(tz: str) -> datetime:
    """A local hour boundary of ``tz`` as an aware datetime: ``date_bin('1 hour', ts, origin)``
    then cuts at the zone's hours (Tehran: :30 UTC) without needing PostgreSQL's tz database.
    Correct for zones whose UTC offset keeps the same minutes all year (all but a few)."""
    from zoneinfo import ZoneInfo

    return datetime(2020, 1, 1, tzinfo=ZoneInfo(tz))


def encode_value(value: object) -> str:
    return json.dumps(value)


def _split(value: object) -> tuple[float | None, bool | None, str | None]:
    if value is None:
        return None, None, None
    if isinstance(value, bool):
        return None, value, None
    if isinstance(value, str):
        return None, None, value
    return float(value), None, None  # type: ignore[arg-type]


_UNNEST = ("unnest(%s::integer[], %s::timestamptz[], %s::float8[], %s::boolean[], %s::text[], "
           "%s::smallint[], %s::smallint[])")
_UNNEST_LATEST = ("unnest(%s::integer[], %s::timestamptz[], %s::float8[], %s::boolean[], "
                  "%s::text[], %s::smallint[], %s::smallint[], %s::timestamptz[])")


def _columns(rows: Sequence[tuple], width: int = 7) -> tuple[list[Any], ...]:
    """Sample rows (tag_id, ts, num, bool, text, quality, reason[, received_at]) as arrays for
    _UNNEST / _UNNEST_LATEST."""
    return tuple(list(column) for column in zip(*rows, strict=True)) if rows else ([],) * width


def _join(data_type: str, num: float | None, flag: bool | None, text: str | None) -> Any:
    if flag is not None:
        return flag
    if text is not None:
        return text
    if num is not None and DataType(data_type) in _INTEGER_TYPES and float(num).is_integer():
        return int(num)
    return num


def _address(tag: TagBase) -> dict[str, Any]:
    if isinstance(tag, ModbusTag):
        return {k: v for k, v in {"register_type": str(tag.register_type), "address": tag.address,
                                  "bit": tag.bit, "bit_count": tag.bit_count, "count": tag.count,
                                  "encoding": None if tag.encoding == "binary" else tag.encoding,
                                  }.items() if v is not None}
    if isinstance(tag, MqttTag):
        return {"json_path": tag.json_path}
    if isinstance(tag, OpcUaTag):
        return {"node_id": tag.node_id}
    if isinstance(tag, S7Tag):
        return {"address": tag.address} | (
            {"string_length": tag.string_length} if tag.string_length else {})
    if isinstance(tag, HttpTag):
        return {"path": tag.path, "json_path": tag.json_path}
    if isinstance(tag, SnmpTag):
        return {"oid": tag.oid}
    if isinstance(tag, FinsTag | McTag):
        return {"address": tag.address.upper()}
    if isinstance(tag, EnipTag):
        return {"tag": tag.tag} | ({"bit": tag.bit} if tag.bit is not None else {})
    if isinstance(tag, CalcTag):
        return {"formula": tag.formula, "inputs": dict(tag.inputs)}
    if isinstance(tag, Iec104Tag):
        return {"ioa": tag.ioa, "type": tag.type} | (
            {"command_ioa": tag.command_ioa} if tag.command_ioa else {})
    return {}


class PostgresStore:
    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.database_url = database_url
        self._pool = AsyncConnectionPool(database_url, min_size=min_size, max_size=max_size,
                                         open=False, kwargs={"row_factory": dict_row})
        self._tag_ids: dict[str, int] = {}
        self._tag_types: dict[int, str] = {}
        from hub.storage.platform import PlatformStore

        self.platform = PlatformStore(self)      # products, platform devices (0009)
        from hub.storage.accounts import AccountStore

        self.accounts = AccountStore(self)       # sign-up, invitations, sharing (0010)
        from hub.storage.claims import ClaimStore

        self.claims = ClaimStore(self)           # claim codes, credentials, edge hubs (0011)
        from hub.storage.commands import CommandStore

        self.commands = CommandStore(self)       # the life of every command (0012)
        from hub.storage.automations import AutomationStore

        self.automations = AutomationStore(self)  # scenes, automations, holidays (0013)
        from hub.storage.versions import VersionStore

        self.versions = VersionStore(self)       # template upgrades, firmware (0014)

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=30)

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> PostgresStore:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _conn(self) -> Any:
        return self._pool.connection()

    # --- config sync --------------------------------------------------------------------------

    async def sync_config(self, devices: Sequence[AnyDevice]) -> dict[str, TagRecord]:
        """Mirror the YAML config: upsert devices/tags, retire what disappeared. Platform
        devices (source 'platform', from the database) are never touched."""
        async with self._conn() as conn, conn.transaction():
            for d in devices:
                connection = getattr(d, "connection", None)    # calc devices have none
                conn_json = connection.model_dump(mode="json") if connection else {}
                await conn.execute(
                    "INSERT INTO devices (device_id, name, site, area, protocol, enabled, "
                    "connection, description) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (device_id) DO UPDATE SET name=EXCLUDED.name, site=EXCLUDED.site,"
                    " area=EXCLUDED.area, protocol=EXCLUDED.protocol, enabled=EXCLUDED.enabled,"
                    " connection=EXCLUDED.connection, description=EXCLUDED.description,"
                    " updated_at=now(), retired_at=NULL WHERE devices.source = 'config'",
                    (d.device_id, d.name, d.site, d.area, d.protocol, d.enabled,
                     Jsonb(conn_json), d.description),
                )
                for t in d.tags:
                    await conn.execute(
                        "INSERT INTO tags (device_id, name, description, data_type, unit, scale,"
                        ' "offset", access, min_value, max_value, address, write_class) '
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (device_id, name) DO UPDATE SET "
                        "description=EXCLUDED.description, data_type=EXCLUDED.data_type, "
                        'unit=EXCLUDED.unit, scale=EXCLUDED.scale, "offset"=EXCLUDED."offset", '
                        "access=EXCLUDED.access, min_value=EXCLUDED.min_value, "
                        "max_value=EXCLUDED.max_value, address=EXCLUDED.address, "
                        "write_class=EXCLUDED.write_class, updated_at=now(), retired_at=NULL",
                        (d.device_id, t.name, t.description, str(t.data_type), t.unit, t.scale,
                         t.offset, str(t.access), t.min, t.max, Jsonb(_address(t)),
                         t.write_class and str(t.write_class)),
                    )
                    if isinstance(t, CalcTag):
                        await self._record_calc_version(conn, d.device_id, t)
                await conn.execute(
                    "UPDATE tags SET retired_at=now() WHERE device_id=%s AND retired_at IS NULL "
                    "AND NOT (name = ANY(%s))", (d.device_id, [t.name for t in d.tags]))
                await conn.execute(
                    "INSERT INTO device_status (device_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (d.device_id,))
            await conn.execute(
                "UPDATE devices SET retired_at=now() WHERE retired_at IS NULL "
                "AND source = 'config' AND NOT (device_id = ANY(%s))",
                ([d.device_id for d in devices],))
            await conn.execute(
                "UPDATE tags SET retired_at=now() WHERE retired_at IS NULL AND device_id IN "
                "(SELECT device_id FROM devices WHERE retired_at IS NOT NULL)")
        return await self.tags()

    async def _record_calc_version(self, conn: Any, device_id: str, tag: CalcTag) -> None:
        """A new row when the definition differs from the one in force (by version hash)."""
        from hub.calc.engine import definition_version

        version = definition_version(tag)
        definition = tag.model_dump(mode="json", include={
            "formula", "inputs", "unit", "data_type", "missing", "stale_s", "max_gap_s",
            "min_interval_s", "period_s"})
        await conn.execute(
            "INSERT INTO calc_versions (tag_id, version, definition) "
            "SELECT t.tag_id, %s, %s FROM tags t WHERE t.device_id=%s AND t.name=%s "
            "AND %s IS DISTINCT FROM (SELECT v.version FROM calc_versions v "
            "WHERE v.tag_id=t.tag_id ORDER BY v.valid_from DESC LIMIT 1)",
            (version, Jsonb(definition), device_id, tag.name, version))

    async def calc_versions(self, tag_path: str) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT v.valid_from, v.version, v.definition FROM calc_versions v "
                "JOIN tag_paths p USING (tag_id) WHERE p.tag_path=%s ORDER BY v.valid_from DESC",
                (tag_path,))).fetchall()
        return [dict(r) for r in rows]

    async def tags(self) -> dict[str, TagRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT p.tag_id, p.tag_path, p.device_id, p.site, p.area, p.tag AS name, "
                "p.data_type, p.unit, p.access, p.min_value, p.max_value, t.description "
                "FROM tag_paths p JOIN tags t USING (tag_id) ORDER BY p.tag_path")).fetchall()
        records = {r["tag_path"]: TagRecord.model_validate(r) for r in rows}
        self._tag_ids = {p: r.tag_id for p, r in records.items()}
        self._tag_types = {r.tag_id: str(r.data_type) for r in records.values()}
        return records

    async def refresh_device_tags(self, device_id: str) -> None:
        """Refresh the tag ids of one device (a platform device started, changed or stopped)
        without reading the whole tag table."""
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT tag_id, tag_path, data_type FROM tag_paths WHERE device_id=%s",
                (device_id,))).fetchall()
        prefix = f".{device_id}."
        for path in [p for p in self._tag_ids if prefix in p]:
            del self._tag_ids[path]
        for r in rows:
            self._tag_ids[r["tag_path"]] = r["tag_id"]
            self._tag_types[r["tag_id"]] = str(r["data_type"])

    # --- samples ------------------------------------------------------------------------------

    async def write_samples(self, samples: Sequence[Sample]) -> int:
        """Insert samples (duplicates ignored) and advance tag_latest. Returns rows written."""
        if not self._tag_ids:
            await self.tags()
        rows = []
        for s in samples:
            tag_id = self._tag_ids.get(s.tag)
            if tag_id is None:
                log.warning("sample for unknown tag %s dropped", s.tag)
                continue
            num, flag, text = _split(s.value)
            rows.append((tag_id, s.timestamp, num, flag, text, QUALITY_CODES[s.quality],
                         REASON_CODES[s.reason] if s.reason else None, s.received_at))
        if not rows:
            return 0
        latest: dict[int, tuple] = {}
        for row in rows:
            if row[0] not in latest or row[1] >= latest[row[0]][1]:
                latest[row[0]] = row
        # one statement per table and batch (arrays + unnest): executemany costs a round trip
        # per row, which made the event loop the bottleneck above ~2000 samples/s.
        # _UNNEST is a constant; every value is a parameter.
        async with self._conn() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO samples (tag_id, ts, value_num, value_bool, value_text, quality, "
                f"reason) SELECT * FROM {_UNNEST} "  # nosec B608
                "ON CONFLICT (tag_id, ts) DO NOTHING",
                _columns([r[:7] for r in rows]))
            await conn.execute(
                "INSERT INTO tag_latest (tag_id, ts, value_num, value_bool, value_text, quality, "
                f"reason, received_at) SELECT * FROM {_UNNEST_LATEST} "  # nosec B608
                "ON CONFLICT (tag_id) DO UPDATE SET "
                "ts=EXCLUDED.ts, value_num=EXCLUDED.value_num, "
                "value_bool=EXCLUDED.value_bool, value_text=EXCLUDED.value_text, "
                "quality=EXCLUDED.quality, reason=EXCLUDED.reason, "
                "received_at=EXCLUDED.received_at WHERE tag_latest.ts <= EXCLUDED.ts",
                _columns(list(latest.values()), 8))
        return len(rows)

    async def latest(self, tag_paths: Iterable[str] | None = None) -> list[LatestValue]:
        sql = ("SELECT p.tag_path, p.data_type, l.ts, l.value_num, l.value_bool, l.value_text, "
               "l.quality, l.reason, l.received_at FROM tag_latest l JOIN tag_paths p "
               "USING (tag_id)")
        params: tuple = ()
        if tag_paths is not None:
            sql += " WHERE p.tag_path = ANY(%s)"
            params = (list(tag_paths),)
        async with self._conn() as conn:
            rows = await (await conn.execute(sql + " ORDER BY p.tag_path", params)).fetchall()
        return [LatestValue(tag_path=r["tag_path"], ts=r["ts"],
                            value=_join(r["data_type"], r["value_num"], r["value_bool"],
                                        r["value_text"]),
                            quality=_QUALITY_BY_CODE[r["quality"]],
                            reason=_REASON_BY_CODE.get(r["reason"]),
                            received_at=r["received_at"]) for r in rows]

    async def history(self, tag_path: str, start: datetime, end: datetime,
                      max_points: int = 1000) -> list[HistoryPoint]:
        """Raw samples, or time buckets (avg/min/max of good values) above ``max_points``."""
        if not self._tag_ids:
            await self.tags()
        tag_id = self._tag_ids.get(tag_path)
        if tag_id is None:
            return []
        data_type = self._tag_types[tag_id]
        numeric = data_type not in ("bool", "string")
        bucket = max((end - start) / max_points, timedelta(seconds=1))
        if numeric and bucket >= timedelta(hours=1):
            return await self._history_from_hourly(tag_id, start, end, bucket)
        async with self._conn() as conn:
            count = (await (await conn.execute(
                "SELECT count(*) AS n FROM samples WHERE tag_id=%s AND ts >= %s AND ts < %s",
                (tag_id, start, end))).fetchone())["n"]
            if count <= max_points or not numeric:
                rows = await (await conn.execute(
                    "SELECT ts, value_num, value_bool, value_text, quality FROM samples "
                    "WHERE tag_id=%s AND ts >= %s AND ts < %s ORDER BY ts LIMIT %s",
                    (tag_id, start, end, max_points * (1 if numeric else 10)))).fetchall()
                return [HistoryPoint(ts=r["ts"], value=_join(data_type, r["value_num"],
                                                             r["value_bool"], r["value_text"]),
                                     quality=_QUALITY_BY_CODE[r["quality"]]) for r in rows]
            rows = await (await conn.execute(
                "SELECT date_bin(%s, ts, %s) AS bucket, avg(value_num) AS avg, "
                "min(value_num) AS min, max(value_num) AS max, count(*) AS n FROM samples "
                "WHERE tag_id=%s AND ts >= %s AND ts < %s AND quality = 0 "
                "GROUP BY bucket ORDER BY bucket",
                (bucket, start, tag_id, start, end))).fetchall()
        return [HistoryPoint(ts=r["bucket"], avg=r["avg"], min=r["min"], max=r["max"],
                             count=r["n"]) for r in rows]

    async def _history_from_hourly(self, tag_id: int, start: datetime, end: datetime,
                                   bucket: timedelta) -> list[HistoryPoint]:
        """Buckets of >= 1 h: hourly roll-up where it exists, raw samples after it (so data
        older than the raw retention still shows, and the last hours are not missing)."""
        async with self._conn() as conn:
            last = (await (await conn.execute(
                "SELECT max(bucket) AS b FROM samples_hourly WHERE tag_id=%s",
                (tag_id,))).fetchone())["b"]
            cut = (last + timedelta(hours=1)) if last is not None else start
            cut = min(max(cut, start), end)
            rows = await (await conn.execute(
                "WITH h AS (SELECT date_bin(%(b)s, bucket, %(s)s) AS b, sum(avg_value * n) AS t,"
                " min(min_value) AS mn, max(max_value) AS mx, sum(n) AS n FROM samples_hourly"
                " WHERE tag_id=%(id)s AND bucket >= %(s)s AND bucket < %(c)s GROUP BY 1),"
                " r AS (SELECT date_bin(%(b)s, ts, %(s)s) AS b, sum(value_num) AS t,"
                " min(value_num) AS mn, max(value_num) AS mx, count(*) AS n FROM samples"
                " WHERE tag_id=%(id)s AND quality = 0 AND value_num IS NOT NULL"
                " AND ts >= %(c)s AND ts < %(e)s GROUP BY 1)"
                " SELECT b, sum(t) / sum(n) AS avg, min(mn) AS min, max(mx) AS max,"
                " sum(n) AS n FROM (SELECT * FROM h UNION ALL SELECT * FROM r) x"
                " GROUP BY b ORDER BY b",
                {"b": bucket, "s": start, "e": end, "c": cut, "id": tag_id})).fetchall()
        return [HistoryPoint(ts=r["b"], avg=r["avg"], min=r["min"], max=r["max"],
                             count=int(r["n"])) for r in rows]

    async def hourly_series(self, tag_path: str, start: datetime, end: datetime,
                            tz: str = "UTC") -> list[dict[str, Any]]:
        """Hourly avg/min/max/first/last/n of good values for [start, end): the roll-up where it
        exists, computed from raw samples for the hours after it (reports, long charts)."""
        if not self._tag_ids:
            await self.tags()
        tag_id = self._tag_ids.get(tag_path)
        if tag_id is None:
            return []
        async with self._conn() as conn:
            last = (await (await conn.execute(
                "SELECT max(bucket) AS b FROM samples_hourly WHERE tag_id=%s",
                (tag_id,))).fetchone())["b"]
            cut = (last + timedelta(hours=1)) if last is not None else start
            cut = min(max(cut, start), end)
            rows = await (await conn.execute(
                "SELECT bucket, avg_value, min_value, max_value, first_value, last_value, n"
                " FROM samples_hourly WHERE tag_id=%(id)s AND bucket >= %(s)s"
                " AND bucket < %(c)s"
                " UNION ALL"
                " SELECT date_bin('1 hour', ts, %(origin)s), avg(value_num), min(value_num),"
                " max(value_num),"
                " (array_agg(value_num ORDER BY ts))[1],"
                " (array_agg(value_num ORDER BY ts DESC))[1],"
                " count(*) FROM samples WHERE tag_id=%(id)s AND quality = 0"
                " AND value_num IS NOT NULL AND ts >= %(c)s AND ts < %(e)s"
                " GROUP BY 1 ORDER BY 1",
                {"id": tag_id, "s": start, "c": cut, "e": end,
                 "origin": hour_origin(tz)})).fetchall()
        return [dict(r) for r in rows]

    # --- device status ------------------------------------------------------------------------

    async def upsert_device_status(self, status: DeviceStatus) -> None:
        await self.upsert_device_statuses([status])

    async def upsert_device_statuses(self, statuses: Sequence[DeviceStatus]) -> None:
        """All changed device states in one statement (the hub flushes them every second)."""
        if not statuses:
            return
        columns = ([s.device_id for s in statuses], [str(s.state) for s in statuses],
                   [s.last_seen for s in statuses], [s.last_error for s in statuses],
                   [s.last_error_at for s in statuses])
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO device_status (device_id, state, last_seen, last_error, "
                "last_error_at, updated_at) SELECT d, s, ls, le, lea, now() FROM "
                "unnest(%s::text[], %s::text[], %s::timestamptz[], %s::text[], "
                "%s::timestamptz[]) AS u(d, s, ls, le, lea) "
                "ON CONFLICT (device_id) DO UPDATE SET state=EXCLUDED.state, "
                "last_seen=coalesce(EXCLUDED.last_seen, device_status.last_seen), "
                "last_error=EXCLUDED.last_error, last_error_at=EXCLUDED.last_error_at, "
                "updated_at=now()", columns)

    async def device_statuses(self) -> list[DeviceStatusRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT s.* FROM device_status s JOIN devices d USING (device_id) "
                "WHERE d.retired_at IS NULL ORDER BY device_id")).fetchall()
        return [DeviceStatusRecord.model_validate(r) for r in rows]

    # --- users / RBAC -------------------------------------------------------------------------

    async def count_users(self) -> int:
        async with self._conn() as conn:
            return (await (await conn.execute("SELECT count(*) AS n FROM users")).fetchone())["n"]

    async def create_user(self, username: str, display_name: str, password_hash: str, *,
                          must_change_password: bool = True, language: str = "fa",
                          auth_source: str = "local", external_dn: str | None = None
                          ) -> UserRecord:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "INSERT INTO users (username, display_name, password_hash, must_change_password,"
                " language, auth_source, external_dn) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (username, display_name, password_hash, must_change_password, language,
                 auth_source, external_dn),
            )).fetchone()
        return UserRecord.model_validate(row)

    async def get_user(self, user_id: int) -> UserRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT * FROM users WHERE user_id=%s",
                                            (user_id,))).fetchone()
        return UserRecord.model_validate(row) if row else None

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT * FROM users WHERE lower(email)=lower(%s)",
                                            (email,))).fetchone()
        return UserRecord.model_validate(row) if row else None

    async def get_user_by_username(self, username: str) -> UserRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT * FROM users WHERE username=%s",
                                            (username,))).fetchone()
        return UserRecord.model_validate(row) if row else None

    async def list_users(self) -> list[UserRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute("SELECT * FROM users ORDER BY username")).fetchall()
        return [UserRecord.model_validate(r) for r in rows]

    async def update_user(self, user_id: int, **fields: Any) -> UserRecord | None:
        unknown = set(fields) - _USER_FIELDS
        if unknown:
            raise ValueError(f"cannot update user fields {sorted(unknown)}")
        if not fields:
            return await self.get_user(user_id)
        assignments = ", ".join(f"{k}=%s" for k in fields)
        values = [Jsonb(v) if isinstance(v, list | dict) else v for v in fields.values()]
        async with self._conn() as conn:
            row = await (await conn.execute(
                # column names checked against _USER_FIELDS above; values are parameters
                f"UPDATE users SET {assignments} WHERE user_id=%s RETURNING *",  # nosec B608
                (*values, user_id))).fetchone()
        return UserRecord.model_validate(row) if row else None

    async def role_permissions(self) -> dict[str, frozenset[str]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT r.role_id, array_remove(array_agg(rp.permission_id), NULL) AS perms "
                "FROM roles r LEFT JOIN role_permissions rp USING (role_id) GROUP BY r.role_id"
            )).fetchall()
        return {r["role_id"]: frozenset(r["perms"]) for r in rows}

    async def grants(self, user_id: int) -> list[RoleGrant]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM user_roles WHERE user_id=%s ORDER BY user_role_id",
                (user_id,))).fetchall()
        return [RoleGrant.model_validate(r) for r in rows]

    async def grant_role(self, user_id: int, role_id: str, site: str | None, area: str | None,
                         granted_by: int | None, org_id: str | None = None) -> RoleGrant:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "INSERT INTO user_roles (user_id, role_id, site, area, granted_by, org_id) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (user_id, role_id, site, area, granted_by, org_id))).fetchone()
        return RoleGrant.model_validate(row)

    async def replace_roles(self, user_id: int,
                            grants: list[tuple[str, str | None, str | None]]) -> bool:
        """Make the user's grants exactly ``grants`` (directory users: the groups decide).
        Returns whether anything changed."""
        wanted = sorted(set(grants), key=lambda g: (g[0], g[1] or "", g[2] or ""))
        async with self._conn() as conn, conn.transaction():
            rows = await (await conn.execute(
                "SELECT role_id, site, area FROM user_roles WHERE user_id=%s FOR UPDATE",
                (user_id,))).fetchall()
            current = sorted({(r["role_id"], r["site"], r["area"]) for r in rows},
                             key=lambda g: (g[0], g[1] or "", g[2] or ""))
            if current == wanted:
                return False
            await conn.execute("DELETE FROM user_roles WHERE user_id=%s", (user_id,))
            for role, site, area in wanted:
                await conn.execute(
                    "INSERT INTO user_roles (user_id, role_id, site, area, granted_by) "
                    "VALUES (%s,%s,%s,%s,NULL)", (user_id, role, site, area))
        return True

    async def revoke_role(self, user_role_id: int) -> RoleGrant | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "DELETE FROM user_roles WHERE user_role_id=%s RETURNING *",
                (user_role_id,))).fetchone()
        return RoleGrant.model_validate(row) if row else None

    # --- sessions -----------------------------------------------------------------------------

    async def create_session(self, session_id: UUID, user_id: int, refresh_hash: str,
                             expires_at: datetime, client_addr: str | None,
                             user_agent: str | None, mfa: bool = False) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO sessions (session_id, user_id, refresh_hash, expires_at, "
                "client_addr, user_agent, mfa) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (session_id, user_id, refresh_hash, expires_at, client_addr, user_agent, mfa))

    async def touch_session(self, session_id: UUID, now: datetime) -> None:
        """Record activity (at most once a minute per session, to keep writes low)."""
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE sessions SET last_seen_at=%s WHERE session_id=%s "
                "AND last_seen_at < %s - interval '60 seconds'", (now, session_id, now))

    async def get_mfa(self, user_id: int) -> dict[str, Any]:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT totp_secret_enc, totp_pending_enc, totp_enabled, totp_last_step, "
                "recovery_codes, password_history FROM users WHERE user_id=%s",
                (user_id,))).fetchone()
        return dict(row) if row else {}

    async def get_session_by_hash(self, refresh_hash: str) -> SessionRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT session_id, user_id, created_at, expires_at, revoked_at, last_seen_at, mfa "
                "FROM sessions WHERE refresh_hash=%s", (refresh_hash,))).fetchone()
        return SessionRecord.model_validate(row) if row else None

    async def get_session(self, session_id: UUID) -> SessionRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT session_id, user_id, created_at, expires_at, revoked_at, last_seen_at, mfa "
                "FROM sessions WHERE session_id=%s", (session_id,))).fetchone()
        return SessionRecord.model_validate(row) if row else None

    async def revoke_session(self, session_id: UUID) -> None:
        async with self._conn() as conn:
            await conn.execute("UPDATE sessions SET revoked_at=now() WHERE session_id=%s "
                               "AND revoked_at IS NULL", (session_id,))

    async def revoke_user_sessions(self, user_id: int) -> None:
        async with self._conn() as conn:
            await conn.execute("UPDATE sessions SET revoked_at=now() WHERE user_id=%s "
                               "AND revoked_at IS NULL", (user_id,))

    async def list_sessions(self, user_id: int, now: datetime) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT session_id, created_at, expires_at, host(client_addr) AS client_addr, "
                "user_agent FROM sessions WHERE user_id=%s AND revoked_at IS NULL AND "
                "expires_at > %s ORDER BY created_at DESC", (user_id, now))).fetchall()
        return [dict(r) for r in rows]

    # --- dashboards ---------------------------------------------------------------------------

    async def list_dashboards(self, user_id: int) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT d.dashboard_id, d.name, d.shared, d.is_default, d.updated_at, "
                "d.owner_id, u.username AS owner FROM user_dashboards d JOIN users u "
                "ON u.user_id = d.owner_id WHERE d.owner_id=%s OR d.shared "
                "ORDER BY d.owner_id <> %s, d.name", (user_id, user_id))).fetchall()
        return [dict(r) for r in rows]

    async def get_dashboard(self, dashboard_id: int) -> dict[str, Any] | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT d.*, u.username AS owner FROM user_dashboards d JOIN users u "
                "ON u.user_id = d.owner_id WHERE d.dashboard_id=%s", (dashboard_id,))).fetchone()
        return dict(row) if row else None

    async def save_dashboard(self, owner_id: int, name: str, layout: dict[str, Any],
                             shared: bool, is_default: bool, dashboard_id: int | None = None
                             ) -> dict[str, Any]:
        async with self._conn() as conn, conn.transaction():
            if is_default:
                await conn.execute("UPDATE user_dashboards SET is_default=false "
                                   "WHERE owner_id=%s AND is_default", (owner_id,))
            if dashboard_id is None:
                row = await (await conn.execute(
                    "INSERT INTO user_dashboards (owner_id, name, layout, shared, is_default) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING dashboard_id",
                    (owner_id, name, Jsonb(layout), shared, is_default))).fetchone()
            else:
                row = await (await conn.execute(
                    "UPDATE user_dashboards SET name=%s, layout=%s, shared=%s, is_default=%s, "
                    "updated_at=now() WHERE dashboard_id=%s RETURNING dashboard_id",
                    (name, Jsonb(layout), shared, is_default, dashboard_id))).fetchone()
        saved = await self.get_dashboard(row["dashboard_id"])
        assert saved is not None
        return saved

    async def delete_dashboard(self, dashboard_id: int) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute("DELETE FROM user_dashboards WHERE dashboard_id=%s",
                                     (dashboard_id,))
            return cur.rowcount > 0

    # --- audit --------------------------------------------------------------------------------

    async def add_write_audit(self, *, request_id: UUID, phase: str, user_id: int,
                              username: str, client_addr: str | None, tag_path: str,
                              requested_value: object, result: str,
                              previous_value: object = None, readback_value: object = None,
                              confirmed_by: int | None = None, reason: str | None = None) -> None:
        tag_id = self._tag_ids.get(tag_path) or (await self.tags())[tag_path].tag_id
        fields = {"table": "write_audit", "request_id": str(request_id), "phase": phase,
                  "user": username, "client": client_addr, "tag": tag_path,
                  "value": requested_value, "previous": previous_value,
                  "readback": readback_value, "result": result, "reason": reason}
        try:
            async with self._conn() as conn:
                await conn.execute(
                    "INSERT INTO write_audit (request_id, phase, user_id, username, client_addr, "
                    "tag_id, tag_path, requested_value, previous_value, readback_value, "
                    "confirmed_by, result, reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (request_id, phase, user_id, username, client_addr, tag_id, tag_path,
                     encode_value(requested_value),
                     None if previous_value is None else encode_value(previous_value),
                     None if readback_value is None else encode_value(readback_value),
                     confirmed_by, result, reason))
        except Exception:
            audit_log.error("audit row NOT stored: write %s %s by %s", phase, tag_path, username,
                            extra={"audit": fields})
            raise
        audit_log.info("write %s %s=%s by %s: %s", phase, tag_path, requested_value, username,
                       result, extra={"audit": fields})

    async def list_write_audit(self, *, tag_path: str | None = None, user_id: int | None = None,
                               request_id: UUID | None = None, since: datetime | None = None,
                               limit: int = 200) -> list[WriteAuditRecord]:
        where, params = ["TRUE"], []
        for column, value in (("tag_path", tag_path), ("user_id", user_id),
                              ("request_id", request_id)):
            if value is not None:
                where.append(f"{column}=%s")
                params.append(value)
        if since is not None:
            where.append("ts >= %s")
            params.append(since)
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT id, ts, request_id, phase, user_id, username, host(client_addr) AS "
                "client_addr, tag_path, requested_value, previous_value, readback_value, "
                f"confirmed_by, result, reason FROM write_audit WHERE {' AND '.join(where)} "  # nosec B608
                "ORDER BY id DESC LIMIT %s", (*params, limit))).fetchall()
        return [WriteAuditRecord.model_validate(r) for r in rows]

    async def add_auth_audit(self, event: str, username: str, *, user_id: int | None = None,
                             actor_id: int | None = None, client_addr: str | None = None,
                             detail: dict[str, Any] | None = None) -> None:
        fields = {"table": "auth_audit", "event": event, "user": username, "user_id": user_id,
                  "actor_id": actor_id, "client": client_addr, "detail": detail or {}}
        try:
            async with self._conn() as conn:
                await conn.execute(
                    "INSERT INTO auth_audit (event, username, user_id, actor_id, client_addr, "
                    "detail) VALUES (%s,%s,%s,%s,%s,%s)",
                    (event, username, user_id, actor_id, client_addr, Jsonb(detail or {})))
        except Exception:
            audit_log.error("audit row NOT stored: %s %s", event, username,
                            extra={"audit": fields})
            raise
        level = logging.WARNING if event in _SECURITY_EVENTS else logging.INFO
        audit_log.log(level, "%s %s%s", event, username,
                      f" from {client_addr}" if client_addr else "", extra={"audit": fields})

    async def list_auth_audit(self, *, event: str | None = None, limit: int = 200
                              ) -> list[AuthAuditRecord]:
        sql = ("SELECT id, ts, event, username, user_id, actor_id, host(client_addr) AS "
               "client_addr, detail FROM auth_audit")
        params: tuple = ()
        if event:
            sql += " WHERE event=%s"
            params = (event,)
        async with self._conn() as conn:
            rows = await (await conn.execute(sql + " ORDER BY id DESC LIMIT %s",
                                             (*params, limit))).fetchall()
        return [AuthAuditRecord.model_validate(r) for r in rows]

    # --- pending writes -----------------------------------------------------------------------

    async def add_pending(self, *, request_id: UUID, tag_path: str, requested_by: int,
                          value: object, reason: str | None, expires_at: datetime) -> PendingWrite:
        tag_id = self._tag_ids.get(tag_path) or (await self.tags())[tag_path].tag_id
        async with self._conn() as conn:
            row = await (await conn.execute(
                "INSERT INTO pending_writes (request_id, tag_id, tag_path, requested_by, value, "
                "reason, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (request_id, tag_id, tag_path, requested_by, Jsonb(value), reason, expires_at),
            )).fetchone()
        return PendingWrite.model_validate(row)

    async def get_pending(self, request_id: UUID) -> PendingWrite | None:
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT * FROM pending_writes WHERE request_id=%s",
                                            (request_id,))).fetchone()
        return PendingWrite.model_validate(row) if row else None

    async def list_pending(self) -> list[PendingWrite]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM pending_writes WHERE status='pending' ORDER BY created_at"
            )).fetchall()
        return [PendingWrite.model_validate(r) for r in rows]

    async def decide_pending(self, request_id: UUID, status: str, decided_by: int | None
                             ) -> PendingWrite | None:
        """Move a pending write to confirmed/rejected/expired; None if it was not pending."""
        async with self._conn() as conn:
            row = await (await conn.execute(
                "UPDATE pending_writes SET status=%s, decided_by=%s, decided_at=now() "
                "WHERE request_id=%s AND status='pending' RETURNING *",
                (status, decided_by, request_id))).fetchone()
        return PendingWrite.model_validate(row) if row else None

    async def expire_pending(self, now: datetime) -> list[PendingWrite]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "UPDATE pending_writes SET status='expired', decided_at=now() "
                "WHERE status='pending' AND expires_at <= %s RETURNING *", (now,))).fetchall()
        return [PendingWrite.model_validate(r) for r in rows]

    # --- alarms -------------------------------------------------------------------------------

    _ALARM_SELECT = ("SELECT a.alarm_id, a.source, p.tag_path, a.device_id, a.kind, a.severity, "
                     "a.message, a.value, a.raised_at, a.cleared_at, a.acked_at, a.acked_by, "
                     "a.ack_comment FROM alarm_events a LEFT JOIN tag_paths p USING (tag_id)")

    async def open_alarm(self, *, source: str, kind: str, severity: str, message: str,
                         value: object, raised_at: datetime, tag_path: str | None = None,
                         device_id: str | None = None) -> AlarmRecord:
        tag_id = None
        if tag_path is not None:
            tag_id = self._tag_ids.get(tag_path) or (await self.tags())[tag_path].tag_id
        async with self._conn() as conn:
            row = await (await conn.execute(
                "INSERT INTO alarm_events (source, tag_id, device_id, kind, severity, message, "
                "value, raised_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING alarm_id",
                (source, tag_id, device_id, kind, severity, message,
                 None if value is None else encode_value(value), raised_at))).fetchone()
        alarm = await self._alarm(alarm_id=row["alarm_id"])
        assert alarm is not None
        return alarm

    async def _alarm(self, *, alarm_id: int) -> AlarmRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute(self._ALARM_SELECT + " WHERE a.alarm_id=%s",
                                            (alarm_id,))).fetchone()
        return AlarmRecord.model_validate(row) if row else None

    async def clear_alarm(self, source: str, cleared_at: datetime) -> AlarmRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "UPDATE alarm_events SET cleared_at=%s WHERE source=%s AND cleared_at IS NULL "
                "RETURNING alarm_id", (cleared_at, source))).fetchone()
        return await self._alarm(alarm_id=row["alarm_id"]) if row else None

    async def ack_alarm(self, alarm_id: int, user_id: int, comment: str | None
                        ) -> AlarmRecord | None:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "UPDATE alarm_events SET acked_at=now(), acked_by=%s, ack_comment=%s "
                "WHERE alarm_id=%s AND acked_at IS NULL RETURNING alarm_id",
                (user_id, comment, alarm_id))).fetchone()
        return await self._alarm(alarm_id=row["alarm_id"]) if row else None

    async def get_alarm(self, alarm_id: int) -> AlarmRecord | None:
        return await self._alarm(alarm_id=alarm_id)

    async def active_alarms(self) -> list[AlarmRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                self._ALARM_SELECT + " WHERE a.cleared_at IS NULL OR a.acked_at IS NULL "
                "ORDER BY a.raised_at DESC")).fetchall()
        return [AlarmRecord.model_validate(r) for r in rows]

    async def open_alarm_sources(self) -> dict[str, AlarmRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                self._ALARM_SELECT + " WHERE a.cleared_at IS NULL")).fetchall()
        return {r["source"]: AlarmRecord.model_validate(r) for r in rows}

    async def alarm_history(self, since: datetime | None = None, limit: int = 500
                            ) -> list[AlarmRecord]:
        sql, params = self._ALARM_SELECT, []
        if since is not None:
            sql += " WHERE a.raised_at >= %s"
            params.append(since)
        async with self._conn() as conn:
            rows = await (await conn.execute(sql + " ORDER BY a.raised_at DESC LIMIT %s",
                                             (*params, limit))).fetchall()
        return [AlarmRecord.model_validate(r) for r in rows]

    # --- schedules ------------------------------------------------------------------------------

    _SCHEDULE_SELECT = ("SELECT s.*, u.username AS owner FROM schedules s "
                        "JOIN users u ON u.user_id = s.owner_id")

    async def list_schedules(self) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(self._SCHEDULE_SELECT + " ORDER BY s.name")
                          ).fetchall()
        return [dict(r) for r in rows]

    async def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        async with self._conn() as conn:
            row = await (await conn.execute(self._SCHEDULE_SELECT + " WHERE s.schedule_id=%s",
                                            (schedule_id,))).fetchone()
        return dict(row) if row else None

    async def save_schedule(self, *, name: str, description: str | None, owner_id: int,
                            enabled: bool, cron: str, timezone: str, steps: list[Any],
                            next_run_at: datetime | None, schedule_id: int | None = None
                            ) -> dict[str, Any]:
        async with self._conn() as conn:
            if schedule_id is None:
                row = await (await conn.execute(
                    "INSERT INTO schedules (name, description, owner_id, enabled, cron, timezone,"
                    " steps, next_run_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "RETURNING schedule_id",
                    (name, description, owner_id, enabled, cron, timezone, Jsonb(steps),
                     next_run_at))).fetchone()
            else:
                row = await (await conn.execute(
                    "UPDATE schedules SET name=%s, description=%s, enabled=%s, cron=%s, "
                    "timezone=%s, steps=%s, next_run_at=%s, updated_at=now() "
                    "WHERE schedule_id=%s RETURNING schedule_id",
                    (name, description, enabled, cron, timezone, Jsonb(steps), next_run_at,
                     schedule_id))).fetchone()
        saved = await self.get_schedule(row["schedule_id"])
        assert saved is not None
        return saved

    async def delete_schedule(self, schedule_id: int) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute("DELETE FROM schedules WHERE schedule_id=%s", (schedule_id,))
            return cur.rowcount > 0

    async def set_schedule_times(self, schedule_id: int, last_run_at: datetime | None,
                                 next_run_at: datetime | None) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE schedules SET last_run_at=coalesce(%s, last_run_at), next_run_at=%s "
                "WHERE schedule_id=%s", (last_run_at, next_run_at, schedule_id))

    async def start_schedule_run(self, schedule_id: int, trigger: str) -> int:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "INSERT INTO schedule_runs (schedule_id, trigger) VALUES (%s,%s) "
                "RETURNING run_id", (schedule_id, trigger))).fetchone()
        return int(row["run_id"])

    async def finish_schedule_run(self, run_id: int, status: str, detail: list[Any]) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE schedule_runs SET finished_at=now(), status=%s, detail=%s "
                "WHERE run_id=%s", (status, Jsonb(detail), run_id))

    async def schedule_runs(self, schedule_id: int, limit: int = 50) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM schedule_runs WHERE schedule_id=%s ORDER BY run_id DESC LIMIT %s",
                (schedule_id, limit))).fetchall()
        return [dict(r) for r in rows]

    # --- notification log ----------------------------------------------------------------------

    async def add_notification_log(self, *, channel: str, rule: str, event: str, source: str,
                                   status: str, attempt: int = 1,
                                   error: str | None = None) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO notification_log (channel, rule, event, source, status, attempt, "
                "error) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (channel, rule, event, source, status, attempt, error))

    async def notification_log(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM notification_log ORDER BY id DESC LIMIT %s", (limit,))).fetchall()
        return [dict(r) for r in rows]

    # --- alarm shelving ------------------------------------------------------------------------

    async def shelve(self, source: str, user_id: int, reason: str, until: datetime) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO alarm_shelves (source, shelved_by, reason, until) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (source) DO UPDATE SET "
                "shelved_by=EXCLUDED.shelved_by, reason=EXCLUDED.reason, "
                "created_at=now(), until=EXCLUDED.until", (source, user_id, reason, until))

    async def unshelve(self, source: str) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute("DELETE FROM alarm_shelves WHERE source=%s", (source,))
            return cur.rowcount > 0

    async def shelves(self, now: datetime) -> dict[str, dict[str, Any]]:
        """Active shelves {source: {...}}; expired ones are removed on the way."""
        async with self._conn() as conn:
            await conn.execute("DELETE FROM alarm_shelves WHERE until <= %s", (now,))
            rows = await (await conn.execute(
                "SELECT s.source, s.reason, s.created_at, s.until, u.username AS shelved_by "
                "FROM alarm_shelves s JOIN users u ON u.user_id = s.shelved_by")).fetchall()
        return {r["source"]: dict(r) for r in rows}

    # --- high availability -----------------------------------------------------------------------

    async def heartbeat_node(self, node_id: str, hostname: str, role: str, version: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO hub_nodes (node_id, hostname, role, version) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (node_id) DO UPDATE SET hostname=EXCLUDED.hostname, "
                "role=EXCLUDED.role, version=EXCLUDED.version, last_heartbeat=now()",
                (node_id, hostname, role, version))

    async def nodes(self) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT *, now() - last_heartbeat < interval '30 seconds' AS alive "
                "FROM hub_nodes ORDER BY node_id")).fetchall()
        return [dict(r) for r in rows]

    # --- audit chain ---------------------------------------------------------------------------

    async def audit_verify(self, table: str) -> dict[str, Any]:
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT * FROM audit_verify(%s)", (table,))
                         ).fetchone()
        return {"table": table, **dict(row), "ok": row["first_bad_id"] is None}

    # --- maintenance --------------------------------------------------------------------------

    async def has_timescale(self) -> bool:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT count(*) AS n FROM pg_extension WHERE extname = 'timescaledb'")).fetchone()
        return bool(row["n"])

    async def rollup_hourly(self, start: datetime, end: datetime, tz: str = "UTC") -> int:
        """(Re)compute hourly aggregates of good numeric samples for [start, end). Hours follow
        the plant's time zone (Tehran hours start at :30 UTC), so local days split cleanly."""
        async with self._conn() as conn:
            cur = await conn.execute(
                "INSERT INTO samples_hourly (tag_id, bucket, avg_value, min_value, max_value, "
                "first_value, last_value, n) "
                "SELECT tag_id, date_bin('1 hour', ts, %(origin)s) AS bucket, avg(value_num), "
                "min(value_num), max(value_num), (array_agg(value_num ORDER BY ts))[1], "
                "(array_agg(value_num ORDER BY ts DESC))[1], count(*) FROM samples "
                "WHERE value_num IS NOT NULL AND quality = 0 AND ts >= %(s)s AND ts < %(e)s "
                "GROUP BY tag_id, bucket ON CONFLICT (tag_id, bucket) DO UPDATE SET "
                "avg_value=EXCLUDED.avg_value, min_value=EXCLUDED.min_value, "
                "max_value=EXCLUDED.max_value, first_value=EXCLUDED.first_value, "
                "last_value=EXCLUDED.last_value, n=EXCLUDED.n",
                {"origin": hour_origin(tz), "s": start, "e": end})
            return cur.rowcount

    async def last_rollup(self) -> datetime | None:
        async with self._conn() as conn:
            cur = await conn.execute("SELECT max(bucket) AS b FROM samples_hourly")
            row = await cur.fetchone()
        return row["b"]

    async def oldest_sample(self) -> datetime | None:
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT min(ts) AS ts FROM samples")).fetchone()
        return row["ts"]

    async def delete_raw_before(self, cutoff: datetime, timescale: bool,
                                 batch: int = 50_000) -> int:
        async with self._conn() as conn:
            if timescale:
                await conn.execute("SELECT drop_chunks('samples', older_than => %s)", (cutoff,))
                return -1  # chunks dropped; row count unknown
            total = 0
            while True:
                cur = await conn.execute(
                    "DELETE FROM samples WHERE ctid IN (SELECT ctid FROM samples WHERE ts < %s "
                    "LIMIT %s)", (cutoff, batch))
                total += cur.rowcount
                if cur.rowcount < batch:
                    return total

    async def delete_hourly_before(self, cutoff: datetime) -> int:
        async with self._conn() as conn:
            cur = await conn.execute("DELETE FROM samples_hourly WHERE bucket < %s", (cutoff,))
            return cur.rowcount

    async def add_event(self, kind: str, node_id: str | None,
                         detail: dict[str, Any]) -> None:
        async with self._conn() as conn:
            await conn.execute("INSERT INTO system_events (kind, node_id, detail) "
                               "VALUES (%s,%s,%s)",
                               (kind, node_id, Jsonb(detail)))

    async def events(self, kind: str | None, limit: int) -> list[dict[str, Any]]:
        sql, params = "SELECT id, ts, kind, node_id, detail FROM system_events", []
        if kind:
            sql += " WHERE kind = %s"
            params.append(kind)
        async with self._conn() as conn:
            return list(await (await conn.execute(sql + " ORDER BY id DESC LIMIT %s",
                                                  (*params, limit))).fetchall())


    async def ping(self) -> bool:
        async with self._conn() as conn:
            await conn.execute("SELECT 1")
        return True

    async def server_time(self) -> datetime:
        """The database server's clock (for the clock-skew health check)."""
        async with self._conn() as conn:
            row = await (await conn.execute("SELECT clock_timestamp() AS now")).fetchone()
        return row["now"]  # type: ignore[index]

    # --- building model (mirror of config/building.yaml) ----------------------------------------

    async def sync_building(self, building: BuildingFile) -> None:
        """Upsert organizations, locations, systems, equipment and points; retire what is gone.
        Points whose tag is not in the database (yet) are skipped."""
        b = building
        async with self._conn() as conn, conn.transaction():
            await conn.execute("SET CONSTRAINTS ALL DEFERRED")
            for org in b.organizations:
                await conn.execute(
                    "INSERT INTO organizations (org_id, name) VALUES (%s,%s) ON CONFLICT (org_id)"
                    " DO UPDATE SET name=EXCLUDED.name, updated_at=now(), retired_at=NULL"
                    " WHERE organizations.source = 'config'",
                    (org.id, org.name))
            await conn.execute("DELETE FROM organization_sites")
            for org in b.organizations:
                for site in org.sites:
                    await conn.execute("INSERT INTO organization_sites (site, org_id) VALUES "
                                       "(%s,%s) ON CONFLICT (site) DO NOTHING", (site, org.id))
            for loc in b.locations:
                await conn.execute(
                    "INSERT INTO locations (location_id, kind, name, parent_id, site, level, "
                    "area_m2, description, haystack, brick_class) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (location_id) DO UPDATE "
                    "SET kind=EXCLUDED.kind, name=EXCLUDED.name, parent_id=EXCLUDED.parent_id, "
                    "site=EXCLUDED.site, level=EXCLUDED.level, area_m2=EXCLUDED.area_m2, "
                    "description=EXCLUDED.description, haystack=EXCLUDED.haystack, "
                    "brick_class=EXCLUDED.brick_class, updated_at=now(), retired_at=NULL",
                    (loc.id, str(loc.kind), loc.name, loc.parent, loc.site, loc.level,
                     loc.area_m2, loc.description, list(loc.haystack), loc.brick_class))
            for s in b.systems:
                await conn.execute(
                    "INSERT INTO building_systems (system_id, kind, name, building_id, "
                    "description, haystack, brick_class) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (system_id) DO UPDATE SET kind=EXCLUDED.kind, "
                    "name=EXCLUDED.name, building_id=EXCLUDED.building_id, "
                    "description=EXCLUDED.description, haystack=EXCLUDED.haystack, "
                    "brick_class=EXCLUDED.brick_class, updated_at=now(), retired_at=NULL",
                    (s.id, str(s.kind), s.name, s.building, s.description, list(s.haystack),
                     s.brick_class))
            await conn.execute("DELETE FROM equipment_points")
            for e in b.equipment:
                await conn.execute(
                    "INSERT INTO equipment (equipment_id, name, type, location_id, system_id, "
                    "device_id, manufacturer, model, serial_number, status, installed_on, "
                    "warranty_until, description, documents, haystack, brick_class) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (equipment_id) "
                    "DO UPDATE SET name=EXCLUDED.name, type=EXCLUDED.type, "
                    "location_id=EXCLUDED.location_id, system_id=EXCLUDED.system_id, "
                    "device_id=EXCLUDED.device_id, manufacturer=EXCLUDED.manufacturer, "
                    "model=EXCLUDED.model, serial_number=EXCLUDED.serial_number, "
                    "status=EXCLUDED.status, installed_on=EXCLUDED.installed_on, "
                    "warranty_until=EXCLUDED.warranty_until, description=EXCLUDED.description, "
                    "documents=EXCLUDED.documents, haystack=EXCLUDED.haystack, "
                    "brick_class=EXCLUDED.brick_class, updated_at=now(), retired_at=NULL",
                    (e.id, e.name, e.type, e.location, e.system, e.device, e.manufacturer,
                     e.model, e.serial_number, str(e.status), e.installed_on, e.warranty_until,
                     e.description, Jsonb([d.model_dump() for d in e.documents]),
                     list(e.haystack), e.brick_class))
                for p in e.points:
                    await conn.execute(
                        "INSERT INTO equipment_points (equipment_id, role, tag_id, description, "
                        "haystack, brick_class) SELECT %s, %s, tag_id, %s, %s, %s FROM tag_paths "
                        "WHERE tag_path=%s",
                        (e.id, p.role, p.description, list(p.haystack), p.brick_class, p.tag))
            await conn.execute(      # platform organizations are not in building.yaml
                "UPDATE organizations SET retired_at=now() WHERE retired_at IS NULL "
                "AND source = 'config' AND NOT (org_id = ANY(%s))",
                ([o.id for o in b.organizations],))
            for table, key, ids in (
                    ("locations", "location_id", [loc.id for loc in b.locations]),
                    ("building_systems", "system_id", [s.id for s in b.systems]),
                    ("equipment", "equipment_id", [e.id for e in b.equipment])):
                await conn.execute(  # table/key are the constants above
                    f"UPDATE {table} SET retired_at=now() WHERE retired_at IS NULL "  # nosec B608
                    f"AND NOT ({key} = ANY(%s))", (ids,))

    async def add_maintenance_record(self, *, equipment_id: str, performed_at: datetime,
                                     kind: str, description: str, performed_by: str | None,
                                     work_order: str | None, created_by: int
                                     ) -> MaintenanceRecord:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "INSERT INTO maintenance_records (equipment_id, performed_at, kind, description, "
                "performed_by, work_order, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING *", (equipment_id, performed_at, kind, description, performed_by,
                                work_order, created_by))).fetchone()
        return MaintenanceRecord.model_validate(row)

    async def maintenance_records(self, equipment_id: str, limit: int = 200
                                  ) -> list[MaintenanceRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                "SELECT m.*, u.username AS created_by_name FROM maintenance_records m "
                "JOIN users u ON u.user_id = m.created_by WHERE m.equipment_id=%s "
                "ORDER BY m.performed_at DESC, m.record_id DESC LIMIT %s",
                (equipment_id, limit))).fetchall()
        return [MaintenanceRecord.model_validate(r) for r in rows]

    async def equipment_known(self, equipment_id: str) -> bool:
        async with self._conn() as conn:
            row = await (await conn.execute(
                "SELECT 1 AS ok FROM equipment WHERE equipment_id=%s", (equipment_id,))).fetchone()
        return row is not None

    # --- collection errors --------------------------------------------------------------------

    async def add_collection_errors(self, errors: Sequence[tuple[datetime, str, str, str]]
                                    ) -> None:
        """(ts, device_id, state, message) rows; devices unknown to the database are skipped."""
        if not errors:
            return
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO collection_errors (ts, device_id, state, message) "
                "SELECT e.ts, e.device_id, e.state, left(e.message, 1000) FROM "
                "unnest(%s::timestamptz[], %s::text[], %s::text[], %s::text[]) "
                "AS e(ts, device_id, state, message) JOIN devices d USING (device_id)",
                tuple(list(c) for c in zip(*errors, strict=True)))

    async def collection_errors(self, device_id: str | None = None, limit: int = 200
                                ) -> list[CollectionError]:
        sql, params = "SELECT * FROM collection_errors", []
        if device_id:
            sql += " WHERE device_id=%s"
            params.append(device_id)
        async with self._conn() as conn:
            rows = await (await conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT %s",
                                             (*params, limit))).fetchall()
        return [CollectionError.model_validate(r) for r in rows]

    async def delete_collection_errors_before(self, cutoff: datetime) -> int:
        async with self._conn() as conn:
            cur = await conn.execute("DELETE FROM collection_errors WHERE ts < %s", (cutoff,))
            return cur.rowcount

