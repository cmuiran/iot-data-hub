-- 0013_automations: scenes and automations of an organization (platform phase 6), their run
-- history, and holidays (Jalali calendar). Rollback: db/migrations/down/0013_automations.sql.
--
-- An automation = trigger (time / interval / once on the Jalali calendar, a value, a device
-- going offline) + conditions + actions (writes through the control-safety layer as its
-- creator, scenes, delays, notifications). A run is recorded once per trigger occurrence:
-- UNIQUE (automation_id, trigger_key) keeps two nodes or a restart from running it twice.

CREATE TABLE scenes (
    scene_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      text NOT NULL REFERENCES organizations (org_id),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    icon        text,
    actions     jsonb NOT NULL,          -- backend.automations.Action list
    created_by  integer REFERENCES users (user_id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX scenes_org_idx ON scenes (org_id);

CREATE TABLE automations (
    automation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id        text NOT NULL REFERENCES organizations (org_id),
    name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    enabled       boolean NOT NULL DEFAULT true,
    definition    jsonb NOT NULL,        -- trigger, conditions, window, actions, policies
    created_by    integer NOT NULL REFERENCES users (user_id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_fired_at timestamptz            -- the last trigger occurrence handled
);
CREATE INDEX automations_org_idx ON automations (org_id);

CREATE TABLE automation_runs (
    run_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    automation_id bigint NOT NULL REFERENCES automations (automation_id) ON DELETE CASCADE,
    trigger_key   text NOT NULL,         -- the occurrence: time ISO, value edge, test, ...
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'ok', 'partial', 'failed', 'skipped')),
    detail        jsonb NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (automation_id, trigger_key)
);
CREATE INDEX automation_runs_idx ON automation_runs (automation_id, started_at DESC);

-- Holidays besides the fixed solar ones (hub.jalali): the lunar holidays of each year, and an
-- organization's own days off. org_id NULL = every organization (platform administrators).
CREATE TABLE holidays (
    holiday_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      text REFERENCES organizations (org_id),
    day         date NOT NULL,
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    created_by  integer REFERENCES users (user_id),
    UNIQUE (org_id, day)
);
CREATE UNIQUE INDEX holidays_global_uq ON holidays (day) WHERE org_id IS NULL;

ALTER TABLE scenes ENABLE ROW LEVEL SECURITY;
ALTER TABLE automations ENABLE ROW LEVEL SECURITY;
ALTER TABLE automation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE holidays ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_all ON scenes FOR ALL TO hub_tenant
    USING (org_id = ANY(app_org_ids())) WITH CHECK (org_id = ANY(app_org_ids()));
CREATE POLICY tenant_all ON automations FOR ALL TO hub_tenant
    USING (org_id = ANY(app_org_ids())) WITH CHECK (org_id = ANY(app_org_ids()));
CREATE POLICY tenant_read ON automation_runs FOR SELECT TO hub_tenant
    USING (EXISTS (SELECT 1 FROM automations a WHERE a.automation_id =
                   automation_runs.automation_id AND a.org_id = ANY(app_org_ids())));
CREATE POLICY tenant_read ON holidays FOR SELECT TO hub_tenant
    USING (org_id IS NULL OR org_id = ANY(app_org_ids()));
CREATE POLICY tenant_write ON holidays FOR INSERT TO hub_tenant
    WITH CHECK (org_id = ANY(app_org_ids()));
CREATE POLICY tenant_delete ON holidays FOR DELETE TO hub_tenant
    USING (org_id = ANY(app_org_ids()));
GRANT SELECT, INSERT, UPDATE, DELETE ON scenes, automations TO hub_tenant;
GRANT SELECT ON automation_runs TO hub_tenant;
GRANT SELECT, INSERT, DELETE ON holidays TO hub_tenant;
GRANT USAGE ON SEQUENCE scenes_scene_id_seq, automations_automation_id_seq,
    holidays_holiday_id_seq TO hub_tenant;

INSERT INTO permissions (permission_id, description) VALUES
    ('automation.manage', 'Create and change the scenes and automations of an organization');
INSERT INTO role_permissions (role_id, permission_id) VALUES
    ('customer_owner', 'automation.manage'), ('customer_member', 'automation.manage'),
    ('admin', 'automation.manage');

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
    'device_transfer_started', 'device_reset', 'child_device_added', 'child_device_removed',
    'device_auth_failed', 'automation_saved', 'automation_deleted', 'scene_saved',
    'scene_deleted', 'scene_run', 'holiday_saved'));
