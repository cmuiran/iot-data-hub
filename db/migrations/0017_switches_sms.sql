-- 0017_switches_sms: how each output is switched in the home (a toggle, separate on and off
-- keys, or a momentary push), the people a home alerts by SMS or call and who may send it
-- commands, a scene's SMS keyword, and the home's SMS PIN.

ALTER TABLE output_labels
    ADD COLUMN mode    text NOT NULL DEFAULT 'toggle' CHECK (mode IN ('toggle', 'dual', 'momentary')),
    ADD COLUMN pulse_s real CHECK (pulse_s > 0 AND pulse_s <= 600);

-- people of a home reached by SMS / voice call (they need no account); ``command``: may run
-- the home's scenes by an SMS keyword (with the home's PIN)
CREATE TABLE home_contacts (
    contact_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      text NOT NULL REFERENCES organizations (org_id),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    phone       text NOT NULL CHECK (phone ~ '^\+?[0-9]{6,15}$'),
    sms         boolean NOT NULL DEFAULT true,
    call        boolean NOT NULL DEFAULT false,
    command     boolean NOT NULL DEFAULT false,
    created_by  integer REFERENCES users (user_id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, phone)
);
CREATE INDEX home_contacts_phone_idx ON home_contacts (phone);

ALTER TABLE scenes ADD COLUMN sms_keyword text
    CHECK (sms_keyword IS NULL OR length(sms_keyword) BETWEEN 2 AND 20);
CREATE UNIQUE INDEX scenes_keyword_idx ON scenes (org_id, lower(sms_keyword))
    WHERE sms_keyword IS NOT NULL;

-- the PIN an SMS command must carry (an argon2 hash, never the PIN itself)
ALTER TABLE home_settings ADD COLUMN sms_pin_hash text;

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
    'firmware_withdrawn', 'firmware_rollout',
    'account_deleted', 'home_created', 'home_updated', 'ticket_opened', 'ticket_updated',
    'plan_changed', 'branding_saved', 'sms_settings_saved', 'group_saved',
    'contact_saved', 'contact_deleted', 'sms_pin_set', 'sms_command', 'sms_command_refused'));
