-- 0005_protocols: Siemens S7, HTTP/REST and SNMP devices (phase 3).
-- Their addresses (S7 address, path + json_path, OID) are stored in tags.address like the
-- others; nothing else in the schema depends on the protocol.

ALTER TABLE devices DROP CONSTRAINT devices_protocol_check;
ALTER TABLE devices ADD CONSTRAINT devices_protocol_check
    CHECK (protocol IN ('modbus_tcp', 'modbus_rtu', 'mqtt', 'opcua', 's7', 'http', 'snmp'));
