-- Separate relational entity tables retain versioned JSON payloads while PostgreSQL enforces identities, relations and invariants.

CREATE TABLE users (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    login text GENERATED ALWAYS AS (payload->>'login') STORED NOT NULL UNIQUE,
    role text GENERATED ALWAYS AS (payload->>'role') STORED NOT NULL CHECK(role IN ('client','bank_employee')),
    CHECK(payload ? 'password_hash')
);

CREATE INDEX users_payload_idx ON users USING gin(payload jsonb_path_ops);

CREATE TABLE auth_sessions (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    user_id uuid GENERATED ALWAYS AS ((payload->>'user_id')::uuid) STORED NOT NULL REFERENCES users(id),
    CHECK(payload ? 'expires_at'), CHECK(jsonb_typeof(payload->'revoked')='boolean')
);

CREATE INDEX auth_sessions_payload_idx ON auth_sessions USING gin(payload jsonb_path_ops);

CREATE TABLE refresh_tokens (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    session_id uuid GENERATED ALWAYS AS ((payload->>'session_id')::uuid) STORED NOT NULL REFERENCES auth_sessions(id),
    token_hash text GENERATED ALWAYS AS (payload->>'token_hash') STORED NOT NULL UNIQUE,
    CHECK(length(token_hash)=64), CHECK(jsonb_typeof(payload->'used')='boolean')
);

CREATE INDEX refresh_tokens_payload_idx ON refresh_tokens USING gin(payload jsonb_path_ops);

CREATE TABLE companies (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    inn text GENERATED ALWAYS AS (payload->>'inn') STORED NOT NULL UNIQUE,
    version integer GENERATED ALWAYS AS ((payload->>'version')::integer) STORED NOT NULL CHECK(version>=1),
    CHECK((payload->>'legal_type'='legal_entity' AND inn ~ '^[0-9]{10}$') OR (payload->>'legal_type'='individual_entrepreneur' AND inn ~ '^[0-9]{12}$'))
);

CREATE INDEX companies_payload_idx ON companies USING gin(payload jsonb_path_ops);

CREATE TABLE company_members (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    user_id uuid GENERATED ALWAYS AS ((payload->>'user_id')::uuid) STORED NOT NULL REFERENCES users(id),
    company_id uuid GENERATED ALWAYS AS ((payload->>'company_id')::uuid) STORED NOT NULL REFERENCES companies(id),
    role text GENERATED ALWAYS AS (payload->>'role') STORED NOT NULL CHECK(role IN ('owner','representative')),
    UNIQUE(user_id,company_id)
);

CREATE INDEX company_members_payload_idx ON company_members USING gin(payload jsonb_path_ops);

CREATE TABLE loan_products (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    code text GENERATED ALWAYS AS (payload->>'code') STORED NOT NULL UNIQUE,
    min_amount numeric(17,2) GENERATED ALWAYS AS ((payload->>'min_amount')::numeric) STORED NOT NULL,
    max_amount numeric(17,2) GENERATED ALWAYS AS ((payload->>'max_amount')::numeric) STORED NOT NULL,
    min_term integer GENERATED ALWAYS AS ((payload->>'min_term_months')::integer) STORED NOT NULL,
    max_term integer GENERATED ALWAYS AS ((payload->>'max_term_months')::integer) STORED NOT NULL,
    CHECK(min_amount>0 AND max_amount>=min_amount), CHECK(min_term>0 AND max_term>=min_term), CHECK(payload->>'currency'='RUB')
);

CREATE INDEX loan_products_payload_idx ON loan_products USING gin(payload jsonb_path_ops);

CREATE TABLE applications (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    company_id uuid GENERATED ALWAYS AS ((payload->>'company_id')::uuid) STORED NOT NULL REFERENCES companies(id),
    status text GENERATED ALWAYS AS (payload->>'status') STORED NOT NULL CHECK(status IN ('Draft','Submitted','Checking','Scoring','NeedsInfo','ManualReview','Offered','Rejected','Accepted','Declined','Signed','Disbursed')),
    version integer GENERATED ALWAYS AS ((payload->>'version')::integer) STORED NOT NULL CHECK(version>=1),
    current_snapshot_id uuid GENERATED ALWAYS AS ((payload->>'current_snapshot_id')::uuid) STORED,
    UNIQUE(id,company_id)
);

CREATE INDEX applications_payload_idx ON applications USING gin(payload jsonb_path_ops);

CREATE TABLE documents (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    s3_key text GENERATED ALWAYS AS (payload->>'s3_key') STORED NOT NULL UNIQUE,
    size_bytes integer GENERATED ALWAYS AS ((payload->>'size_bytes')::integer) STORED NOT NULL CHECK(size_bytes BETWEEN 1 AND 10485760),
    UNIQUE(id,application_id)
);

CREATE INDEX documents_payload_idx ON documents USING gin(payload jsonb_path_ops);

CREATE TABLE upload_reservations (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    s3_key text GENERATED ALWAYS AS (payload->>'s3_key') STORED NOT NULL UNIQUE
);

CREATE INDEX upload_reservations_payload_idx ON upload_reservations USING gin(payload jsonb_path_ops);

CREATE TABLE application_snapshots (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    revision integer GENERATED ALWAYS AS ((payload->>'revision')::integer) STORED NOT NULL CHECK(revision>=1),
    UNIQUE(application_id,revision), UNIQUE(id,application_id), CHECK(jsonb_typeof(payload->'document_ids')='array')
);

CREATE INDEX application_snapshots_payload_idx ON application_snapshots USING gin(payload jsonb_path_ops);

CREATE TABLE information_requests (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    snapshot_id uuid GENERATED ALWAYS AS ((payload->>'snapshot_id')::uuid) STORED NOT NULL,
    status text GENERATED ALWAYS AS (payload->>'status') STORED NOT NULL CHECK(status IN ('open','answered')),
    FOREIGN KEY(snapshot_id,application_id) REFERENCES application_snapshots(id,application_id)
);

CREATE INDEX information_requests_payload_idx ON information_requests USING gin(payload jsonb_path_ops);

CREATE TABLE check_runs (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    snapshot_id uuid GENERATED ALWAYS AS ((payload->>'snapshot_id')::uuid) STORED NOT NULL,
    source text GENERATED ALWAYS AS (payload->>'source') STORED NOT NULL CHECK(source IN ('fns','bki')),
    attempt integer GENERATED ALWAYS AS ((payload->>'attempt')::integer) STORED NOT NULL CHECK(attempt>=1),
    status text GENERATED ALWAYS AS (payload->>'status') STORED NOT NULL CHECK(status IN ('pending','running','succeeded','failed','needs_info')),
    FOREIGN KEY(snapshot_id,application_id) REFERENCES application_snapshots(id,application_id), UNIQUE(snapshot_id,source,attempt)
);

CREATE INDEX check_runs_payload_idx ON check_runs USING gin(payload jsonb_path_ops);

CREATE TABLE scoring_results (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    snapshot_id uuid GENERATED ALWAYS AS ((payload->>'snapshot_id')::uuid) STORED NOT NULL UNIQUE,
    request_id uuid GENERATED ALWAYS AS ((payload->>'request_id')::uuid) STORED NOT NULL UNIQUE,
    FOREIGN KEY(snapshot_id,application_id) REFERENCES application_snapshots(id,application_id),
    CHECK(payload->>'model_version'='stub-v1'), CHECK((payload->>'score')::integer BETWEEN 0 AND 100)
);

CREATE INDEX scoring_results_payload_idx ON scoring_results USING gin(payload jsonb_path_ops);

CREATE TABLE credit_decisions (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    snapshot_id uuid GENERATED ALWAYS AS ((payload->>'snapshot_id')::uuid) STORED NOT NULL,
    author_id uuid GENERATED ALWAYS AS ((payload->>'author_id')::uuid) STORED NOT NULL REFERENCES users(id),
    FOREIGN KEY(snapshot_id,application_id) REFERENCES application_snapshots(id,application_id),
    CHECK(payload->>'source'='manual'), CHECK(payload->>'outcome' IN ('offer','reject')), UNIQUE(id,application_id)
);

CREATE INDEX credit_decisions_payload_idx ON credit_decisions USING gin(payload jsonb_path_ops);

CREATE TABLE offers (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    decision_id uuid GENERATED ALWAYS AS ((payload->>'decision_id')::uuid) STORED NOT NULL UNIQUE,
    status text GENERATED ALWAYS AS (payload->>'status') STORED NOT NULL CHECK(status IN ('pending','accepted','declined')),
    FOREIGN KEY(decision_id,application_id) REFERENCES credit_decisions(id,application_id)
);

CREATE INDEX offers_payload_idx ON offers USING gin(payload jsonb_path_ops);

CREATE TABLE application_events (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id)
);

CREATE INDEX application_events_payload_idx ON application_events USING gin(payload jsonb_path_ops);

CREATE TABLE audit_events (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    actor_id uuid GENERATED ALWAYS AS ((payload->>'actor_id')::uuid) STORED REFERENCES users(id),
    CHECK(payload->>'result' IN ('success','denied','failure'))
);

CREATE INDEX audit_events_payload_idx ON audit_events USING gin(payload jsonb_path_ops);

CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload ? 'id' AND (payload->>'id')::uuid=id),
    application_id uuid GENERATED ALWAYS AS ((payload->>'application_id')::uuid) STORED NOT NULL REFERENCES applications(id),
    snapshot_id uuid GENERATED ALWAYS AS ((payload->>'snapshot_id')::uuid) STORED NOT NULL,
    source text GENERATED ALWAYS AS (payload->>'source') STORED NOT NULL CHECK(source IN ('fns','bki','scoring','disbursement')),
    status text GENERATED ALWAYS AS (payload->>'status') STORED NOT NULL CHECK(status IN ('queued','running','blocked','done')),
    run_at timestamptz,
    lease_until timestamptz,
    FOREIGN KEY(snapshot_id,application_id) REFERENCES application_snapshots(id,application_id), UNIQUE(snapshot_id,source),
    CHECK((payload->>'attempt')::integer>=0), CHECK((payload->>'cycle_attempt')::integer>=0)
);

CREATE INDEX jobs_payload_idx ON jobs USING gin(payload jsonb_path_ops);

ALTER TABLE applications ADD CONSTRAINT applications_current_snapshot_fk FOREIGN KEY(current_snapshot_id,id) REFERENCES application_snapshots(id,application_id) DEFERRABLE INITIALLY DEFERRED;
CREATE UNIQUE INDEX refresh_tokens_one_current ON refresh_tokens(session_id) WHERE (payload->>'used')::boolean=false;
CREATE UNIQUE INDEX information_requests_one_open ON information_requests(application_id) WHERE status='open';
CREATE UNIQUE INDEX offers_one_pending ON offers(application_id) WHERE status='pending';
CREATE INDEX applications_company_status_idx ON applications(company_id,status);
CREATE INDEX jobs_due_idx ON jobs(run_at,lease_until) WHERE status IN ('queued','running');
CREATE INDEX check_runs_attempt_idx ON check_runs(snapshot_id,source,attempt DESC);

CREATE TABLE snapshot_documents (
    snapshot_id uuid NOT NULL,
    application_id uuid NOT NULL,
    document_id uuid NOT NULL,
    PRIMARY KEY(snapshot_id,document_id),
    FOREIGN KEY(snapshot_id,application_id) REFERENCES application_snapshots(id,application_id),
    FOREIGN KEY(document_id,application_id) REFERENCES documents(id,application_id)
);

CREATE FUNCTION protect_immutable_row() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Immutable historical record in %', TG_TABLE_NAME USING ERRCODE='23514';
END $$;
CREATE TRIGGER application_snapshots_immutable BEFORE UPDATE OR DELETE ON application_snapshots FOR EACH ROW EXECUTE FUNCTION protect_immutable_row();
CREATE TRIGGER snapshot_documents_immutable BEFORE UPDATE OR DELETE ON snapshot_documents FOR EACH ROW EXECUTE FUNCTION protect_immutable_row();
CREATE TRIGGER credit_decisions_immutable BEFORE UPDATE OR DELETE ON credit_decisions FOR EACH ROW EXECUTE FUNCTION protect_immutable_row();
CREATE TRIGGER scoring_results_immutable BEFORE UPDATE OR DELETE ON scoring_results FOR EACH ROW EXECUTE FUNCTION protect_immutable_row();
CREATE TRIGGER application_events_immutable BEFORE UPDATE OR DELETE ON application_events FOR EACH ROW EXECUTE FUNCTION protect_immutable_row();
CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION protect_immutable_row();

CREATE FUNCTION link_snapshot_documents() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE doc_id text;
BEGIN
    FOR doc_id IN SELECT jsonb_array_elements_text(NEW.payload->'document_ids') LOOP
        IF EXISTS(SELECT 1 FROM documents WHERE id=doc_id::uuid AND payload->>'deleted_at' IS NOT NULL) THEN
            RAISE EXCEPTION 'Deleted document cannot enter a snapshot' USING ERRCODE='23514';
        END IF;
        INSERT INTO snapshot_documents(snapshot_id,application_id,document_id)
        VALUES(NEW.id,NEW.application_id,doc_id::uuid);
    END LOOP;
    RETURN NEW;
END $$;
CREATE TRIGGER application_snapshots_link_documents AFTER INSERT ON application_snapshots FOR EACH ROW EXECUTE FUNCTION link_snapshot_documents();

CREATE FUNCTION protect_snapshot_document() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM snapshot_documents WHERE document_id=OLD.id) THEN
        RAISE EXCEPTION 'Document is part of an immutable snapshot' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER documents_immutable_when_linked BEFORE UPDATE OR DELETE ON documents FOR EACH ROW EXECUTE FUNCTION protect_snapshot_document();

CREATE FUNCTION project_job_times() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.run_at := (NEW.payload->>'run_at')::timestamptz;
    NEW.lease_until := (NEW.payload->>'lease_until')::timestamptz;
    RETURN NEW;
END $$;
CREATE TRIGGER jobs_project_times BEFORE INSERT OR UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION project_job_times();

