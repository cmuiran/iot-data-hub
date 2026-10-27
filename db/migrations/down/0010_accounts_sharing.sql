-- Rollback of 0010_accounts_sharing (python -m hub.storage.migrate --rollback 0010_accounts_sharing).
-- Refused (nothing changes) while self-registered accounts, personal organizations, customer
-- role grants or device shares exist: restore the pre-upgrade backup instead.
-- Kept on purpose: the auth_audit event names (the audit log is append-only).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM users WHERE email IS NOT NULL) THEN
        RAISE EXCEPTION 'accounts with an e-mail address exist; restore the pre-upgrade backup';
    END IF;
    IF EXISTS (SELECT 1 FROM organizations WHERE personal_user_id IS NOT NULL) THEN
        RAISE EXCEPTION 'personal organizations exist; restore the pre-upgrade backup';
    END IF;
    IF EXISTS (SELECT 1 FROM user_roles WHERE role_id IN
               ('customer_owner', 'customer_member', 'customer_viewer')) THEN
        RAISE EXCEPTION 'users hold customer roles; revoke them first';
    END IF;
    IF EXISTS (SELECT 1 FROM device_access WHERE level <> 'owner') THEN
        RAISE EXCEPTION 'devices are shared; restore the pre-upgrade backup';
    END IF;
END
$$;

DELETE FROM role_permissions WHERE permission_id IN
    ('device.view', 'device.operate', 'device.share', 'org.members');
DELETE FROM role_permissions WHERE role_id IN
    ('customer_owner', 'customer_member', 'customer_viewer');
DELETE FROM permissions WHERE permission_id IN
    ('device.view', 'device.operate', 'device.share', 'org.members');
DELETE FROM roles WHERE role_id IN ('customer_owner', 'customer_member', 'customer_viewer');

REVOKE UPDATE (revoked_at) ON device_access FROM hub_tenant;
DROP POLICY tenant_update ON device_access;

DROP TABLE invitations;
DROP TABLE account_tokens;
ALTER TABLE organizations DROP COLUMN personal_user_id;
DROP INDEX users_email_uq;
ALTER TABLE users DROP COLUMN email_verified_at;
ALTER TABLE users DROP COLUMN email;
