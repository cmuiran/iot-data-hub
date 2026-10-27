-- Rollback of 0012_commands_ingest (python -m hub.storage.migrate --rollback 0012_commands_ingest).
-- Refused while native devices exist (their history needs the protocol): restore the backup.
-- The command history is dropped (write_audit keeps every write). The auth_audit event name
-- stays (the audit log is append-only).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM devices WHERE protocol = 'native') THEN
        RAISE EXCEPTION 'native devices exist; restore the pre-upgrade backup instead';
    END IF;
END
$$;

DROP TABLE commands;
ALTER TABLE devices DROP CONSTRAINT devices_protocol_check;
ALTER TABLE devices ADD CONSTRAINT devices_protocol_check
    CHECK (protocol IN ('modbus_tcp', 'modbus_rtu', 'mqtt', 'opcua', 's7', 'http', 'snmp',
                        'fins', 'mc', 'enip', 'iec104', 'calc'));
