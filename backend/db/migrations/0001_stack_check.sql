CREATE TABLE IF NOT EXISTS stack_check_records (
    id UUID PRIMARY KEY,
    amount NUMERIC(18, 2) NOT NULL,
    metadata JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
