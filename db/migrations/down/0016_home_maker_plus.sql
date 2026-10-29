-- Rollback of 0016_home_maker_plus (python -m hub.storage.migrate --rollback 0016_home_maker_plus).
-- Refused while tickets or invoices exist (restore the backup instead). The auth_audit event
-- names stay (the audit log is append-only).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM tickets) OR EXISTS (SELECT 1 FROM invoices) THEN
        RAISE EXCEPTION 'tickets or invoices exist; restore the pre-upgrade backup instead';
    END IF;
END
$$;

DROP TABLE invoices;
DROP TABLE org_plans;
DROP TABLE plans;
DROP TABLE org_branding;
DROP TABLE ticket_messages;
DROP TABLE tickets;
DROP TABLE device_sms;
DROP TABLE home_settings;
DROP TABLE output_labels;
DROP TABLE device_groups;
