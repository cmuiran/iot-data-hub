-- 0004_security: MFA (TOTP), password history/expiry, idle sessions, tamper-evident audit.

-- ---------------------------------------------------------------------------------------------
-- Users: TOTP and password lifecycle. Secrets are encrypted by the application (HUB_DATA_KEY);
-- recovery codes and old passwords are stored as hashes only.
-- ---------------------------------------------------------------------------------------------

ALTER TABLE users
    ADD COLUMN totp_secret_enc     text,
    ADD COLUMN totp_pending_enc    text,
    ADD COLUMN totp_enabled        boolean NOT NULL DEFAULT false,
    ADD COLUMN totp_last_step      bigint,
    ADD COLUMN recovery_codes      jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN password_changed_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN password_history    jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE sessions
    ADD COLUMN last_seen_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN mfa          boolean NOT NULL DEFAULT false;

ALTER TABLE auth_audit DROP CONSTRAINT auth_audit_event_check;
ALTER TABLE auth_audit ADD CONSTRAINT auth_audit_event_check CHECK (event IN (
    'login_ok', 'login_failed', 'login_throttled', 'logout', 'locked', 'unlocked',
    'password_changed', 'password_expired', 'user_created', 'user_updated', 'user_disabled',
    'role_granted', 'role_revoked', 'session_revoked', 'session_expired', 'alarm_acked',
    'alarm_shelved', 'alarm_unshelved', 'write_confirmed', 'write_rejected', 'config_changed',
    'schedule_created', 'schedule_updated', 'schedule_deleted', 'opcua_login', 'mfa_enabled',
    'mfa_disabled', 'mfa_failed', 'mfa_reset', 'network_denied'));

-- ---------------------------------------------------------------------------------------------
-- Tamper-evident audit: every new row carries chain_seq, prev_hash and
--   row_hash = sha256(prev_hash || '|' || row as jsonb without the chain columns)
-- computed under a per-table lock, so the rows form one ordered chain. Changing or deleting a
-- row (even as a database superuser with the append-only triggers disabled) breaks the chain;
-- audit_verify() finds the first broken link. Rows written before this migration are not part
-- of the chain. The hub records the chain head daily (system_events 'audit_anchor') so that a
-- re-computed chain can be told apart from the original.
-- ---------------------------------------------------------------------------------------------

ALTER TABLE write_audit ADD COLUMN chain_seq bigint, ADD COLUMN prev_hash text,
                        ADD COLUMN row_hash text;
ALTER TABLE auth_audit  ADD COLUMN chain_seq bigint, ADD COLUMN prev_hash text,
                        ADD COLUMN row_hash text;
CREATE UNIQUE INDEX write_audit_chain_uq ON write_audit (chain_seq);
CREATE UNIQUE INDEX auth_audit_chain_uq ON auth_audit (chain_seq);

CREATE FUNCTION audit_chain_payload(row_data jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT (row_data - 'chain_seq' - 'prev_hash' - 'row_hash')::text
$$;

CREATE FUNCTION audit_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    last_seq  bigint;
    last_hash text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('audit_chain:' || TG_TABLE_NAME));
    EXECUTE format('SELECT chain_seq, row_hash FROM %I WHERE chain_seq IS NOT NULL '
                   'ORDER BY chain_seq DESC LIMIT 1', TG_TABLE_NAME)
        INTO last_seq, last_hash;
    NEW.chain_seq := coalesce(last_seq, 0) + 1;
    NEW.prev_hash := coalesce(last_hash, 'genesis');
    NEW.row_hash := encode(sha256(convert_to(
        NEW.prev_hash || '|' || audit_chain_payload(to_jsonb(NEW)), 'UTF8')), 'hex');
    RETURN NEW;
END;
$$;

CREATE TRIGGER write_audit_chain BEFORE INSERT ON write_audit
    FOR EACH ROW EXECUTE FUNCTION audit_chain();
CREATE TRIGGER auth_audit_chain BEFORE INSERT ON auth_audit
    FOR EACH ROW EXECUTE FUNCTION audit_chain();

-- Walks the chain in order; returns the number of rows checked and the first bad row (if any).
CREATE FUNCTION audit_verify(tbl text, OUT checked bigint, OUT first_bad_id bigint,
                             OUT problem text, OUT head_hash text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r        record;
    expected text := 'genesis';
    seq      bigint := 0;
BEGIN
    IF tbl NOT IN ('write_audit', 'auth_audit') THEN
        RAISE EXCEPTION 'not an audit table: %', tbl;
    END IF;
    checked := 0;
    FOR r IN EXECUTE format('SELECT id, chain_seq, prev_hash, row_hash, to_jsonb(t) AS j '
                            'FROM %I t WHERE chain_seq IS NOT NULL ORDER BY chain_seq', tbl)
    LOOP
        seq := seq + 1;
        -- head_hash = last verified row, so an anchor still marks the good part of the chain
        IF r.chain_seq <> seq THEN
            first_bad_id := r.id; problem := 'missing row before this one (gap in chain_seq)';
            head_hash := CASE WHEN checked > 0 THEN expected END;
            RETURN;
        END IF;
        IF r.prev_hash <> expected THEN
            first_bad_id := r.id; problem := 'prev_hash does not match the previous row';
            head_hash := CASE WHEN checked > 0 THEN expected END;
            RETURN;
        END IF;
        IF r.row_hash <> encode(sha256(convert_to(
               r.prev_hash || '|' || audit_chain_payload(r.j), 'UTF8')), 'hex') THEN
            first_bad_id := r.id; problem := 'row content was changed';
            head_hash := CASE WHEN checked > 0 THEN expected END;
            RETURN;
        END IF;
        expected := r.row_hash;
        checked := checked + 1;
    END LOOP;
    head_hash := CASE WHEN checked > 0 THEN expected END;
END;
$$;
