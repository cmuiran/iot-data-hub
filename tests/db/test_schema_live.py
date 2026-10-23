"""Run db/migrations against a real PostgreSQL and exercise the schema's constraints.

Uses the ``database_url`` fixture (tests/conftest.py): pgserver's PostgreSQL 16 by default, or
HUB_TEST_DATABASE_URL (e.g. a server with TimescaleDB; then the TimescaleDB tests run too):

    $env:HUB_TEST_DATABASE_URL = "postgresql://postgres:<password>@localhost:5432/postgres"
    .venv/Scripts/python.exe -m pytest -q tests/db -rs
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import errors  # noqa: E402

pytestmark = pytest.mark.postgres


@pytest.fixture
def db(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=True) as conn:
        _seed(conn)
        yield conn


def _has_timescale(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'"
                        ).fetchone()[0] == 1


def _seed(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO devices (device_id, name, site, area, protocol, enabled) "
        "VALUES ('meter_01', 'Meter', 'kish', 'gate', 'modbus_tcp', true)"
    )
    conn.execute(
        "INSERT INTO tags (device_id, name, data_type, unit) "
        "VALUES ('meter_01', 'voltage_l1', 'float32', 'V')"
    )
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash) "
        "VALUES ('op.one', 'Operator', 'x')"
    )


def _one(conn: psycopg.Connection, sql: str, *params: object) -> object:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _tag_id(conn: psycopg.Connection) -> int:
    return int(_one(conn, "SELECT tag_id FROM tags WHERE name = 'voltage_l1'"))


def _rejects(conn: psycopg.Connection, sql: str, exc: type[Exception], *params: object) -> None:
    with pytest.raises(exc), conn.transaction():
        conn.execute(sql, params)


# --- structure ------------------------------------------------------------------------------


def test_tables_exist(db: psycopg.Connection) -> None:
    tables = {r[0] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )}
    assert {"devices", "tags", "samples", "tag_latest", "device_status", "users", "roles",
            "permissions", "role_permissions", "user_roles", "write_audit",
            "auth_audit", "sessions", "pending_writes", "alarm_events",
            "schema_migrations", "samples_hourly", "alarm_shelves", "user_dashboards",
            "notification_log", "schedules", "schedule_runs", "hub_nodes",
            "system_events"} <= tables


def test_samples_is_a_hypertable(db: psycopg.Connection) -> None:
    if not _has_timescale(db):
        pytest.skip("TimescaleDB not installed on this server")
    assert _one(db, "SELECT count(*) FROM timescaledb_information.hypertables "
                    "WHERE hypertable_name = 'samples'") == 1


def test_compression_policy(db: psycopg.Connection) -> None:
    if not _has_timescale(db):
        pytest.skip("TimescaleDB not installed on this server")
    procs = {r[0] for r in db.execute("SELECT proc_name FROM timescaledb_information.jobs")}
    assert "policy_compression" in procs


def test_tag_paths_view(db: psycopg.Connection) -> None:
    assert _one(db, "SELECT tag_path FROM tag_paths") == "kish.gate.meter_01.voltage_l1"


# --- tag rules (mirror hub/models.py) ------------------------------------------------------


@pytest.mark.parametrize(
    "columns,values",
    [
        ("data_type", "'int128'"),                                   # unknown type
        ("data_type, access", "'uint16', 'read_write'"),             # writable without range
        ("data_type, min_value", "'bool', 0"),                       # bool with range
        ("data_type, access", "'string', 'read_write'"),             # writable string
        ("data_type, min_value, max_value", "'uint16', 10, 10"),     # min not below max
        ("data_type, scale", "'uint16', 0"),                         # zero scale
    ],
)
def test_invalid_tags_rejected(db: psycopg.Connection, columns: str, values: str) -> None:
    _rejects(db, f"INSERT INTO tags (device_id, name, {columns}) "
                 f"VALUES ('meter_01', 'bad_tag', {values})", errors.CheckViolation)


def test_writable_tag_with_range_accepted(db: psycopg.Connection) -> None:
    with db.transaction(force_rollback=True):
        db.execute("INSERT INTO tags (device_id, name, data_type, access, min_value, max_value) "
                   "VALUES ('meter_01', 'demand_limit', 'uint16', 'read_write', 0, 500)")


def test_bad_names_rejected(db: psycopg.Connection) -> None:
    _rejects(db, "INSERT INTO devices (device_id, name, site, area, protocol) "
                 "VALUES ('Meter-02', 'x', 'kish', 'gate', 'modbus_tcp')", errors.CheckViolation)


def test_device_with_history_cannot_be_deleted(db: psycopg.Connection) -> None:
    _rejects(db, "DELETE FROM devices WHERE device_id = 'meter_01'", errors.ForeignKeyViolation)


# --- samples --------------------------------------------------------------------------------


def test_duplicate_sample_is_ignored_with_on_conflict(db: psycopg.Connection) -> None:
    tag = _tag_id(db)
    sql = ("INSERT INTO samples (tag_id, ts, value_num) VALUES (%s, '2026-10-01T00:00Z', 230.1) "
           "ON CONFLICT (tag_id, ts) DO NOTHING")
    db.execute(sql, (tag,))
    db.execute(sql, (tag,))
    assert _one(db, "SELECT count(*) FROM samples WHERE tag_id = %s", tag) == 1
    _rejects(db, "INSERT INTO samples (tag_id, ts, value_num) "
                 "VALUES (%s, '2026-10-01T00:00Z', 1)", errors.UniqueViolation, tag)


def test_sample_with_two_values_rejected(db: psycopg.Connection) -> None:
    _rejects(db, "INSERT INTO samples (tag_id, ts, value_num, value_bool) "
                 "VALUES (%s, now(), 1, true)", errors.CheckViolation, _tag_id(db))


def test_bad_quality_rejected(db: psycopg.Connection) -> None:
    _rejects(db, "INSERT INTO samples (tag_id, ts, value_num, quality) VALUES (%s, now(), 1, 3)",
             errors.CheckViolation, _tag_id(db))


def test_bad_sample_without_value_accepted(db: psycopg.Connection) -> None:
    with db.transaction(force_rollback=True):
        db.execute("INSERT INTO samples (tag_id, ts, quality) VALUES (%s, now(), 2)",
                   (_tag_id(db),))


# --- RBAC -----------------------------------------------------------------------------------


def _can(db: psycopg.Connection, role: str, permission: str) -> bool:
    return bool(_one(db, "SELECT count(*) FROM role_permissions "
                         "WHERE role_id = %s AND permission_id = %s", role, permission))


def test_seeded_roles_follow_least_privilege(db: psycopg.Connection) -> None:
    assert _can(db, "operator", "tag.write")
    assert not _can(db, "viewer", "tag.write")
    assert not _can(db, "operator", "user.manage")
    assert _can(db, "admin", "user.manage")


def test_role_scope_area_needs_site(db: psycopg.Connection) -> None:
    user = _one(db, "SELECT user_id FROM users WHERE username = 'op.one'")
    _rejects(db, "INSERT INTO user_roles (user_id, role_id, area) VALUES (%s, 'operator', 'gate')",
             errors.CheckViolation, user)


def test_duplicate_role_grant_rejected(db: psycopg.Connection) -> None:
    user = _one(db, "SELECT user_id FROM users WHERE username = 'op.one'")
    with db.transaction(force_rollback=True):
        db.execute("INSERT INTO user_roles (user_id, role_id, site) VALUES (%s, 'viewer', 'kish')",
                   (user,))
        _rejects(db, "INSERT INTO user_roles (user_id, role_id, site) "
                     "VALUES (%s, 'viewer', 'kish')", errors.UniqueViolation, user)


# --- audit ----------------------------------------------------------------------------------


def _audit(db: psycopg.Connection, request: uuid.UUID, phase: str, result: str) -> None:
    user = _one(db, "SELECT user_id FROM users WHERE username = 'op.one'")
    db.execute(
        "INSERT INTO write_audit (request_id, phase, user_id, username, tag_id, tag_path, "
        "requested_value, result) VALUES (%s, %s, %s, 'op.one', %s, "
        "'kish.gate.meter_01.voltage_l1', '1', %s)",
        (request, phase, user, _tag_id(db), result),
    )


def test_write_audit_request_and_outcome(db: psycopg.Connection) -> None:
    request = uuid.uuid4()
    _audit(db, request, "requested", "requested")
    _audit(db, request, "outcome", "applied")
    assert _one(db, "SELECT count(*) FROM write_audit WHERE request_id = %s", request) == 2
    with pytest.raises(errors.UniqueViolation), db.transaction():
        _audit(db, request, "outcome", "failed")


def test_write_audit_phase_must_match_result(db: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation), db.transaction():
        _audit(db, uuid.uuid4(), "outcome", "requested")


@pytest.mark.parametrize("table", ["write_audit", "auth_audit"])
@pytest.mark.parametrize("statement", ["UPDATE {t} SET ts = now()", "DELETE FROM {t}",
                                       "TRUNCATE {t}"])
def test_audit_tables_are_append_only(db: psycopg.Connection, table: str, statement: str) -> None:
    _rejects(db, statement.format(t=table), errors.RaiseException)


# --- migrations, pending writes, alarms -------------------------------------------------------


def test_migrations_are_recorded_and_idempotent(db: psycopg.Connection, database_url: str) -> None:
    from hub.storage.migrate import migrate

    versions = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
    assert "0001_core" in versions
    assert migrate(database_url) == []  # nothing left to apply


def test_nobody_confirms_their_own_write(db: psycopg.Connection) -> None:
    user = _one(db, "SELECT user_id FROM users WHERE username = 'op.one'")
    _rejects(db, "INSERT INTO pending_writes (request_id, tag_id, tag_path, requested_by, value, "
                 "expires_at, decided_by) VALUES (%s, %s, 'p', %s, '1', now(), %s)",
             errors.CheckViolation, uuid.uuid4(), _tag_id(db), user, user)


def test_one_open_alarm_per_source(db: psycopg.Connection) -> None:
    sql = ("INSERT INTO alarm_events (source, kind, severity, message, raised_at) "
           "VALUES ('kish.gate.meter_01.voltage_l1', 'hi', 'high', 'm', now())")
    db.execute(sql)
    _rejects(db, sql, errors.UniqueViolation)
    db.execute("UPDATE alarm_events SET cleared_at = now()")
    db.execute(sql)  # a new alarm after the old one cleared is fine
