from copy import deepcopy

import pytest

from tools.validate_contract import (
    ContractError,
    check_auth_headers,
    check_examples,
    load_contract,
    schema_validator,
    validate_contract,
)


@pytest.fixture(scope="module")
def contract():
    return load_contract()


def valid(contract, schema, value):
    return schema_validator(contract, f"#/components/schemas/{schema}").is_valid(value)


def test_published_contract_and_examples_are_valid():
    document, examples = validate_contract()
    assert document["info"]["version"] == "0.2.0"
    assert examples > 0


def test_duplicate_yaml_key_is_rejected(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("outer:\n  description: original\n  description: overwritten\n")
    with pytest.raises(ContractError, match="Duplicate YAML key"):
        load_contract(path)


def test_yaml_merge_cannot_hide_overwrite(tmp_path):
    path = tmp_path / "merge.yaml"
    path.write_text("base: &base\n  value: first\nchild:\n  <<: *base\n  value: second\n")
    with pytest.raises(ContractError, match="merge keys"):
        load_contract(path)


def test_missing_response_description_fails_openapi_validation(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text(
        "openapi: 3.1.0\ninfo: {title: Test, version: '1'}\n"
        "paths:\n  /test:\n    get:\n      responses:\n        '200': {}\n"
    )
    with pytest.raises(ContractError, match="description"):
        validate_contract(path)


def test_bad_published_example_is_rejected(contract):
    broken = deepcopy(contract)
    media = broken["paths"]["/applications"]["post"]["requestBody"]["content"]["application/json"]
    media["example"]["data"]["amount"] = 123.45
    with pytest.raises(ContractError, match="Invalid example"):
        check_examples(broken)


def test_auth_errors_require_no_store(contract):
    broken = deepcopy(contract)
    del broken["components"]["responses"]["AuthBadRequest"]["headers"]
    with pytest.raises(ContractError, match="Cache-Control"):
        check_auth_headers(broken)


def test_empty_draft_can_later_select_product(contract):
    product = "22222222-2222-4222-8222-222222222222"
    assert valid(contract, "LoanInput", {})
    assert valid(contract, "ApplicationUpdate", {"expected_version": 1, "data": {"product_id": product}})
    assert not valid(contract, "ApplicationUpdate", {"expected_version": 1, "data": {}})


def test_clarification_cannot_change_product_or_clear_fields(contract):
    assert valid(contract, "LoanClarification", {"amount": "100000.00"})
    assert not valid(contract, "LoanClarification", {"product_id": "22222222-2222-4222-8222-222222222222"})
    assert not valid(contract, "LoanClarification", {"amount": None})
    assert not valid(contract, "LoanClarification", {"financials": {"monthly_revenue": "100.00"}})


@pytest.mark.parametrize("fields,documents", [([], []), (["/product_id"], []), (["/company/name"], []), (["/purpose", "/purpose"], []), ([], [""])])
def test_information_request_rejects_unanswerable_requirements(contract, fields, documents):
    assert not valid(contract, "InformationRequestCreate", {
        "expected_version": 1, "message": "Уточните данные",
        "requested_fields": fields, "required_document_types": documents,
    })


def test_document_only_request_and_unique_selected_documents(contract):
    assert valid(contract, "InformationRequestCreate", {
        "expected_version": 1, "message": "Пришлите отчёт",
        "requested_fields": [], "required_document_types": ["financial_statement"],
    })
    document_id = "44444444-4444-4444-8444-444444444444"
    assert not valid(contract, "InformationAnswer", {"comment": "Отчёт", "document_ids": [document_id, document_id]})


def test_disbursement_has_technical_status(contract):
    assert valid(contract, "ProcessingStatus", {
        "stage": "disbursement", "status": "blocked", "attempt": 3,
        "retry_available": True, "error_code": "TIMEOUT",
    })


def test_scoring_scale_is_bounded_and_versioned(contract):
    score = {
        "id": "22222222-2222-4222-8222-222222222222",
        "snapshot_id": "33333333-3333-4333-8333-333333333333",
        "request_id": "44444444-4444-4444-8444-444444444444",
        "model_version": "stub-v1", "score": 20,
        "scale": "risk_0_100_higher_is_riskier", "factors": [],
        "created_at": "2026-10-24T12:00:00Z",
    }
    assert valid(contract, "ScoringResult", score)
    assert not valid(contract, "ScoringResult", {**score, "score": 101})
    assert not valid(contract, "ScoringResult", {**score, "score": 20.5})
    assert not valid(contract, "ScoringResult", {**score, "model_version": "unknown"})
