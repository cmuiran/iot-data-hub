-- Rollback of 0009_platform_core (python -m hub.storage.migrate --rollback 0009_platform_core).
-- Refused (nothing changes) while platform devices or platform organizations exist (their
-- history refers to them: restore the pre-upgrade backup instead), or while users hold the
-- manufacturer roles (revoke them first). Products and template versions without devices are
-- dropped: take a backup first.
--
-- Kept on purpose: the auth_audit event names (the audit log is append-only) and the
-- cluster-wide role hub_tenant (other databases of the cluster may use it).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM platform_devices) OR EXISTS (SELECT 1 FROM devices
                                                          WHERE source = 'platform') THEN
        RAISE EXCEPTION 'platform devices exist; restore the pre-upgrade backup instead';
    END IF;
    IF EXISTS (SELECT 1 FROM organizations WHERE source = 'platform') THEN
        RAISE EXCEPTION 'platform organizations exist; restore the pre-upgrade backup instead';
    END IF;
    IF EXISTS (SELECT 1 FROM user_roles WHERE role_id IN
               ('manufacturer_admin', 'production_operator', 'manufacturer_support')) THEN
        RAISE EXCEPTION 'users still hold manufacturer roles; revoke them first';
    END IF;
END
$$;

DELETE FROM role_permissions WHERE permission_id IN
    ('org.manage', 'product.read', 'product.manage', 'device.register');
DELETE FROM role_permissions WHERE role_id IN
    ('manufacturer_admin', 'production_operator', 'manufacturer_support');
DELETE FROM permissions WHERE permission_id IN
    ('org.manage', 'product.read', 'product.manage', 'device.register');
DELETE FROM roles WHERE role_id IN
    ('manufacturer_admin', 'production_operator', 'manufacturer_support');

REVOKE ALL ON organizations FROM hub_tenant;
DROP POLICY tenant_read ON organizations;
ALTER TABLE organizations DISABLE ROW LEVEL SECURITY;

DROP TABLE device_access;
DROP TABLE platform_devices;
DROP TABLE product_versions;
DROP FUNCTION product_version_frozen();
DROP TABLE products;
DROP FUNCTION app_device_maker_is_caller(text);
DROP FUNCTION app_device_used_by_caller(text);
DROP FUNCTION app_device_owned_by_caller(text);
DROP FUNCTION app_product_made_by_caller(uuid);
DROP FUNCTION app_product_used_by_caller(uuid);
DROP FUNCTION app_org_ids();

DROP INDEX devices_source_idx;
ALTER TABLE devices DROP COLUMN source;
ALTER TABLE organizations DROP COLUMN created_at;
ALTER TABLE organizations DROP COLUMN source;
ALTER TABLE organizations DROP COLUMN kind;
