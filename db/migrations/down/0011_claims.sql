-- Rollback of 0011_claims (python -m hub.storage.migrate --rollback 0011_claims).
-- Refused while claim codes, device credentials, edge-hub children or catalog / edge-hub
-- products exist: restore the pre-upgrade backup instead.
-- Kept on purpose: the auth_audit event names (the audit log is append-only).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM device_claim_codes) OR EXISTS (SELECT 1 FROM device_credentials)
    THEN
        RAISE EXCEPTION 'claim codes or device credentials exist; restore the backup instead';
    END IF;
    IF EXISTS (SELECT 1 FROM products WHERE kind <> 'device' OR catalog) THEN
        RAISE EXCEPTION 'edge-hub or catalog products exist; restore the backup instead';
    END IF;
    IF EXISTS (SELECT 1 FROM platform_devices WHERE parent_device_id IS NOT NULL) THEN
        RAISE EXCEPTION 'devices behind edge hubs exist; restore the backup instead';
    END IF;
END
$$;

DROP POLICY tenant_read ON product_versions;
CREATE POLICY tenant_read ON product_versions FOR SELECT TO hub_tenant
    USING (app_product_made_by_caller(product_id) OR app_product_used_by_caller(product_id));
DROP POLICY tenant_read ON products;
CREATE POLICY tenant_read ON products FOR SELECT TO hub_tenant
    USING (org_id = ANY(app_org_ids()) OR app_product_used_by_caller(product_id));
DROP FUNCTION app_product_in_catalog(uuid);

DROP TABLE device_credentials;
DROP TABLE device_claim_codes;
ALTER TABLE products DROP COLUMN catalog;
ALTER TABLE products DROP COLUMN kind;
