-- 0009_platform_core: the platform plane (platform phase 1): organizations as tenants, products,
-- product template versions, platform device instances, device access, tenant isolation.
-- Rollback: db/migrations/down/0009_platform_core.sql (python -m hub.storage.migrate --rollback).
-- Design: docs/fa/platform/phase0-audit.md (ADR-001 .. ADR-004, ADR-010).
--
-- Platform devices are rows of `devices` / `tags` like the YAML ones (so history, alarms and
-- control work unchanged) with source = 'platform'; the YAML sync never touches them. Their
-- product, identifiers and lifecycle are in `platform_devices`.
--
-- Isolation: tenant requests run as the role hub_tenant (SET LOCAL ROLE in a transaction, with
-- app.org_ids = the caller's organizations). Row-level security on the platform tables lets
-- that role see and write only its organizations' rows; it has no privilege on any other table.
-- The application role (owner of the tables) is not subject to these policies (service code).
-- hub_tenant is a cluster-wide role: created here when the migrating role may create roles,
-- otherwise create it once as a superuser:  CREATE ROLE hub_tenant NOLOGIN;
--                                           GRANT hub_tenant TO hub_app;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_tenant') THEN
        CREATE ROLE hub_tenant NOLOGIN;
    END IF;
    IF NOT pg_has_role(current_user, 'hub_tenant', 'MEMBER') THEN
        EXECUTE format('GRANT hub_tenant TO %I', current_user);
    END IF;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE EXCEPTION 'role hub_tenant is missing or not granted to %: as a superuser run '
        'CREATE ROLE hub_tenant NOLOGIN; GRANT hub_tenant TO %;', current_user, current_user;
END
$$;

-- ---------------------------------------------------------------------------------------------
-- Organizations = tenants; config-sourced ones still mirror config/building.yaml
-- ---------------------------------------------------------------------------------------------

ALTER TABLE organizations ADD COLUMN kind text NOT NULL DEFAULT 'site_operator'
    CHECK (kind IN ('platform', 'manufacturer', 'customer', 'site_operator'));
ALTER TABLE organizations ADD COLUMN source text NOT NULL DEFAULT 'config'
    CHECK (source IN ('config', 'platform'));
ALTER TABLE organizations ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE devices ADD COLUMN source text NOT NULL DEFAULT 'config'
    CHECK (source IN ('config', 'platform'));
CREATE INDEX devices_source_idx ON devices (source) WHERE source = 'platform';

-- ---------------------------------------------------------------------------------------------
-- Products and template versions
-- ---------------------------------------------------------------------------------------------

CREATE TABLE products (
    product_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              text NOT NULL REFERENCES organizations (org_id),   -- the manufacturer
    code                text NOT NULL CHECK (code ~ '^[A-Z0-9][A-Z0-9-]{1,31}$'),
    name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    category            text,
    description         text,
    image_ref           text,
    identifier_pattern  text NOT NULL,            -- e.g. CTRL-8R-{0000} (hub/platform/devices.py)
    created_by          integer REFERENCES users (user_id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    retired_at          timestamptz,
    UNIQUE (org_id, code)
);

-- One template per version: tags + protocol bindings (hub/platform/template.py ProductTemplate).
-- A published version never changes; every change is a new version.
CREATE TABLE product_versions (
    product_id    uuid NOT NULL REFERENCES products (product_id),
    version       integer NOT NULL CHECK (version >= 1),
    status        text NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'published', 'deprecated')),
    definition    jsonb NOT NULL,
    notes         text,
    created_by    integer REFERENCES users (user_id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    published_by  integer REFERENCES users (user_id),
    published_at  timestamptz,
    PRIMARY KEY (product_id, version),
    CHECK ((status = 'draft') = (published_at IS NULL))
);

CREATE FUNCTION product_version_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'version % of product % is published and cannot be deleted',
                OLD.version, OLD.product_id;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'draft' AND (NEW.definition IS DISTINCT FROM OLD.definition
            OR NEW.product_id IS DISTINCT FROM OLD.product_id
            OR NEW.version IS DISTINCT FROM OLD.version
            OR NEW.status = 'draft') THEN
        RAISE EXCEPTION 'version % of product % is published: make a new version instead',
            OLD.version, OLD.product_id;
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER product_versions_frozen BEFORE UPDATE OR DELETE ON product_versions
    FOR EACH ROW EXECUTE FUNCTION product_version_frozen();

-- ---------------------------------------------------------------------------------------------
-- Platform device instances and access
-- ---------------------------------------------------------------------------------------------

CREATE TABLE platform_devices (
    device_id        text PRIMARY KEY REFERENCES devices (device_id)
                     DEFERRABLE INITIALLY DEFERRED,
    product_id       uuid NOT NULL,
    version          integer NOT NULL,
    maker_org_id     text NOT NULL REFERENCES organizations (org_id),
    identifier       text NOT NULL CHECK (identifier ~ '^[A-Z0-9][A-Z0-9-]{2,63}$'),
    serial_number    text CHECK (length(serial_number) BETWEEN 1 AND 64),
    batch            text CHECK (length(batch) BETWEEN 1 AND 64),
    lifecycle        text NOT NULL DEFAULT 'registered'
                     CHECK (lifecycle IN ('registered', 'ready', 'sold', 'active', 'disabled')),
    warranty_months  smallint CHECK (warranty_months BETWEEN 0 AND 240),
    options          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- device-level protocol options
    parent_device_id text REFERENCES platform_devices (device_id),  -- behind an edge hub
    registered_by    integer REFERENCES users (user_id),
    registered_at    timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    retired_at       timestamptz,
    FOREIGN KEY (product_id, version) REFERENCES product_versions (product_id, version)
);
-- the identifier a buyer types resolves to exactly one device, whoever made it
CREATE UNIQUE INDEX platform_devices_identifier_uq ON platform_devices (upper(identifier));
CREATE UNIQUE INDEX platform_devices_serial_uq ON platform_devices (product_id, serial_number)
    WHERE serial_number IS NOT NULL;
CREATE INDEX platform_devices_product_idx ON platform_devices (product_id, version);
CREATE INDEX platform_devices_maker_idx ON platform_devices (maker_org_id);

-- Which organizations may use a device. Owner = the buyer (at most one at a time).
CREATE TABLE device_access (
    access_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id   text NOT NULL REFERENCES platform_devices (device_id),
    org_id      text NOT NULL REFERENCES organizations (org_id),
    level       text NOT NULL CHECK (level IN ('owner', 'operator', 'viewer')),
    granted_by  integer REFERENCES users (user_id),
    granted_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);
CREATE UNIQUE INDEX device_access_active_uq ON device_access (device_id, org_id)
    WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX device_access_one_owner_uq ON device_access (device_id)
    WHERE level = 'owner' AND revoked_at IS NULL;
CREATE INDEX device_access_org_idx ON device_access (org_id) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------------------------------
-- Tenant isolation (row-level security for hub_tenant)
-- ---------------------------------------------------------------------------------------------

-- The caller's organizations, set per transaction: SELECT set_config('app.org_ids', 'a,b', true)
CREATE FUNCTION app_org_ids() RETURNS text[] LANGUAGE sql STABLE AS $$
    SELECT coalesce(string_to_array(nullif(current_setting('app.org_ids', true), ''), ','),
                    '{}'::text[])
$$;

-- Helpers run as the owner (no RLS inside), so policies do not recurse into each other.
CREATE FUNCTION app_device_maker_is_caller(p_device text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM platform_devices d
                   WHERE d.device_id = p_device AND d.maker_org_id = ANY(app_org_ids()))
$$;
CREATE FUNCTION app_device_used_by_caller(p_device text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM device_access a
                   WHERE a.device_id = p_device AND a.revoked_at IS NULL
                     AND a.org_id = ANY(app_org_ids()))
$$;
CREATE FUNCTION app_device_owned_by_caller(p_device text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM device_access a
                   WHERE a.device_id = p_device AND a.revoked_at IS NULL AND a.level = 'owner'
                     AND a.org_id = ANY(app_org_ids()))
$$;
CREATE FUNCTION app_product_made_by_caller(p_product uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM products p
                   WHERE p.product_id = p_product AND p.org_id = ANY(app_org_ids()))
$$;
CREATE FUNCTION app_product_used_by_caller(p_product uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT EXISTS (SELECT 1 FROM platform_devices d JOIN device_access a USING (device_id)
                   WHERE d.product_id = p_product AND a.revoked_at IS NULL
                     AND a.org_id = ANY(app_org_ids()))
$$;
REVOKE EXECUTE ON FUNCTION app_device_maker_is_caller(text), app_device_used_by_caller(text),
    app_device_owned_by_caller(text), app_product_made_by_caller(uuid),
    app_product_used_by_caller(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_org_ids(), app_device_maker_is_caller(text),
    app_device_used_by_caller(text), app_device_owned_by_caller(text),
    app_product_made_by_caller(uuid), app_product_used_by_caller(uuid) TO hub_tenant;

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE products ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_access ENABLE ROW LEVEL SECURITY;

-- (policies apply to hub_tenant only; the owner and other roles are not restricted by them)
CREATE POLICY tenant_read ON organizations FOR SELECT TO hub_tenant
    USING (org_id = ANY(app_org_ids()));

-- manufacturers see and change their products; buyers see the products of their devices
CREATE POLICY tenant_read ON products FOR SELECT TO hub_tenant
    USING (org_id = ANY(app_org_ids()) OR app_product_used_by_caller(product_id));
CREATE POLICY tenant_insert ON products FOR INSERT TO hub_tenant
    WITH CHECK (org_id = ANY(app_org_ids()));
CREATE POLICY tenant_update ON products FOR UPDATE TO hub_tenant
    USING (org_id = ANY(app_org_ids())) WITH CHECK (org_id = ANY(app_org_ids()));

CREATE POLICY tenant_read ON product_versions FOR SELECT TO hub_tenant
    USING (app_product_made_by_caller(product_id) OR app_product_used_by_caller(product_id));
CREATE POLICY tenant_insert ON product_versions FOR INSERT TO hub_tenant
    WITH CHECK (app_product_made_by_caller(product_id));
CREATE POLICY tenant_update ON product_versions FOR UPDATE TO hub_tenant
    USING (app_product_made_by_caller(product_id))
    WITH CHECK (app_product_made_by_caller(product_id));
CREATE POLICY tenant_delete ON product_versions FOR DELETE TO hub_tenant
    USING (app_product_made_by_caller(product_id));

-- maker and users of a device see it; only the maker registers devices of its own products
CREATE POLICY tenant_read ON platform_devices FOR SELECT TO hub_tenant
    USING (maker_org_id = ANY(app_org_ids()) OR app_device_used_by_caller(device_id));
CREATE POLICY tenant_insert ON platform_devices FOR INSERT TO hub_tenant
    WITH CHECK (maker_org_id = ANY(app_org_ids()) AND app_product_made_by_caller(product_id));
CREATE POLICY tenant_update ON platform_devices FOR UPDATE TO hub_tenant
    USING (maker_org_id = ANY(app_org_ids()))
    WITH CHECK (maker_org_id = ANY(app_org_ids()));

-- an organization sees its own access rows, and the owner sees every row of its device;
-- the maker does not see who uses its devices (privacy: platform_devices.lifecycle tells it
-- whether a device is in use)
CREATE POLICY tenant_read ON device_access FOR SELECT TO hub_tenant
    USING (org_id = ANY(app_org_ids()) OR app_device_owned_by_caller(device_id));

GRANT SELECT ON organizations TO hub_tenant;
GRANT SELECT, INSERT, UPDATE ON products TO hub_tenant;
GRANT SELECT, INSERT, UPDATE, DELETE ON product_versions TO hub_tenant;
GRANT SELECT, INSERT, UPDATE ON platform_devices TO hub_tenant;
GRANT SELECT ON device_access TO hub_tenant;

-- ---------------------------------------------------------------------------------------------
-- Permissions and roles (organization-scoped: user_roles.org_id)
-- ---------------------------------------------------------------------------------------------

INSERT INTO permissions (permission_id, description) VALUES
    ('org.manage',      'Create and change organizations (platform administration)'),
    ('product.read',    'See the products and devices of an organization'),
    ('product.manage',  'Define products and publish template versions'),
    ('device.register', 'Register manufactured devices of the organization''s products');

INSERT INTO roles (role_id, description) VALUES
    ('manufacturer_admin',   'Products, template versions and device registration'),
    ('production_operator',  'Register manufactured devices and print labels'),
    ('manufacturer_support', 'See products and devices (support)');

INSERT INTO role_permissions (role_id, permission_id) VALUES
    ('manufacturer_admin', 'product.read'), ('manufacturer_admin', 'product.manage'),
    ('manufacturer_admin', 'device.register'),
    ('production_operator', 'product.read'), ('production_operator', 'device.register'),
    ('manufacturer_support', 'product.read'),
    ('admin', 'org.manage'), ('admin', 'product.read'), ('admin', 'product.manage'),
    ('admin', 'device.register');

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
    'product_version_published', 'devices_registered', 'platform_device_updated'));
