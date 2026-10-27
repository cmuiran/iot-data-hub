-- Rollback of 0008_building_model (python -m hub.storage.migrate --rollback 0008_building_model).
-- Take a backup first: the building model tables, calculated-tag versions, collection errors and
-- maintenance records are dropped. Refused (nothing changes) when calculated devices exist
-- (their history needs the 'calc' protocol: restore the pre-upgrade backup instead) or users
-- still hold the setpoint_operator role (revoke it first).
--
-- Kept on purpose: the two extra auth_audit event names (the audit log is append-only, rows
-- that use them cannot be removed; older code simply never writes them).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM devices WHERE protocol = 'calc') THEN
        RAISE EXCEPTION 'calculated devices exist; restore the pre-upgrade backup instead';
    END IF;
    IF EXISTS (SELECT 1 FROM user_roles WHERE role_id = 'setpoint_operator') THEN
        RAISE EXCEPTION 'users still hold the setpoint_operator role; revoke it first';
    END IF;
    IF EXISTS (SELECT 1 FROM user_roles WHERE org_id IS NOT NULL) THEN
        RAISE EXCEPTION 'roles are granted for organizations; revoke them first';
    END IF;
END
$$;

DELETE FROM role_permissions WHERE permission_id IN
    ('control.setpoint', 'control.command', 'model.edit', 'calc.publish', 'maintenance.write');
DELETE FROM role_permissions WHERE role_id = 'setpoint_operator';
DELETE FROM permissions WHERE permission_id IN
    ('control.setpoint', 'control.command', 'model.edit', 'calc.publish', 'maintenance.write');
DELETE FROM roles WHERE role_id = 'setpoint_operator';

DROP INDEX user_roles_scope_uq;
ALTER TABLE user_roles DROP COLUMN org_id;
CREATE UNIQUE INDEX user_roles_scope_uq
    ON user_roles (user_id, role_id, coalesce(site, ''), coalesce(area, ''));

DROP TABLE maintenance_records;
DROP TABLE equipment_points;
DROP TABLE equipment;
DROP TABLE building_systems;
DROP TABLE locations;
DROP TABLE organization_sites;
DROP TABLE organizations;
DROP TABLE collection_errors;
DROP TABLE calc_versions;

ALTER TABLE tag_latest DROP COLUMN received_at;
ALTER TABLE tag_latest DROP COLUMN reason;
ALTER TABLE samples DROP COLUMN reason;
ALTER TABLE tags DROP COLUMN write_class;

ALTER TABLE devices DROP CONSTRAINT devices_protocol_check;
ALTER TABLE devices ADD CONSTRAINT devices_protocol_check
    CHECK (protocol IN ('modbus_tcp', 'modbus_rtu', 'mqtt', 'opcua', 's7', 'http', 'snmp',
                        'fins', 'mc', 'enip', 'iec104'));
