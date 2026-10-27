-- 0008_building_model: building information model, calculated tags, data quality reasons,
-- setpoint/command permissions and equipment maintenance records (BMS phase 1).
-- Rollback: db/migrations/down/0008_building_model.sql (python -m hub.storage.migrate --rollback).
--
-- The building model mirrors config/building.yaml like devices/tags mirror config/devices:
-- the hub upserts it on start-up and on every configuration change, and sets retired_at on
-- what disappeared (maintenance records keep referring to retired equipment).

-- ---------------------------------------------------------------------------------------------
-- Calculated tags, write classes, quality reasons
-- ---------------------------------------------------------------------------------------------

ALTER TABLE devices DROP CONSTRAINT devices_protocol_check;
ALTER TABLE devices ADD CONSTRAINT devices_protocol_check
    CHECK (protocol IN ('modbus_tcp', 'modbus_rtu', 'mqtt', 'opcua', 's7', 'http', 'snmp',
                        'fins', 'mc', 'enip', 'iec104', 'calc'));

ALTER TABLE tags ADD COLUMN write_class text CHECK (write_class IN ('setpoint', 'command'));

-- Why a sample is not good (QualityReason / REASON_CODES in hub/models.py):
-- 1 comm_failure, 2 out_of_range, 3 stale, 4 config_error, 5 device_reported, 6 math_error,
-- 7 input_bad, 8 manual. NULL = good, or no reason known.
ALTER TABLE samples ADD COLUMN reason smallint CHECK (reason BETWEEN 1 AND 8);
ALTER TABLE tag_latest ADD COLUMN reason smallint CHECK (reason BETWEEN 1 AND 8);
-- when the hub received the value (ts is the source time when the device provides one)
ALTER TABLE tag_latest ADD COLUMN received_at timestamptz;

-- Every definition of a calculated tag that was in force, from valid_from until the next row:
-- each stored result can be traced to the formula that computed it.
CREATE TABLE calc_versions (
    tag_id      integer NOT NULL REFERENCES tags (tag_id),
    valid_from  timestamptz NOT NULL DEFAULT now(),
    version     text NOT NULL CHECK (version ~ '^[0-9a-f]{16}$'),
    definition  jsonb NOT NULL,          -- formula, inputs, unit, data type, missing, ...
    PRIMARY KEY (tag_id, valid_from)
);

-- Collection problems over time (device_status only keeps the last one).
CREATE TABLE collection_errors (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    device_id   text NOT NULL REFERENCES devices (device_id) ON UPDATE CASCADE ON DELETE CASCADE,
    state       text NOT NULL CHECK (state IN ('offline', 'error')),
    message     text NOT NULL
);
CREATE INDEX collection_errors_device_ts_idx ON collection_errors (device_id, ts DESC);
CREATE INDEX collection_errors_ts_idx ON collection_errors (ts);

-- ---------------------------------------------------------------------------------------------
-- Building information model
-- ---------------------------------------------------------------------------------------------

CREATE TABLE organizations (
    org_id      text PRIMARY KEY CHECK (org_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    name        text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    retired_at  timestamptz
);

CREATE TABLE organization_sites (
    site        text PRIMARY KEY CHECK (site ~ '^[a-z][a-z0-9_]{0,63}$'),
    org_id      text NOT NULL REFERENCES organizations (org_id) ON DELETE CASCADE
);

CREATE TABLE locations (
    location_id text PRIMARY KEY CHECK (location_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    kind        text NOT NULL CHECK (kind IN ('campus', 'building', 'floor', 'zone', 'room')),
    name        text NOT NULL,
    parent_id   text REFERENCES locations (location_id) DEFERRABLE INITIALLY DEFERRED,
    site        text,                      -- campus only; below it the site is inherited
    level       integer,
    area_m2     double precision CHECK (area_m2 > 0),
    description text,
    haystack    text[] NOT NULL DEFAULT '{}',
    brick_class text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    retired_at  timestamptz,
    CHECK ((kind = 'campus') = (parent_id IS NULL)),
    CHECK ((kind = 'campus') = (site IS NOT NULL))
);
CREATE INDEX locations_parent_idx ON locations (parent_id);

CREATE TABLE building_systems (
    system_id   text PRIMARY KEY CHECK (system_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    kind        text NOT NULL CHECK (kind IN ('hvac', 'electrical', 'water', 'lighting', 'fire',
                                              'access', 'elevator', 'security', 'metering',
                                              'other')),
    name        text NOT NULL,
    building_id text NOT NULL REFERENCES locations (location_id) DEFERRABLE INITIALLY DEFERRED,
    description text,
    haystack    text[] NOT NULL DEFAULT '{}',
    brick_class text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    retired_at  timestamptz
);

CREATE TABLE equipment (
    equipment_id   text PRIMARY KEY CHECK (equipment_id ~ '^[a-z][a-z0-9_]{0,63}$'),
    name           text NOT NULL,
    type           text NOT NULL,
    location_id    text NOT NULL REFERENCES locations (location_id) DEFERRABLE INITIALLY DEFERRED,
    system_id      text REFERENCES building_systems (system_id) DEFERRABLE INITIALLY DEFERRED,
    device_id      text,                   -- the device it is read through (may be retired)
    manufacturer   text,
    model          text,
    serial_number  text,
    status         text NOT NULL DEFAULT 'in_service'
                   CHECK (status IN ('in_service', 'maintenance', 'out_of_service', 'retired')),
    installed_on   date,
    warranty_until date,
    description    text,
    documents      jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{title, ref}]: links, not files
    haystack       text[] NOT NULL DEFAULT '{}',
    brick_class    text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    retired_at     timestamptz
);
CREATE INDEX equipment_location_idx ON equipment (location_id);

CREATE TABLE equipment_points (
    equipment_id text NOT NULL REFERENCES equipment (equipment_id) ON DELETE CASCADE,
    role         text NOT NULL CHECK (role ~ '^[a-z][a-z0-9_]{0,63}$'),
    tag_id       integer NOT NULL UNIQUE REFERENCES tags (tag_id),
    description  text,
    haystack     text[] NOT NULL DEFAULT '{}',
    brick_class  text,
    PRIMARY KEY (equipment_id, role)
);

-- Maintenance history of an equipment (data entered by people, not configuration).
CREATE TABLE maintenance_records (
    record_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    equipment_id  text NOT NULL REFERENCES equipment (equipment_id),
    performed_at  timestamptz NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('inspection', 'preventive', 'corrective',
                                                'calibration', 'replacement', 'note')),
    description   text NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
    performed_by  text,                    -- technician / contractor as written
    work_order    text,
    created_by    integer NOT NULL REFERENCES users (user_id),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX maintenance_records_equipment_idx
    ON maintenance_records (equipment_id, performed_at DESC);
-- corrections are new records: nothing is changed or removed afterwards
CREATE TRIGGER maintenance_records_append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON maintenance_records FOR EACH STATEMENT EXECUTE FUNCTION audit_is_append_only();

-- ---------------------------------------------------------------------------------------------
-- Permissions: setpoint vs command, model, calculations, maintenance; config restore events
-- ---------------------------------------------------------------------------------------------

INSERT INTO roles (role_id, description) VALUES
    ('setpoint_operator', 'View, acknowledge alarms and adjust setpoints (no start/stop)');

INSERT INTO permissions (permission_id, description) VALUES
    ('control.setpoint',  'Change setpoints (tags with write_class setpoint)'),
    ('control.command',   'Send commands: start/stop, open/close (write_class command or unset)'),
    ('model.edit',        'Edit the building model (config/building.yaml)'),
    ('calc.publish',      'Publish or change calculated tags'),
    ('maintenance.write', 'Add maintenance records to equipment');

-- tag.write keeps meaning "both"; the new permissions let a role have only one of them
INSERT INTO role_permissions (role_id, permission_id) VALUES
    ('setpoint_operator', 'data.read'), ('setpoint_operator', 'alarm.ack'),
    ('setpoint_operator', 'control.setpoint'),
    ('operator', 'control.setpoint'), ('operator', 'control.command'),
    ('operator', 'maintenance.write'),
    ('engineer', 'control.setpoint'), ('engineer', 'control.command'),
    ('engineer', 'model.edit'), ('engineer', 'calc.publish'), ('engineer', 'maintenance.write'),
    ('admin', 'control.setpoint'), ('admin', 'control.command'), ('admin', 'model.edit'),
    ('admin', 'calc.publish'), ('admin', 'maintenance.write');

-- A role granted for an organization covers every site of that organization
-- (organization_sites); site/area stay NULL then.
ALTER TABLE user_roles ADD COLUMN org_id text CHECK (org_id ~ '^[a-z][a-z0-9_]{0,63}$');
ALTER TABLE user_roles ADD CONSTRAINT user_roles_org_or_site_check
    CHECK (org_id IS NULL OR site IS NULL);
DROP INDEX user_roles_scope_uq;
CREATE UNIQUE INDEX user_roles_scope_uq ON user_roles
    (user_id, role_id, coalesce(org_id, ''), coalesce(site, ''), coalesce(area, ''));

ALTER TABLE auth_audit DROP CONSTRAINT auth_audit_event_check;
ALTER TABLE auth_audit ADD CONSTRAINT auth_audit_event_check CHECK (event IN (
    'login_ok', 'login_failed', 'login_throttled', 'logout', 'locked', 'unlocked',
    'password_changed', 'password_expired', 'user_created', 'user_updated', 'user_disabled',
    'role_granted', 'role_revoked', 'session_revoked', 'session_expired', 'alarm_acked',
    'alarm_shelved', 'alarm_unshelved', 'write_confirmed', 'write_rejected', 'config_changed',
    'schedule_created', 'schedule_updated', 'schedule_deleted', 'opcua_login', 'mfa_enabled',
    'mfa_disabled', 'mfa_failed', 'mfa_reset', 'network_denied', 'roles_synced',
    'directory_unavailable', 'config_restored', 'maintenance_recorded'));
