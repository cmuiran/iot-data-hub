-- 0010_accounts_sharing: self-service accounts and sharing (platform phase 2): e-mail sign-up
-- and verification, password reset, personal organizations, invitations to an organization,
-- sharing a device with another organization, customer roles.
-- Rollback: db/migrations/down/0010_accounts_sharing.sql.
--
-- Tokens (verification, reset, invitation) are random; only their SHA-256 is stored, each is
-- used once and expires. A self-registered user gets a personal organization (kind customer)
-- as its owner; family members or staff join an organization by invitation; a device is shared
-- by giving another organization operator or viewer access (device_access, from 0009).

-- ---------------------------------------------------------------------------------------------
-- Users: e-mail; organizations: the personal one of a user
-- ---------------------------------------------------------------------------------------------

ALTER TABLE users ADD COLUMN email text CHECK (email ~ '^[^@\s]{1,64}@[^@\s]{1,190}$');
ALTER TABLE users ADD COLUMN email_verified_at timestamptz;
CREATE UNIQUE INDEX users_email_uq ON users (lower(email)) WHERE email IS NOT NULL;

ALTER TABLE organizations ADD COLUMN personal_user_id integer UNIQUE
    REFERENCES users (user_id) ON DELETE SET NULL;

-- ---------------------------------------------------------------------------------------------
-- One-time account tokens and invitations
-- ---------------------------------------------------------------------------------------------

CREATE TABLE account_tokens (
    token_hash  text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_id     integer NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    purpose     text NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);
CREATE INDEX account_tokens_user_idx ON account_tokens (user_id, purpose);

CREATE TABLE invitations (
    invitation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('organization', 'device')),
    org_id        text NOT NULL REFERENCES organizations (org_id),   -- the inviting org
    device_id     text REFERENCES platform_devices (device_id),      -- kind device
    role_id       text REFERENCES roles (role_id),                   -- kind organization
    level         text CHECK (level IN ('operator', 'viewer')),      -- kind device
    email         text NOT NULL CHECK (email ~ '^[^@\s]{1,64}@[^@\s]{1,190}$'),
    token_hash    text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    invited_by    integer REFERENCES users (user_id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    accepted_at   timestamptz,
    accepted_by   integer REFERENCES users (user_id),
    accepted_org  text REFERENCES organizations (org_id),
    revoked_at    timestamptz,
    CHECK ((kind = 'organization') = (role_id IS NOT NULL AND device_id IS NULL
                                      AND level IS NULL)),
    CHECK ((kind = 'device') = (device_id IS NOT NULL AND level IS NOT NULL
                                AND role_id IS NULL))
);
CREATE INDEX invitations_org_idx ON invitations (org_id, created_at DESC);
CREATE INDEX invitations_email_idx ON invitations (lower(email)) WHERE accepted_at IS NULL
    AND revoked_at IS NULL;

-- ---------------------------------------------------------------------------------------------
-- Tenant isolation for the new rows
-- ---------------------------------------------------------------------------------------------

ALTER TABLE invitations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_read ON invitations FOR SELECT TO hub_tenant
    USING (org_id = ANY(app_org_ids()));
CREATE POLICY tenant_insert ON invitations FOR INSERT TO hub_tenant
    WITH CHECK (org_id = ANY(app_org_ids())
                AND (device_id IS NULL OR app_device_owned_by_caller(device_id)));
CREATE POLICY tenant_update ON invitations FOR UPDATE TO hub_tenant
    USING (org_id = ANY(app_org_ids())) WITH CHECK (org_id = ANY(app_org_ids()));
GRANT SELECT, INSERT, UPDATE ON invitations TO hub_tenant;
GRANT USAGE ON SEQUENCE invitations_invitation_id_seq TO hub_tenant;

-- the owner revokes a share of its device; an organization gives up a share it received
CREATE POLICY tenant_update ON device_access FOR UPDATE TO hub_tenant
    USING (level <> 'owner' AND (app_device_owned_by_caller(device_id)
                                 OR org_id = ANY(app_org_ids())))
    WITH CHECK (level <> 'owner');
GRANT UPDATE (revoked_at) ON device_access TO hub_tenant;

-- ---------------------------------------------------------------------------------------------
-- Permissions and roles (organization scope)
-- ---------------------------------------------------------------------------------------------

INSERT INTO permissions (permission_id, description) VALUES
    ('device.view',    'See the devices of an organization, their values and history'),
    ('device.operate', 'Operate the devices of an organization (through the control layer)'),
    ('device.share',   'Share the organization''s own devices with other organizations'),
    ('org.members',    'Invite and remove the members of an organization');

INSERT INTO roles (role_id, description) VALUES
    ('customer_owner',  'Owner of a home or company: devices, sharing and members'),
    ('customer_member', 'Family member or staff: sees and operates the devices'),
    ('customer_viewer', 'Sees the devices only');

INSERT INTO role_permissions (role_id, permission_id) VALUES
    ('customer_owner', 'device.view'), ('customer_owner', 'device.operate'),
    ('customer_owner', 'device.share'), ('customer_owner', 'org.members'),
    ('customer_member', 'device.view'), ('customer_member', 'device.operate'),
    ('customer_viewer', 'device.view'),
    ('manufacturer_admin', 'org.members'),
    ('admin', 'device.view'), ('admin', 'device.operate'), ('admin', 'device.share'),
    ('admin', 'org.members');

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
    'device_shared', 'device_share_revoked', 'account_throttled'));
