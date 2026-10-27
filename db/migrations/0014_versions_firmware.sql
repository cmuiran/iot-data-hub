-- 0014_versions_firmware: upgrading devices to a newer template version, and firmware
-- (platform phase 7). Rollback: db/migrations/down/0014_versions_firmware.sql.
--
-- A firmware release is an image uploaded by the manufacturer for one product (kept here, at
-- most 16 MB, with its SHA-256), offered to devices by a rollout: one job per device, whose
-- status the device reports (downloading, installed, failed). Devices fetch the image with
-- their own credential and must check the SHA-256 (and their own signature, e.g. ESP32 secure
-- boot) before installing.

ALTER TABLE platform_devices ADD COLUMN firmware_version text
    CHECK (length(firmware_version) BETWEEN 1 AND 40);
ALTER TABLE platform_devices ADD COLUMN firmware_reported_at timestamptz;

CREATE TABLE firmware_releases (
    release_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id           uuid NOT NULL REFERENCES products (product_id),
    version              text NOT NULL CHECK (version ~ '^[0-9A-Za-z][0-9A-Za-z.+-]{0,39}$'),
    notes                text,
    sha256               text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes           integer NOT NULL CHECK (size_bytes BETWEEN 1 AND 16777216),
    image                bytea NOT NULL,
    min_template_version integer CHECK (min_template_version >= 1),
    status               text NOT NULL DEFAULT 'released'
                         CHECK (status IN ('released', 'withdrawn')),
    created_by           integer REFERENCES users (user_id),
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (product_id, version)
);

CREATE TABLE firmware_jobs (
    job_id            uuid PRIMARY KEY,
    release_id        bigint NOT NULL REFERENCES firmware_releases (release_id),
    device_id         text NOT NULL REFERENCES platform_devices (device_id),
    status            text NOT NULL DEFAULT 'pending' CHECK (status IN (
                          'pending', 'sent', 'downloading', 'installed', 'failed',
                          'cancelled')),
    created_by        integer REFERENCES users (user_id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    reported_version  text,
    error             text
);
-- one open job per device
CREATE UNIQUE INDEX firmware_jobs_open_uq ON firmware_jobs (device_id)
    WHERE status IN ('pending', 'sent', 'downloading');
CREATE INDEX firmware_jobs_release_idx ON firmware_jobs (release_id, status);

ALTER TABLE firmware_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE firmware_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_read ON firmware_releases FOR SELECT TO hub_tenant
    USING (app_product_made_by_caller(product_id));
CREATE POLICY tenant_read ON firmware_jobs FOR SELECT TO hub_tenant
    USING (app_device_maker_is_caller(device_id) OR app_device_owned_by_caller(device_id));
GRANT SELECT (release_id, product_id, version, notes, sha256, size_bytes, min_template_version,
              status, created_by, created_at) ON firmware_releases TO hub_tenant;
GRANT SELECT ON firmware_jobs TO hub_tenant;

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
    'device_auth_failed', 'automation_saved', 'automation_deleted', 'scene_saved',
    'scene_deleted', 'scene_run', 'holiday_saved', 'devices_upgraded', 'firmware_released',
    'firmware_withdrawn', 'firmware_rollout'));
