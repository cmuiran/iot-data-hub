-- 0007_directory_users: users that sign in with Active Directory / LDAP (phase 7).
-- auth_source 'ldap': no local password (password_hash holds an unusable marker), roles come
-- from the directory groups on every sign-in, external_dn is the user's distinguished name.

ALTER TABLE users ADD COLUMN auth_source text NOT NULL DEFAULT 'local'
    CHECK (auth_source IN ('local', 'ldap'));
ALTER TABLE users ADD COLUMN external_dn text;

-- directory sign-in events: roles taken over from the directory groups, directory unreachable
ALTER TABLE auth_audit DROP CONSTRAINT auth_audit_event_check;
ALTER TABLE auth_audit ADD CONSTRAINT auth_audit_event_check CHECK (event IN (
    'login_ok', 'login_failed', 'login_throttled', 'logout', 'locked', 'unlocked',
    'password_changed', 'password_expired', 'user_created', 'user_updated', 'user_disabled',
    'role_granted', 'role_revoked', 'session_revoked', 'session_expired', 'alarm_acked',
    'alarm_shelved', 'alarm_unshelved', 'write_confirmed', 'write_rejected', 'config_changed',
    'schedule_created', 'schedule_updated', 'schedule_deleted', 'opcua_login', 'mfa_enabled',
    'mfa_disabled', 'mfa_failed', 'mfa_reset', 'network_denied', 'roles_synced',
    'directory_unavailable'));
