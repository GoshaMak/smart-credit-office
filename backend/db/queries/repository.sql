-- name: GetUsers :one
SELECT payload FROM users WHERE id=sqlc.arg(id);
-- name: LockUsers :one
SELECT payload FROM users WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListUsers :many
SELECT payload FROM users WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertUsers :one
INSERT INTO users(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveUsers :one
UPDATE users SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteUsers :exec
DELETE FROM users WHERE id=sqlc.arg(id);

-- name: GetAuthSessions :one
SELECT payload FROM auth_sessions WHERE id=sqlc.arg(id);
-- name: LockAuthSessions :one
SELECT payload FROM auth_sessions WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListAuthSessions :many
SELECT payload FROM auth_sessions WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertAuthSessions :one
INSERT INTO auth_sessions(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveAuthSessions :one
UPDATE auth_sessions SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteAuthSessions :exec
DELETE FROM auth_sessions WHERE id=sqlc.arg(id);

-- name: GetRefreshTokens :one
SELECT payload FROM refresh_tokens WHERE id=sqlc.arg(id);
-- name: LockRefreshTokens :one
SELECT payload FROM refresh_tokens WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListRefreshTokens :many
SELECT payload FROM refresh_tokens WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertRefreshTokens :one
INSERT INTO refresh_tokens(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveRefreshTokens :one
UPDATE refresh_tokens SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteRefreshTokens :exec
DELETE FROM refresh_tokens WHERE id=sqlc.arg(id);

-- name: GetCompanies :one
SELECT payload FROM companies WHERE id=sqlc.arg(id);
-- name: LockCompanies :one
SELECT payload FROM companies WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListCompanies :many
SELECT payload FROM companies WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertCompanies :one
INSERT INTO companies(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveCompanies :one
UPDATE companies SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteCompanies :exec
DELETE FROM companies WHERE id=sqlc.arg(id);

-- name: GetCompanyMembers :one
SELECT payload FROM company_members WHERE id=sqlc.arg(id);
-- name: LockCompanyMembers :one
SELECT payload FROM company_members WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListCompanyMembers :many
SELECT payload FROM company_members WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertCompanyMembers :one
INSERT INTO company_members(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveCompanyMembers :one
UPDATE company_members SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteCompanyMembers :exec
DELETE FROM company_members WHERE id=sqlc.arg(id);

-- name: GetLoanProducts :one
SELECT payload FROM loan_products WHERE id=sqlc.arg(id);
-- name: LockLoanProducts :one
SELECT payload FROM loan_products WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListLoanProducts :many
SELECT payload FROM loan_products WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertLoanProducts :one
INSERT INTO loan_products(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveLoanProducts :one
UPDATE loan_products SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteLoanProducts :exec
DELETE FROM loan_products WHERE id=sqlc.arg(id);

-- name: GetApplications :one
SELECT payload FROM applications WHERE id=sqlc.arg(id);
-- name: LockApplications :one
SELECT payload FROM applications WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListApplications :many
SELECT payload FROM applications WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertApplications :one
INSERT INTO applications(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveApplications :one
UPDATE applications SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteApplications :exec
DELETE FROM applications WHERE id=sqlc.arg(id);

-- name: GetDocuments :one
SELECT payload FROM documents WHERE id=sqlc.arg(id);
-- name: LockDocuments :one
SELECT payload FROM documents WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListDocuments :many
SELECT payload FROM documents WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertDocuments :one
INSERT INTO documents(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveDocuments :one
UPDATE documents SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteDocuments :exec
DELETE FROM documents WHERE id=sqlc.arg(id);

-- name: GetUploadReservations :one
SELECT payload FROM upload_reservations WHERE id=sqlc.arg(id);
-- name: LockUploadReservations :one
SELECT payload FROM upload_reservations WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListUploadReservations :many
SELECT payload FROM upload_reservations WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertUploadReservations :one
INSERT INTO upload_reservations(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveUploadReservations :one
UPDATE upload_reservations SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteUploadReservations :exec
DELETE FROM upload_reservations WHERE id=sqlc.arg(id);

-- name: GetApplicationSnapshots :one
SELECT payload FROM application_snapshots WHERE id=sqlc.arg(id);
-- name: LockApplicationSnapshots :one
SELECT payload FROM application_snapshots WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListApplicationSnapshots :many
SELECT payload FROM application_snapshots WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertApplicationSnapshots :one
INSERT INTO application_snapshots(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveApplicationSnapshots :one
UPDATE application_snapshots SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteApplicationSnapshots :exec
DELETE FROM application_snapshots WHERE id=sqlc.arg(id);

-- name: GetInformationRequests :one
SELECT payload FROM information_requests WHERE id=sqlc.arg(id);
-- name: LockInformationRequests :one
SELECT payload FROM information_requests WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListInformationRequests :many
SELECT payload FROM information_requests WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertInformationRequests :one
INSERT INTO information_requests(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveInformationRequests :one
UPDATE information_requests SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteInformationRequests :exec
DELETE FROM information_requests WHERE id=sqlc.arg(id);

-- name: GetCheckRuns :one
SELECT payload FROM check_runs WHERE id=sqlc.arg(id);
-- name: LockCheckRuns :one
SELECT payload FROM check_runs WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListCheckRuns :many
SELECT payload FROM check_runs WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertCheckRuns :one
INSERT INTO check_runs(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveCheckRuns :one
UPDATE check_runs SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteCheckRuns :exec
DELETE FROM check_runs WHERE id=sqlc.arg(id);

-- name: GetScoringResults :one
SELECT payload FROM scoring_results WHERE id=sqlc.arg(id);
-- name: LockScoringResults :one
SELECT payload FROM scoring_results WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListScoringResults :many
SELECT payload FROM scoring_results WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertScoringResults :one
INSERT INTO scoring_results(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveScoringResults :one
UPDATE scoring_results SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteScoringResults :exec
DELETE FROM scoring_results WHERE id=sqlc.arg(id);

-- name: GetCreditDecisions :one
SELECT payload FROM credit_decisions WHERE id=sqlc.arg(id);
-- name: LockCreditDecisions :one
SELECT payload FROM credit_decisions WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListCreditDecisions :many
SELECT payload FROM credit_decisions WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertCreditDecisions :one
INSERT INTO credit_decisions(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveCreditDecisions :one
UPDATE credit_decisions SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteCreditDecisions :exec
DELETE FROM credit_decisions WHERE id=sqlc.arg(id);

-- name: GetOffers :one
SELECT payload FROM offers WHERE id=sqlc.arg(id);
-- name: LockOffers :one
SELECT payload FROM offers WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListOffers :many
SELECT payload FROM offers WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertOffers :one
INSERT INTO offers(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveOffers :one
UPDATE offers SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteOffers :exec
DELETE FROM offers WHERE id=sqlc.arg(id);

-- name: GetApplicationEvents :one
SELECT payload FROM application_events WHERE id=sqlc.arg(id);
-- name: LockApplicationEvents :one
SELECT payload FROM application_events WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListApplicationEvents :many
SELECT payload FROM application_events WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertApplicationEvents :one
INSERT INTO application_events(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveApplicationEvents :one
UPDATE application_events SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteApplicationEvents :exec
DELETE FROM application_events WHERE id=sqlc.arg(id);

-- name: GetAuditEvents :one
SELECT payload FROM audit_events WHERE id=sqlc.arg(id);
-- name: LockAuditEvents :one
SELECT payload FROM audit_events WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListAuditEvents :many
SELECT payload FROM audit_events WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertAuditEvents :one
INSERT INTO audit_events(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveAuditEvents :one
UPDATE audit_events SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteAuditEvents :exec
DELETE FROM audit_events WHERE id=sqlc.arg(id);

-- name: GetJobs :one
SELECT payload FROM jobs WHERE id=sqlc.arg(id);
-- name: LockJobs :one
SELECT payload FROM jobs WHERE id=sqlc.arg(id) FOR UPDATE;
-- name: ListJobs :many
SELECT payload FROM jobs WHERE payload @> sqlc.arg(filters)::jsonb
ORDER BY COALESCE(payload->>'created_at',payload->>'uploaded_at',payload->>'started_at','') DESC,id DESC;
-- name: InsertJobs :one
INSERT INTO jobs(id,payload) VALUES(sqlc.arg(id),sqlc.arg(payload)::jsonb) RETURNING payload;
-- name: SaveJobs :one
UPDATE jobs SET payload=sqlc.arg(payload)::jsonb WHERE id=sqlc.arg(id) RETURNING payload;
-- name: DeleteJobs :exec
DELETE FROM jobs WHERE id=sqlc.arg(id);

-- name: ClaimJobs :one
WITH candidate AS (
    SELECT id FROM jobs
    WHERE (status='queued' AND run_at<=clock_timestamp())
       OR (status='running' AND lease_until<=clock_timestamp())
    ORDER BY run_at NULLS FIRST,id
    FOR UPDATE SKIP LOCKED LIMIT 1
)
UPDATE jobs SET payload=jobs.payload || jsonb_build_object(
    'status','running','claim_id',sqlc.arg(claim_id)::uuid::text,
    'lease_until',clock_timestamp()+(sqlc.arg(lease_seconds)::double precision*interval '1 second'),
    'attempt',COALESCE((jobs.payload->>'attempt')::integer,0)+1,
    'cycle_attempt',COALESCE((jobs.payload->>'cycle_attempt')::integer,0)+1
)
FROM candidate WHERE jobs.id=candidate.id RETURNING jobs.payload;
