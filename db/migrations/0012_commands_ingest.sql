-- 0012_commands_ingest: the platform device contract (protocol native over MQTT / HTTP / TCP)
-- and the life of every command (platform phase 4).
-- Rollback: db/migrations/down/0012_commands_ingest.sql.
--
-- A command is one write through the control-safety layer (command_id = the write's
-- request_id, also sent to the device, which must not execute an id twice). Its status moves
-- created -> sent -> delivered -> executed -> confirmed, or ends failed / expired:
--   sent       handed to the transport (published, written on the socket, fetched by HTTP)
--   delivered  the device said it received it (native) / the protocol write succeeded (polled)
--   executed   the device said it carried it out (native)
--   confirmed  the new value came back from the device (telemetry / read-back)
-- write_audit keeps the security record; this table follows the command on its way.

ALTER TABLE devices DROP CONSTRAINT devices_protocol_check;
ALTER TABLE devices ADD CONSTRAINT devices_protocol_check
    CHECK (protocol IN ('modbus_tcp', 'modbus_rtu', 'mqtt', 'opcua', 's7', 'http', 'snmp',
                        'fins', 'mc', 'enip', 'iec104', 'native', 'calc'));

CREATE TABLE commands (
    command_id    uuid PRIMARY KEY,
    device_id     text NOT NULL REFERENCES devices (device_id) ON UPDATE CASCADE,
    tag_id        integer NOT NULL REFERENCES tags (tag_id),
    tag_path      text NOT NULL,
    value         jsonb NOT NULL,
    status        text NOT NULL DEFAULT 'created' CHECK (status IN (
                      'created', 'sent', 'delivered', 'executed', 'confirmed', 'failed',
                      'expired')),
    transport     text,                      -- mqtt / http / tcp, or the polled protocol
    created_by    integer REFERENCES users (user_id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    sent_at       timestamptz,
    delivered_at  timestamptz,
    executed_at   timestamptz,
    finished_at   timestamptz,               -- confirmed, failed or expired
    expires_at    timestamptz NOT NULL,
    error         text
);
CREATE INDEX commands_device_idx ON commands (device_id, created_at DESC);
CREATE INDEX commands_open_idx ON commands (expires_at) WHERE finished_at IS NULL;

ALTER TABLE auth_audit DROP CONSTRAINT auth_audit_event_check;
ALTER TABLE auth_audit ADD CONSTRAINT auth_audit_event_check CHECK (event IN (
    'login_ok', 'login_failed', 'login_throttled', 'logout', 'locked', 'unlocked',
    'password_changed', 'password_expired', 'user_created', 'user_updated', 'user_disabled',
    'role_granted', 'role_revoked', 'session_revoked', 'session_expired', 'alarm_acked',
    'alarm_shelved', 'alarm_unshelved', 'write_confirmed', 'write_rejected', 'config_changed',
    'schedule_created', 'schedule_updated', 'schedule_deleted', 'opcua_login', 'mfa_enabled',
    'mfa_disabled', 'mfa_failed', 'mfa_reset', 'network_denied', 'roles_synced',
    'directory_unavailable', 'config_restored', 'maintenance_recorded',
    'organization_created', 'organization_updated', 'product_saved', 'product_version_saved',
    'product_version_published', 'devices_registered', 'platform_device_updated',
    'account_signup', 'email_verified', 'password_reset_requested', 'password_reset',
    'invitation_created', 'invitation_accepted', 'invitation_revoked', 'member_removed',
    'device_shared', 'device_share_revoked', 'account_throttled',
    'claim_codes_issued', 'device_claimed', 'claim_failed', 'device_released',
    'device_transfer_started', 'device_reset', 'child_device_added', 'child_device_removed',
    'device_auth_failed'));
