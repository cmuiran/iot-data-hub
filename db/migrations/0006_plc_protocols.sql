-- 0006_plc_protocols: Omron FINS, Mitsubishi MC, EtherNet/IP (Logix) and IEC 60870-5-104
-- devices (phase 7). Their addresses are stored in tags.address like the others.

ALTER TABLE devices DROP CONSTRAINT devices_protocol_check;
ALTER TABLE devices ADD CONSTRAINT devices_protocol_check
    CHECK (protocol IN ('modbus_tcp', 'modbus_rtu', 'mqtt', 'opcua', 's7', 'http', 'snmp',
                        'fins', 'mc', 'enip', 'iec104'));
