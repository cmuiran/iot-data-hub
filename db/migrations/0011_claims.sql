-- 0011_claims: claiming devices (platform phase 3): activation and transfer codes, per-device
-- credentials, edge hubs with child devices from a product catalog.
-- Rollback: db/migrations/down/0011_claims.sql.
--
-- A claim code is printed on the label (QR) and typed by the buyer together with the device
-- identifier: 60 random bits, stored as an HMAC with a server-side key (a database copy alone
-- does not allow guessing it offline), works once, and a device locks for a while after
-- repeated wrong codes. Knowing the identifier alone gives
-- nothing. A device credential is a long random secret for the device itself (firmware, MQTT /
-- HTTP / TCP sign-in from phase 4); only its SHA-256 is stored. Neither table is visible to the
-- tenant role: only service code (backend.claims) reads them.

ALTER TABLE products ADD COLUMN kind text NOT NULL DEFAULT 'device'
    CHECK (kind IN ('device', 'edge_hub'));
-- catalog products are templates any buyer may add behind its edge hub (third-party equipment)
ALTER TABLE products ADD COLUMN catalog boolean NOT NULL DEFAULT false;

CREATE TABLE device_claim_codes (
    device_id        text PRIMARY KEY REFERENCES platform_devices (device_id),
    code_hash        text NOT NULL,      -- HMAC-SHA256 with the hub's data key (backend.claims)
    purpose          text NOT NULL CHECK (purpose IN ('activation', 'transfer')),
    created_by       integer REFERENCES users (user_id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    failed_attempts  smallint NOT NULL DEFAULT 0,
    locked_until     timestamptz,
    used_at          timestamptz
);

CREATE TABLE device_credentials (
    credential_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id      text NOT NULL REFERENCES platform_devices (device_id),
    secret_hash    text NOT NULL UNIQUE CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
    created_by     integer REFERENCES users (user_id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    revoked_at     timestamptz
);
CREATE UNIQUE INDEX device_credentials_active_uq ON device_credentials (device_id)
    WHERE revoked_at IS NULL;

-- no policy and no privilege: hub_tenant can never read codes or credentials
ALTER TABLE device_claim_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_credentials ENABLE ROW LEVEL SECURITY;

-- catalog products (and their published versions) are readable by every tenant
CREATE FUNCTION app_product_in_catalog(p_product uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM products p WHERE p.product_id = p_product AND p.catalog
                   AND p.retired_at IS NULL)
$$;
REVOKE EXECUTE ON FUNCTION app_product_in_catalog(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_product_in_catalog(uuid) TO hub_tenant;

DROP POLICY tenant_read ON products;
CREATE POLICY tenant_read ON products FOR SELECT TO hub_tenant
    USING (org_id = ANY(app_org_ids()) OR app_product_used_by_caller(product_id)
           OR (catalog AND retired_at IS NULL));
DROP POLICY tenant_read ON product_versions;
CREATE POLICY tenant_read ON product_versions FOR SELECT TO hub_tenant
    USING (app_product_made_by_caller(product_id) OR app_product_used_by_caller(product_id)
           OR (status = 'published' AND app_product_in_catalog(product_id)));

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
    'device_transfer_started', 'device_reset', 'child_device_added', 'child_device_removed'));
