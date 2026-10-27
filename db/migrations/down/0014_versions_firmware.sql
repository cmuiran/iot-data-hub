-- Rollback of 0014_versions_firmware (python -m hub.storage.migrate --rollback 0014_versions_firmware).
-- Refused while firmware releases exist (restore the backup instead). The auth_audit event
-- names stay (the audit log is append-only).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM firmware_releases) THEN
        RAISE EXCEPTION 'firmware releases exist; restore the pre-upgrade backup instead';
    END IF;
END
$$;

DROP TABLE firmware_jobs;
DROP TABLE firmware_releases;
ALTER TABLE platform_devices DROP COLUMN firmware_reported_at;
ALTER TABLE platform_devices DROP COLUMN firmware_version;
