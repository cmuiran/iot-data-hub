-- Rollback of 0013_automations (python -m hub.storage.migrate --rollback 0013_automations).
-- Drops scenes, automations, their run history and the holidays: take a backup first. The
-- permission automation.manage is removed from every role. The auth_audit event names stay
-- (the audit log is append-only).

DELETE FROM role_permissions WHERE permission_id = 'automation.manage';
DELETE FROM permissions WHERE permission_id = 'automation.manage';
DROP TABLE holidays;
DROP TABLE automation_runs;
DROP TABLE automations;
DROP TABLE scenes;
