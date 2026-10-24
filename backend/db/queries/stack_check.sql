-- name: CreateStackCheckRecord :one
INSERT INTO stack_check_records (id, amount, metadata, created_at)
VALUES ($1, $2, $3, $4)
RETURNING id, amount, metadata, created_at;

-- name: GetStackCheckRecord :one
SELECT id, amount, metadata, created_at
FROM stack_check_records
WHERE id = $1;
