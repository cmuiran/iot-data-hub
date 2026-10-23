-- 0003_operations: Modbus RTU, hourly roll-up, shelving, dashboards, notifications, schedules,
-- high availability and system events.

-- ---------------------------------------------------------------------------------------------
-- New protocol and audit events
-- ---------------------------------------------------------------------------------------------

ALTER TABLE devices DROP CONSTRAINT devices_protocol_check;
ALTER TABLE devices ADD CONSTRAINT devices_protocol_check
    CHECK (protocol IN ('modbus_tcp', 'modbus_rtu', 'mqtt', 'opcua'));

ALTER TABLE auth_audit DROP CONSTRAINT auth_audit_event_check;
ALTER TABLE auth_audit ADD CONSTRAINT auth_audit_event_check CHECK (event IN (
    'login_ok', 'login_failed', 'login_throttled', 'logout', 'locked', 'unlocked',
    'password_changed', 'user_created', 'user_updated', 'user_disabled', 'role_granted',
    'role_revoked', 'session_revoked', 'alarm_acked', 'alarm_shelved', 'alarm_unshelved',
    'write_confirmed', 'write_rejected', 'config_changed', 'schedule_created',
    'schedule_updated', 'schedule_deleted', 'opcua_login'));

-- ---------------------------------------------------------------------------------------------
-- Hourly roll-up (filled by hub/maintenance.py; used for long ranges and reports)
-- ---------------------------------------------------------------------------------------------

CREATE TABLE samples_hourly (
    tag_id       integer NOT NULL REFERENCES tags (tag_id),
    bucket       timestamptz NOT NULL,
    avg_value    double precision,
    min_value    double precision,
    max_value    double precision,
    first_value  double precision,   -- first good value in the hour (counters: delta = last - first)
    last_value   double precision,
    n            integer NOT NULL,
    PRIMARY KEY (tag_id, bucket)
);
CREATE INDEX samples_hourly_bucket_idx ON samples_hourly (bucket);

-- ---------------------------------------------------------------------------------------------
-- Alarm shelving: a shelved source raises no notifications and is marked in the alarm list
-- ---------------------------------------------------------------------------------------------

CREATE TABLE alarm_shelves (
    source      text PRIMARY KEY,
    shelved_by  integer NOT NULL REFERENCES users (user_id),
    reason      text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    until       timestamptz NOT NULL,
    CHECK (until > created_at)
);

-- ---------------------------------------------------------------------------------------------
-- Dashboards (layout is opaque JSON owned by the frontend)
-- ---------------------------------------------------------------------------------------------

CREATE TABLE user_dashboards (
    dashboard_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id      integer NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    layout        jsonb NOT NULL DEFAULT '{}'::jsonb,
    shared        boolean NOT NULL DEFAULT false,
    is_default    boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name)
);
CREATE UNIQUE INDEX user_dashboards_one_default_uq ON user_dashboards (owner_id) WHERE is_default;

-- ---------------------------------------------------------------------------------------------
-- Notifications sent for alarms (one row per attempt)
-- ---------------------------------------------------------------------------------------------

CREATE TABLE notification_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    channel     text NOT NULL,
    rule        text NOT NULL,
    event       text NOT NULL CHECK (event IN ('raised', 'cleared', 'test')),
    source      text NOT NULL,
    status      text NOT NULL CHECK (status IN ('sent', 'failed', 'throttled', 'suppressed')),
    attempt     smallint NOT NULL DEFAULT 1,
    error       text
);
CREATE INDEX notification_log_ts_idx ON notification_log (ts DESC);

-- ---------------------------------------------------------------------------------------------
-- Scheduled commands: steps run through the control-safety layer as the owner
-- ---------------------------------------------------------------------------------------------

CREATE TABLE schedules (
    schedule_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
    description  text,
    owner_id     integer NOT NULL REFERENCES users (user_id),
    enabled      boolean NOT NULL DEFAULT true,
    cron         text NOT NULL,              -- "m h dom mon dow" in the schedule's time zone
    timezone     text NOT NULL DEFAULT 'Asia/Tehran',
    steps        jsonb NOT NULL,             -- see backend/schedules.py
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    last_run_at  timestamptz,
    next_run_at  timestamptz
);

CREATE TABLE schedule_runs (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    schedule_id  integer NOT NULL REFERENCES schedules (schedule_id) ON DELETE CASCADE,
    trigger      text NOT NULL CHECK (trigger IN ('timer', 'manual')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'ok', 'failed', 'skipped')),
    detail       jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX schedule_runs_schedule_idx ON schedule_runs (schedule_id, started_at DESC);

-- ---------------------------------------------------------------------------------------------
-- High availability (active/standby via a PostgreSQL advisory lock) and system events
-- ---------------------------------------------------------------------------------------------

CREATE TABLE hub_nodes (
    node_id        text PRIMARY KEY,
    hostname       text NOT NULL,
    role           text NOT NULL CHECK (role IN ('active', 'standby', 'stopped')),
    started_at     timestamptz NOT NULL DEFAULT now(),
    last_heartbeat timestamptz NOT NULL DEFAULT now(),
    version        text NOT NULL
);

CREATE TABLE system_events (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    kind    text NOT NULL,                   -- backup_ok, backup_failed, retention, failover, ...
    node_id text,
    detail  jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX system_events_ts_idx ON system_events (ts DESC);
