from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from credit.mock import _compute, _valid_payload, app, state


def payload(inn: str = "0000000001", request_id: str = "11111111-1111-4111-8111-111111111111"):
    return {
        "request_id": request_id,
        "snapshot_id": "22222222-2222-4222-8222-222222222222",
        "schema_version": "1",
        "company": {
            "id": "33333333-3333-4333-8333-333333333333", "version": 1,
            "inn": inn, "legal_type": "legal_entity", "name": "Synthetic LLC",
            "registration_date": "2020-01-01", "okved": "47.91", "address": "Synthetic address",
            "created_at": "2020-01-01T00:00:00Z", "updated_at": "2020-01-01T00:00:00Z",
        },
        "data": {"product_id": "44444444-4444-4444-8444-444444444444", "amount": "50000.00",
                 "term_months": 12, "purpose": "Synthetic purpose",
                 "financials": {"as_of_date": "2026-10-01", "monthly_revenue": "100000.00",
                                "monthly_expenses": "50000.00", "outstanding_debt": "1000.00",
                                "monthly_debt_payments": "100.00"}},
        "product": {
            "id": "44444444-4444-4444-8444-444444444444", "code": "synthetic",
            "name": "Synthetic product", "description": "Test only", "currency": "RUB",
            "min_amount": "10000.00", "max_amount": "1000000.00",
            "min_term_months": 1, "max_term_months": 60,
            "required_document_types": [], "required_application_fields": [], "active": True,
        },
        "document_types": [],
    }


def test_mock_success_is_cached_and_input_conflict_is_explicit():
    state.reset()
    body = payload()
    assert _valid_payload(body, "fns")
    first = asyncio.run(_compute(body, "fns"))
    replay = asyncio.run(_compute(body, "fns"))
    assert first[:2] == replay[:2]
    assert first[2] is False and replay[2] is True
    changed = deepcopy(body)
    changed["data"]["financials"]["monthly_revenue"] = "101000.00"
    status, response, _ = asyncio.run(_compute(changed, "fns"))
    assert status == 409
    assert response["error_code"] == "REQUEST_INPUT_CONFLICT"


def test_fixture_outage_is_not_cached_as_a_success():
    state.reset()
    body = payload("0000000007")
    status, first, cached = asyncio.run(_compute(body, "fns"))
    assert status == 503 and not cached and first["status"] == "technical_error"
    status, second, cached = asyncio.run(_compute(body, "fns"))
    assert status == 200 and not cached and second["status"] == "succeeded"


def test_request_id_cannot_be_reused_for_different_input_after_technical_error():
    state.reset()
    body = payload("0000000007")
    status, first, _ = asyncio.run(_compute(body, "fns"))
    assert status == 503 and first["status"] == "technical_error"
    changed = deepcopy(body)
    changed["data"]["financials"]["monthly_revenue"] = "110000.00"
    status, conflict, _ = asyncio.run(_compute(changed, "fns"))
    assert status == 409
    assert conflict["error_code"] == "REQUEST_INPUT_CONFLICT"
    status, restored, _ = asyncio.run(_compute(body, "fns"))
    assert status == 200 and restored["status"] == "succeeded"


def test_mock_rejects_malformed_money_unknown_fields_and_noncanonical_sets():
    base = payload()
    assert _valid_payload(base, "fns")
    invalid = deepcopy(base)
    invalid["data"]["financials"]["monthly_revenue"] = "NaN"
    assert not _valid_payload(invalid, "fns")
    invalid = deepcopy(base)
    invalid["company"]["secret_extra"] = "unexpected"
    assert not _valid_payload(invalid, "fns")
    invalid = deepcopy(base)
    invalid["document_types"] = ["financial_statement", "financial_statement"]
    assert not _valid_payload(invalid, "fns")


def test_scoring_mock_requires_correlated_normalized_check_results():
    state.reset()
    body = payload("0000000001")
    body["checks"] = {
        "fns": {"company_found": True, "business_status": "active", "registration_matches": True,
                "unreliable_data": False, "reason_codes": []},
        "bki": {"history_found": True, "outstanding_debt": "1000.00", "monthly_debt_payments": "100.00",
                "overdue_amount": "0.00", "max_overdue_days": 0, "reason_codes": []},
    }
    assert _valid_payload(body, "scoring")
    status, response, _ = asyncio.run(_compute(body, "scoring"))
    assert status == 200 and response["status"] == "succeeded"
    assert response["result"] == {
        "model_version": "stub-v1", "score": 20,
        "scale": "risk_0_100_higher_is_riskier", "factors": [],
    }
    invalid = deepcopy(body)
    invalid["checks"]["fns"]["unexpected"] = "not allowed"
    assert not _valid_payload(invalid, "scoring")


def test_scoring_mock_returns_needs_info_when_financials_are_missing():
    body = payload()
    body["data"].pop("financials")
    body["request_id"] = "99999999-9999-4999-8999-999999999999"
    body["checks"] = {
        "fns": {"company_found": True, "business_status": "active", "registration_matches": True,
                "unreliable_data": False, "reason_codes": []},
        "bki": {"history_found": True, "outstanding_debt": "0.00", "monthly_debt_payments": "0.00",
                "overdue_amount": "0.00", "max_overdue_days": 0, "reason_codes": []},
    }
    status, response, _ = asyncio.run(_compute(body, "scoring"))
    assert status == 200 and response["status"] == "needs_info"
    assert response["requested_fields"] == ["/financials"]


def test_needs_info_scenario_requires_new_financial_statement_document():
    state.reset()
    body = payload("0000000009")
    status, response, _ = asyncio.run(_compute(body, "bki"))
    assert status == 200 and response["status"] == "needs_info"
    assert response["required_document_types"] == ["financial_statement"]
    fixed = payload("0000000009", "55555555-5555-4555-8555-555555555555")
    fixed["document_types"] = ["financial_statement"]
    status, response, _ = asyncio.run(_compute(fixed, "bki"))
    assert status == 200 and response["status"] == "succeeded"


def test_all_synthetic_company_check_fixtures_are_deterministic():
    for inn in ("0000000001", "0000000002", "0000000003", "0000000004"):
        state.reset()
        body = payload(inn, str(uuid4()))
        first = asyncio.run(_compute(body, "fns"))
        second = asyncio.run(_compute(body, "fns"))
        assert first[0] == 200 and first[2] is False
        assert second[0] == 200 and second[2] is True
        assert first[1] == second[1]


def test_outage_fixture_recovers_after_timeout_window_without_fake_not_found():
    state.reset()
    body = payload("0000000008")
    # The first three attempts deliberately sleep past the HTTP client's timeout;
    # seed their process-local counter to keep this unit test fast.
    state.calls[("0000000008", "fns")] = 3
    status, response, _ = asyncio.run(_compute(body, "fns"))
    assert status == 200
    assert response["result"]["company_found"] is True


def test_mock_internal_health_endpoint_is_available():
    with TestClient(app) as client:
        response = client.get("/_health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
