-- 0001_core: Industrial IoT Data Hub schema on plain PostgreSQL (14+).
-- Applied by hub.storage.migrate inside one transaction. TimescaleDB is optional (0002).
--
-- Sources of truth
--   * Device/tag definitions: config/devices/*.yaml (validated by hub/models.py). The hub syncs
--     them into `devices` / `tags` on start-up: upsert by device_id and (device_id, name).
--     A tag or device removed from YAML is never deleted (history references it); the sync
--     sets retired_at instead. A renamed tag is a new tag.
--   * Enum lists below must match hub/models.py (DataType, PROTOCOLS, QUALITY_CODES);
--     tests/db/test_schema_matches_models.py checks this.
--
-- Conventions: tag path = site.area.device_id.tag; engineering value = raw * scale + offset;
-- all timestamps are timestamptz in UTC; quality 0 = good, 1 = uncertain, 2 = bad.

-- ---------------------------------------------------------------------------------------------
-- Devices and tags (mirrors config/devices/*.yaml)
-- ---------------------------------------------------------------------------------------------

CREATE TABLE devices (
    device_id    text PRIMARY KEY CHECK (device_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    name         text NOT NULL,
    site         text NOT NULL CHECK (site ~ '^[a-z][a-z0-9_]{0,63}$'),
    area         text NOT NULL CHECK (area ~ '^[a-z][a-z0-9_]{0,63}$'),
    protocol     text NOT NULL CHECK (protocol IN ('modbus_tcp', 'mqtt', 'opcua')),
    enabled      boolean NOT NULL DEFAULT false,
    connection   jsonb NOT NULL DEFAULT '{}'::jsonb,   -- as in YAML; env var NAMES only, no secrets
    description  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    retired_at   timestamptz                           -- removed from YAML; kept for history
);
CREATE INDEX devices_site_area_idx ON devices (site, area);

CREATE TABLE tags (
    tag_id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id    text NOT NULL REFERENCES devices (device_id) ON UPDATE CASCADE ON DELETE RESTRICT,
    name         text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$'),
    description  text,
    data_type    text NOT NULL CHECK (data_type IN
                   ('bool', 'int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64',
                    'float32', 'float64', 'string')),
    unit         text,
    scale        double precision NOT NULL DEFAULT 1 CHECK (scale <> 0),
    "offset"     double precision NOT NULL DEFAULT 0,
    access       text NOT NULL DEFAULT 'read' CHECK (access IN ('read', 'read_write')),
    min_value    double precision,
    max_value    double precision,
    address      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- register_type/address/bit | json_path | node_id
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    retired_at   timestamptz,
    UNIQUE (device_id, name),
    -- same rules as TagBase in hub/models.py
    CHECK (data_type NOT IN ('bool', 'string') OR (min_value IS NULL AND max_value IS NULL)),
    CHECK (access = 'read' OR data_type <> 'string'),
    CHECK (access = 'read' OR data_type = 'bool' OR (min_value IS NOT NULL AND max_value IS NOT NULL)),
    CHECK (min_value IS NULL OR max_value IS NULL OR min_value < max_value)
);

-- Full tag path as used across the hub; active tags only.
CREATE VIEW tag_paths AS
SELECT t.tag_id,
       d.site || '.' || d.area || '.' || d.device_id || '.' || t.name AS tag_path,
       d.site, d.area, d.device_id, d.protocol,
       t.name AS tag, t.data_type, t.unit, t.access, t.min_value, t.max_value
FROM tags t
JOIN devices d USING (device_id)
WHERE t.retired_at IS NULL AND d.retired_at IS NULL;

-- ---------------------------------------------------------------------------------------------
-- Time series
-- ---------------------------------------------------------------------------------------------

-- One row per sample. Exactly one value column is used, chosen by the tag's data_type.
-- int64/uint64 are stored in value_num (double): exact up to 2^53, which energy counters do
-- not reach in practice. Insert with ON CONFLICT (tag_id, ts) DO NOTHING: a driver that
-- re-sends a sample with the same timestamp must not fail the batch.
-- With TimescaleDB, 0002 turns this table into a hypertable.
CREATE TABLE samples (
    tag_id      integer NOT NULL REFERENCES tags (tag_id),
    ts          timestamptz NOT NULL,       -- device time if the device provides it, else hub time
    value_num   double precision,
    value_bool  boolean,
    value_text  text,
    quality     smallint NOT NULL DEFAULT 0 CHECK (quality IN (0, 1, 2)),
    PRIMARY KEY (tag_id, ts),
    CHECK (num_nonnulls(value_num, value_bool, value_text) <= 1)   -- bad quality may carry none
);
CREATE INDEX samples_ts_idx ON samples (ts DESC);

-- Latest value per tag, upserted by the hub on every sample (dashboard reads this table).
CREATE TABLE tag_latest (
    tag_id      integer PRIMARY KEY REFERENCES tags (tag_id) ON DELETE CASCADE,
    ts          timestamptz NOT NULL,
    value_num   double precision,
    value_bool  boolean,
    value_text  text,
    quality     smallint NOT NULL DEFAULT 0 CHECK (quality IN (0, 1, 2))
);

-- Connection health per device (what the dashboard shows first).
CREATE TABLE device_status (
    device_id     text PRIMARY KEY REFERENCES devices (device_id) ON UPDATE CASCADE ON DELETE CASCADE,
    state         text NOT NULL DEFAULT 'unknown'
                  CHECK (state IN ('unknown', 'online', 'offline', 'error', 'disabled')),
    last_seen     timestamptz,                -- last successful read / message
    last_error    text,
    last_error_at timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Users and RBAC (default deny: no role, no access)
-- ---------------------------------------------------------------------------------------------

CREATE TABLE users (
    user_id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username       text NOT NULL UNIQUE CHECK (username ~ '^[a-z][a-z0-9_.-]{2,63}$'),
    display_name   text NOT NULL,
    password_hash  text NOT NULL,             -- argon2 hash; never a plain password
    is_active      boolean NOT NULL DEFAULT true,
    failed_logins  smallint NOT NULL DEFAULT 0,
    locked_until   timestamptz,
    must_change_password boolean NOT NULL DEFAULT true,
    language       text NOT NULL DEFAULT 'fa' CHECK (language IN ('fa', 'en')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_login_at  timestamptz
);

CREATE TABLE roles (
    role_id      text PRIMARY KEY,
    description  text NOT NULL
);

CREATE TABLE permissions (
    permission_id text PRIMARY KEY,
    description   text NOT NULL
);

CREATE TABLE role_permissions (
    role_id       text NOT NULL REFERENCES roles (role_id) ON DELETE CASCADE,
    permission_id text NOT NULL REFERENCES permissions (permission_id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

-- A role granted for a scope: site NULL = every site; area NULL = every area of the site.
CREATE TABLE user_roles (
    user_role_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      integer NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    role_id      text NOT NULL REFERENCES roles (role_id),
    site         text CHECK (site ~ '^[a-z][a-z0-9_]{0,63}$'),
    area         text CHECK (area ~ '^[a-z][a-z0-9_]{0,63}$'),
    granted_by   integer REFERENCES users (user_id),
    granted_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (area IS NULL OR site IS NOT NULL)
);
CREATE UNIQUE INDEX user_roles_scope_uq
    ON user_roles (user_id, role_id, coalesce(site, ''), coalesce(area, ''));

-- Refresh-token sessions. Only a SHA-256 of the token is stored; access tokens are short JWTs.
CREATE TABLE sessions (
    session_id    uuid PRIMARY KEY,
    user_id       integer NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    refresh_hash  text NOT NULL UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz,
    client_addr   inet,
    user_agent    text
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

INSERT INTO roles (role_id, description) VALUES
    ('admin',    'Users, roles and everything below'),
    ('engineer', 'Device configuration and control'),
    ('operator', 'View and send commands to writable tags'),
    ('viewer',   'View dashboards and history only');

INSERT INTO permissions (permission_id, description) VALUES
    ('data.read',     'Read live values, history and device status'),
    ('tag.write',     'Send a value to a read_write tag (through the control-safety layer)'),
    ('write.confirm', 'Confirm a write that needs a second person'),
    ('alarm.ack',     'Acknowledge alarms'),
    ('config.edit',   'Edit device configuration'),
    ('audit.read',    'Read the write and auth audit logs'),
    ('user.manage',   'Create users and grant roles');

INSERT INTO role_permissions (role_id, permission_id) VALUES
    ('viewer',   'data.read'),
    ('operator', 'data.read'), ('operator', 'tag.write'), ('operator', 'alarm.ack'),
    ('engineer', 'data.read'), ('engineer', 'tag.write'), ('engineer', 'write.confirm'),
    ('engineer', 'alarm.ack'), ('engineer', 'config.edit'), ('engineer', 'audit.read'),
    ('admin',    'data.read'), ('admin', 'tag.write'), ('admin', 'write.confirm'),
    ('admin',    'alarm.ack'), ('admin', 'config.edit'), ('admin', 'audit.read'),
    ('admin',    'user.manage');

-- ---------------------------------------------------------------------------------------------
-- Control: writes waiting for a second person
-- ---------------------------------------------------------------------------------------------

CREATE TABLE pending_writes (
    request_id    uuid PRIMARY KEY,
    tag_id        integer NOT NULL REFERENCES tags (tag_id),
    tag_path      text NOT NULL,
    requested_by  integer NOT NULL REFERENCES users (user_id),
    value         jsonb NOT NULL,             -- engineering value as requested
    reason        text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired')),
    decided_by    integer REFERENCES users (user_id),
    decided_at    timestamptz,
    CHECK (decided_by IS NULL OR decided_by <> requested_by)   -- nobody confirms their own write
);
CREATE INDEX pending_writes_status_idx ON pending_writes (status, expires_at);

-- ---------------------------------------------------------------------------------------------
-- Alarms
-- ---------------------------------------------------------------------------------------------

-- source = tag path for limit alarms, 'device:<device_id>' for offline alarms. At most one
-- uncleared alarm per source. An alarm stays in the active list until cleared AND acknowledged.
CREATE TABLE alarm_events (
    alarm_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text NOT NULL,
    tag_id       integer REFERENCES tags (tag_id),
    device_id    text REFERENCES devices (device_id) ON UPDATE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('hihi', 'hi', 'lo', 'lolo', 'state', 'offline')),
    severity     text NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    message      text NOT NULL,
    value        text,
    raised_at    timestamptz NOT NULL,
    cleared_at   timestamptz,
    acked_at     timestamptz,
    acked_by     integer REFERENCES users (user_id),
    ack_comment  text
);
CREATE UNIQUE INDEX alarm_events_one_open_uq ON alarm_events (source) WHERE cleared_at IS NULL;
CREATE INDEX alarm_events_raised_idx ON alarm_events (raised_at DESC);

-- ---------------------------------------------------------------------------------------------
-- Audit (append-only)
-- ---------------------------------------------------------------------------------------------

-- Every write attempt writes a 'requested' row before anything is sent to the device, then one
-- outcome row with the same request_id. A request with no outcome row means the hub stopped
-- mid-write: the device state is unknown and must be checked.
CREATE TABLE write_audit (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts               timestamptz NOT NULL DEFAULT now(),
    request_id       uuid NOT NULL,
    phase            text NOT NULL CHECK (phase IN ('requested', 'outcome')),
    user_id          integer NOT NULL REFERENCES users (user_id),
    username         text NOT NULL,          -- copy, so the log reads correctly after renames
    client_addr      inet,
    tag_id           integer NOT NULL REFERENCES tags (tag_id),
    tag_path         text NOT NULL,          -- copy of the path at the time of the write
    requested_value  text NOT NULL,
    previous_value   text,                   -- value read just before the write
    readback_value   text,                   -- value read back after the write
    confirmed_by     integer REFERENCES users (user_id),
    result           text NOT NULL CHECK (result IN (
                         'requested', 'applied', 'readback_mismatch', 'failed', 'timeout',
                         'rejected_rbac', 'rejected_disabled', 'rejected_range', 'rejected_rate',
                         'rejected_interlock', 'rejected_unconfirmed')),
    reason           text,
    UNIQUE (request_id, phase),
    CHECK ((phase = 'requested') = (result = 'requested'))
);
CREATE INDEX write_audit_tag_ts_idx ON write_audit (tag_id, ts DESC);
CREATE INDEX write_audit_user_ts_idx ON write_audit (user_id, ts DESC);

-- Logins, lockouts, user/role changes and alarm acknowledgements.
CREATE TABLE auth_audit (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    event        text NOT NULL CHECK (event IN (
                     'login_ok', 'login_failed', 'logout', 'locked', 'unlocked',
                     'password_changed', 'user_created', 'user_updated', 'user_disabled',
                     'role_granted', 'role_revoked', 'alarm_acked', 'write_confirmed',
                     'write_rejected')),
    username     text NOT NULL,              -- as typed for failed logins
    user_id      integer REFERENCES users (user_id),
    actor_id     integer REFERENCES users (user_id),
    client_addr  inet,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb   -- never passwords or tokens
);
CREATE INDEX auth_audit_ts_idx ON auth_audit (ts DESC);

CREATE FUNCTION audit_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER write_audit_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON write_audit
    FOR EACH STATEMENT EXECUTE FUNCTION audit_is_append_only();
CREATE TRIGGER auth_audit_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON auth_audit
    FOR EACH STATEMENT EXECUTE FUNCTION audit_is_append_only();

-- The application role (create it in deployment, e.g. CREATE ROLE hub_app LOGIN) gets
-- INSERT/SELECT only on the audit tables:
--   REVOKE UPDATE, DELETE, TRUNCATE ON write_audit, auth_audit FROM hub_app;
