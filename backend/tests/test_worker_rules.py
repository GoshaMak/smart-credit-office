from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import hashlib

from credit.adapters import AdapterFailure, _local_mock_url, score_stub, validate_envelope
from credit.worker import _job_allowed, _lease_is_valid
from credit.worker import Worker, _latest_attempt
from credit.adapters import canonical_json


class MemoryStore:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.lock_order = []

    async def get(self, table, row_id, lock=False):
        if lock:
            self.lock_order.append(table)
        row = self.rows.get(table, {}).get(str(row_id))
        return deepcopy(row) if row is not None else None

    async def list(self, table, **filters):
        rows = self.rows.setdefault(table, {})
        return [deepcopy(row) for row in rows.values() if all(row.get(key) == value for key, value in filters.items())]

    async def save(self, table, row):
        self.rows.setdefault(table, {})[str(row["id"])] = deepcopy(row)

    async def insert(self, table, row):
        self.rows.setdefault(table, {})[str(row["id"])] = deepcopy(row)


class MemoryDatabase:
    def __init__(self, store):
        self.store = store

    @asynccontextmanager
    async def transaction(self):
        yield self.store


class WorkerSettings:
    lease_seconds = 30
    max_attempts = 3
    retry_delays = (1, 5)
    poll_interval = 0.1
    adapter_timeout = 5
    mock_url = "http://integration-mock:8001"


def test_stub_scoring_uses_decimal_thresholds_and_sorts_factors():
    data = {"financials": {"monthly_revenue": "1000.00", "monthly_expenses": "1000.00", "outstanding_debt": "6000.00", "monthly_debt_payments": "300.01"}}
    fns = {"company_found": True, "business_status": "active", "registration_matches": True, "unreliable_data": False, "reason_codes": []}
    bki = {"history_found": False, "outstanding_debt": "0.00", "monthly_debt_payments": "0.00", "overdue_amount": "0.00", "max_overdue_days": 0, "reason_codes": ["NO_CREDIT_HISTORY"]}
    result = score_stub(data, fns, bki)
    assert result == {
        "model_version": "stub-v1", "score": 80, "scale": "risk_0_100_higher_is_riskier",
        "factors": ["HIGH_DEBT_PAYMENTS", "NON_POSITIVE_OPERATING_MARGIN", "NO_CREDIT_HISTORY"],
    }


def test_stub_scoring_caps_points_without_dividing_by_zero():
    data = {"financials": {"monthly_revenue": "0.00", "monthly_expenses": "1.00", "outstanding_debt": "1.00", "monthly_debt_payments": "1.00"}}
    fns = {"company_found": False, "business_status": "unknown", "registration_matches": False, "unreliable_data": True, "reason_codes": []}
    bki = {"history_found": True, "outstanding_debt": "0.00", "monthly_debt_payments": "0.00", "overdue_amount": "1.00", "max_overdue_days": 0, "reason_codes": []}
    assert score_stub(data, fns, bki)["score"] == 100


def test_adapter_rejects_mismatched_response_ids_and_schema():
    request = {"request_id": "r", "snapshot_id": "s", "schema_version": "1"}
    response = {"status": "succeeded", **request, "result": {"company_found": True, "business_status": "active", "registration_matches": True, "unreliable_data": False, "reason_codes": []}}
    assert validate_envelope(response, request, "fns") == response
    response["snapshot_id"] = "other"
    try:
        validate_envelope(response, request, "fns")
    except AdapterFailure as exc:
        assert exc.code == "INVALID_RESPONSE" and not exc.retryable
    else:
        raise AssertionError("mismatched snapshot must be blocked")


def test_worker_lease_and_state_gate_reject_old_or_invalid_work():
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()
    job = {"status": "running", "claim_id": "claim-1", "lease_until": expiry, "source": "fns"}
    assert _lease_is_valid(job, "claim-1")
    assert not _lease_is_valid(job, "claim-2")
    assert _job_allowed({"status": "Submitted"}, job)
    assert not _job_allowed({"status": "ManualReview"}, job)


def test_mock_endpoint_accepts_only_local_http_service_urls():
    assert _local_mock_url("http://localhost:8001")
    assert _local_mock_url("http://integration-mock:8001/")
    assert not _local_mock_url("https://integration-mock:8001")
    assert not _local_mock_url("http://example.com")
    assert not _local_mock_url("http://user:password@localhost:8001")
    assert not _local_mock_url("http://localhost:8001/prefix")
    assert not _local_mock_url("http://localhost:99999")


def test_expired_worker_lease_cannot_commit_even_with_same_claim_id():
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert not _lease_is_valid({"status": "running", "claim_id": "same", "lease_until": expired}, "same")


def test_check_attempts_are_immutable_and_latest_wins_independent_of_storage_order():
    older_success = {"id": "a", "attempt": 1, "status": "succeeded", "result": {"company_found": True}}
    current_failure = {"id": "b", "attempt": 2, "status": "failed", "error_code": "TIMEOUT"}
    assert _latest_attempt([older_success, current_failure]) is current_failure
    assert _latest_attempt([current_failure, older_success]) is current_failure


def test_saved_adapter_payload_must_match_immutable_request_hash():
    payload = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "snapshot_id": "22222222-2222-4222-8222-222222222222",
        "schema_version": "1", "company": {}, "data": {}, "product": {}, "document_types": [],
    }
    job = {"request_id": payload["request_id"], "snapshot_id": payload["snapshot_id"],
           "source": "fns", "request_hash": hashlib.sha256(canonical_json(payload)).hexdigest()}
    assert Worker._valid_stored_request(job, payload)
    changed = {**payload, "document_types": ["financial_statement"]}
    assert not Worker._valid_stored_request(job, changed)


def test_worker_canonicalizes_product_set_fields_in_frozen_request():
    product = {
        "required_document_types": ["tax", "financial_statement"],
        "required_application_fields": ["/term_months", "/amount", "/amount"],
    }
    snapshot = {"id": "snapshot", "company": {}, "data": {}, "product": product, "document_ids": []}
    job = {"request_id": "request", "snapshot_id": "snapshot", "application_id": "app", "source": "fns"}
    request = asyncio.run(Worker(MemoryDatabase(MemoryStore({})), WorkerSettings())._build_request(
        MemoryStore({}), job, snapshot,
    ))
    assert request["product"]["required_document_types"] == ["financial_statement", "tax"]
    assert request["product"]["required_application_fields"] == ["/amount", "/term_months"]
    assert product["required_application_fields"] == ["/term_months", "/amount", "/amount"]


def test_stored_payload_rejects_wrong_snapshot_or_extra_mutable_fields():
    payload = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "snapshot_id": "22222222-2222-4222-8222-222222222222",
        "schema_version": "1", "company": {}, "data": {}, "product": {}, "document_types": [],
    }
    job = {"request_id": payload["request_id"], "snapshot_id": "33333333-3333-4333-8333-333333333333",
           "source": "fns", "request_hash": hashlib.sha256(canonical_json(payload)).hexdigest()}
    assert not Worker._valid_stored_request(job, payload)
    payload["mutable_retry_payload"] = True
    job["snapshot_id"] = payload["snapshot_id"]
    assert not Worker._valid_stored_request(job, payload)


def test_worker_persists_immutable_request_before_call_and_archives_expired_attempt():
    now = datetime.now(timezone.utc)
    lease = (now + timedelta(seconds=20)).isoformat()
    application = {
        "id": "app", "status": "Submitted", "version": 1,
        "company_id": "company", "current_snapshot_id": "snapshot",
        "data": {"product_id": "product"},
    }
    job = {
        "id": "job", "application_id": "app", "snapshot_id": "snapshot",
        "stage": "checks", "source": "fns", "status": "running",
        "attempt": 1, "cycle_attempt": 1, "claim_id": "claim-1", "lease_until": lease,
        "request_id": "11111111-1111-4111-8111-111111111111",
    }
    snapshot = {
        "id": "snapshot", "application_id": "app",
        "company": {"id": "company", "inn": "0000000001", "name": "Snapshot company"},
        "data": {"product_id": "product"}, "product": {"id": "product"},
        "document_ids": [],
    }
    store = MemoryStore({
        "applications": {"app": application}, "jobs": {"job": job},
        "application_snapshots": {"snapshot": snapshot}, "check_runs": {},
    })
    worker = Worker(MemoryDatabase(store), WorkerSettings())

    first = asyncio.run(worker._start(job, "claim-1"))
    assert first is not None
    assert store.lock_order[:2] == ["applications", "jobs"]
    persisted = store.rows["jobs"]["job"]["request_payload"]
    saved_hash = store.rows["jobs"]["job"]["request_hash"]
    assert persisted["company"]["name"] == "Snapshot company"
    assert saved_hash == hashlib.sha256(canonical_json(persisted)).hexdigest()

    # Simulate a takeover after this worker's lease expired. The snapshot read
    # path now returns changed data; the retried request must reuse its saved body.
    store.rows["jobs"]["job"].update(
        status="running", attempt=2, cycle_attempt=2, claim_id="claim-2", lease_until=lease,
    )
    store.rows["applications"]["app"].update(status="Checking", processing={
        "stage": "checks", "status": "running", "attempt": 1, "retry_available": False,
    })
    store.rows["application_snapshots"]["snapshot"]["company"]["name"] = "Changed source"

    second = asyncio.run(worker._start(store.rows["jobs"]["job"], "claim-2"))
    assert second is not None
    assert second["request_payload"] == persisted
    assert store.rows["jobs"]["job"]["request_hash"] == saved_hash
    attempts = store.rows["check_runs"]
    assert sorted(item["attempt"] for item in attempts.values()) == [1, 2]
    prior = next(item for item in attempts.values() if item["attempt"] == 1)
    assert prior["status"] == "failed" and prior["error_code"] == "LEASE_EXPIRED"
