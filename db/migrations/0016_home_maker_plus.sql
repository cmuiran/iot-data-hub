-- 0016_home_maker_plus: device groups, home settings (location, tariff), SMS fallback, support
-- tickets and warranty claims, maker branding, maker plans and invoices.
-- Rollback: db/migrations/down/0016_home_maker_plus.sql.
--
-- These tables are reached only through the API, which checks the caller's organizations
-- first (as for rooms); none of them is granted to hub_tenant.

-- a home's groups of devices, shown together (combined I/O) or one by one
CREATE TABLE device_groups (
    group_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      text NOT NULL REFERENCES organizations (org_id),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    icon        text CHECK (icon ~ '^[a-z][a-z0-9_-]{0,39}$'),
    device_ids  text[] NOT NULL CHECK (cardinality(device_ids) BETWEEN 1 AND 50),
    -- named parts inside the group ("chandelier": relay 1 and 2 of one device, ...):
    -- [{"name", "icon", "outputs": [{"device_id", "key"}]}]
    sections    jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(sections) = 'array'),
    position    integer NOT NULL DEFAULT 0,
    created_by  integer REFERENCES users (user_id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX device_groups_org_idx ON device_groups (org_id, position);

-- the owner home's own name and icon for an output or reading of a device ("relay 1" ->
-- "chandelier"), and whether it is a favourite on the home screen; the maker never sees it
CREATE TABLE output_labels (
    org_id      text NOT NULL REFERENCES organizations (org_id),
    device_id   text NOT NULL REFERENCES platform_devices (device_id),
    key         text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,63}$'),
    name        text CHECK (length(name) BETWEEN 1 AND 40),
    icon        text CHECK (icon ~ '^[a-z][a-z0-9_-]{0,39}$'),
    favorite    boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, device_id, key)
);

-- a home's place (sunrise / sunset, presence) and electricity tariff
CREATE TABLE home_settings (
    org_id      text PRIMARY KEY REFERENCES organizations (org_id),
    latitude    double precision CHECK (latitude BETWEEN -90 AND 90),
    longitude   double precision CHECK (longitude BETWEEN -180 AND 180),
    radius_m    integer NOT NULL DEFAULT 150 CHECK (radius_m BETWEEN 30 AND 5000),
    tariff      jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- the SMS fallback of one device in its owner's home: the device's SIM number and a random
-- signing key the hub makes (the device gets it over its own connection; the app signs each
-- message the owner sends by hand with it, so a seen message can be neither changed nor replayed)
CREATE TABLE device_sms (
    device_id   text PRIMARY KEY REFERENCES platform_devices (device_id),
    org_id      text NOT NULL REFERENCES organizations (org_id),
    phone       text CHECK (phone ~ '^\+?[0-9]{6,15}$'),
    key         text NOT NULL CHECK (key ~ '^[A-Za-z0-9_-]{43}$'),     -- 32 bytes, base64url
    updated_by  integer REFERENCES users (user_id),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- support tickets and warranty claims between a buyer and the device's maker
CREATE TABLE tickets (
    ticket_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id      text NOT NULL REFERENCES platform_devices (device_id),
    maker_org_id   text NOT NULL REFERENCES organizations (org_id),
    owner_org_id   text NOT NULL REFERENCES organizations (org_id),
    opened_by      integer NOT NULL REFERENCES users (user_id),
    kind           text NOT NULL CHECK (kind IN ('support', 'warranty')),
    subject        text NOT NULL CHECK (length(subject) BETWEEN 1 AND 120),
    status         text NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'answered', 'in_progress', 'approved', 'rejected',
                                     'closed')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tickets_maker_idx ON tickets (maker_org_id, status, updated_at DESC);
CREATE INDEX tickets_owner_idx ON tickets (owner_org_id, updated_at DESC);

CREATE TABLE ticket_messages (
    message_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id   bigint NOT NULL REFERENCES tickets (ticket_id) ON DELETE CASCADE,
    side        text NOT NULL CHECK (side IN ('buyer', 'maker')),
    author_id   integer REFERENCES users (user_id),
    author_name text NOT NULL,
    body        text NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ticket_messages_idx ON ticket_messages (ticket_id, created_at);

-- a maker's brand: shown in its panel, on its devices in the app, and as the app's own look
-- when the app is opened with ?brand=<org_id>
CREATE TABLE org_branding (
    org_id      text PRIMARY KEY REFERENCES organizations (org_id),
    brand_name  text CHECK (length(brand_name) BETWEEN 1 AND 60),
    accent      text CHECK (accent ~ '^#[0-9A-Fa-f]{6}$'),
    support_email text CHECK (length(support_email) BETWEEN 3 AND 254),
    support_phone text CHECK (support_phone ~ '^\+?[0-9 -]{6,20}$'),
    logo        bytea CHECK (octet_length(logo) BETWEEN 1 AND 262144),
    logo_type   text CHECK (logo_type IN ('image/png', 'image/jpeg', 'image/webp')),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((logo IS NULL) = (logo_type IS NULL))
);

-- what makers pay for the platform: a plan per organization, an invoice per Jalali month
CREATE TABLE plans (
    plan_id           text PRIMARY KEY CHECK (plan_id ~ '^[a-z][a-z0-9_]{1,31}$'),
    name              text NOT NULL,
    monthly_fee       bigint NOT NULL DEFAULT 0 CHECK (monthly_fee >= 0),       -- rial
    included_devices  integer NOT NULL DEFAULT 0 CHECK (included_devices >= 0),
    per_device_fee    bigint NOT NULL DEFAULT 0 CHECK (per_device_fee >= 0),    -- rial / month
    max_devices       integer CHECK (max_devices > 0)
);
INSERT INTO plans (plan_id, name, monthly_fee, included_devices, per_device_fee, max_devices) VALUES
    ('starter', 'Starter', 0, 100, 0, 100),
    ('business', 'Business', 50000000, 1000, 20000, NULL),
    ('enterprise', 'Enterprise', 300000000, 20000, 8000, NULL);

CREATE TABLE org_plans (
    org_id      text PRIMARY KEY REFERENCES organizations (org_id),
    plan_id     text NOT NULL REFERENCES plans (plan_id),
    since       timestamptz NOT NULL DEFAULT now(),
    set_by      integer REFERENCES users (user_id)
);

CREATE TABLE invoices (
    invoice_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id         text NOT NULL REFERENCES organizations (org_id),
    period         text NOT NULL CHECK (period ~ '^[0-9]{4}-[0-9]{2}$'),   -- Jalali year-month
    plan_id        text NOT NULL REFERENCES plans (plan_id),
    active_devices integer NOT NULL,
    amount         bigint NOT NULL,                                         -- rial
    status         text NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'paid', 'void')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    paid_at        timestamptz,
    UNIQUE (org_id, period)
);

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
    'plan_changed', 'branding_saved', 'sms_settings_saved', 'group_saved'));
