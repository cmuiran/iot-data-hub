-- Rollback of 0017_switches_sms (python -m hub.storage.migrate --rollback 0017_switches_sms).
-- The auth_audit event names stay (the audit log is append-only).

ALTER TABLE home_settings DROP COLUMN sms_pin_hash;
DROP INDEX scenes_keyword_idx;
ALTER TABLE scenes DROP COLUMN sms_keyword;
DROP TABLE home_contacts;
ALTER TABLE output_labels DROP COLUMN pulse_s, DROP COLUMN mode;
