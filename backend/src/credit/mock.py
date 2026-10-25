"""Local-only synthetic FNS/BKI/scoring HTTP service (no real external data)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from credit.adapters import AdapterNeedsInfo, canonical_json, make_envelope, score_stub


class MockState:
    """Process-local idempotency cache and fixture counters; restart resets fixtures."""
    def __init__(self) -> None:
        self.cache: dict[str, tuple[str, dict[str, Any]]] = {}
        # Keep request identity after timeout/5xx so changed bodies under a
        # reused request id cannot bypass the idempotency contract.
        self.digests: dict[str, str] = {}
        self.calls: dict[tuple[str, str], int] = {}
        self.lock = asyncio.Lock()

    def reset(self) -> None:
        self.cache.clear()
        self.digests.clear()
        self.calls.clear()


state = MockState()
app = FastAPI(title="Local synthetic integrations", docs_url=None, redoc_url=None, openapi_url=None)
BASE_FIELDS = {"request_id", "snapshot_id", "schema_version", "company", "data", "product", "document_types"}


def _valid_payload(payload: Any, source: str) -> bool:
    if not isinstance(payload, dict):
        return False
    expected = BASE_FIELDS | ({"checks"} if source == "scoring" else set())
    if set(payload) != expected or payload.get("schema_version") != "1":
        return False
    try:
        from datetime import date
        from uuid import UUID
        UUID(payload["request_id"])
        UUID(payload["snapshot_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    if _contains_null(payload):
        return False
    company, data, product, documents = (payload.get(x) for x in ("company", "data", "product", "document_types"))
    if not isinstance(company, dict) or not isinstance(data, dict) or not isinstance(product, dict) or not isinstance(documents, list):
        return False
    company_keys = {"id", "version", "legal_type", "inn", "name", "registration_date", "okved", "address", "created_at", "updated_at"}
    product_keys = {"id", "code", "name", "description", "currency", "min_amount", "max_amount", "min_term_months", "max_term_months", "required_document_types", "required_application_fields", "active"}
    if set(company) - company_keys or set(data) - {"product_id", "amount", "term_months", "purpose", "financials"} or set(product) - product_keys:
        return False
    if not {"id", "version", "inn", "legal_type", "name", "registration_date", "okved", "address", "created_at", "updated_at"}.issubset(company) or not {"id", "code", "name", "description", "currency", "min_amount", "max_amount", "min_term_months", "max_term_months", "required_document_types", "required_application_fields", "active"}.issubset(product):
        return False
    try:
        UUID(company["id"])
        UUID(product["id"])
        date.fromisoformat(company["registration_date"])
    except (TypeError, ValueError, KeyError):
        return False
    if type(company["version"]) is not int or company["version"] < 1:
        return False
    if (not isinstance(company["name"], str) or not company["name"]
            or not isinstance(company["address"], str) or not company["address"]
            or company["legal_type"] not in {"legal_entity", "individual_entrepreneur"}):
        return False
    if (not isinstance(product["active"], bool) or product["currency"] != "RUB"
            or not _valid_amount(product["min_amount"]) or not _valid_amount(product["max_amount"])
            or Decimal(product["min_amount"]) <= 0 or Decimal(product["max_amount"]) < Decimal(product["min_amount"])
            or type(product["min_term_months"]) is not int or type(product["max_term_months"]) is not int
            or not 1 <= product["min_term_months"] <= product["max_term_months"] <= 600):
        return False
    for key in ("required_document_types", "required_application_fields"):
        values = product[key]
        if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values) or values != sorted(set(values)):
            return False
    data = payload["data"]
    if "product_id" in data:
        try:
            UUID(data["product_id"])
        except (TypeError, ValueError):
            return False
    if "amount" in data and (not _valid_amount(data["amount"]) or Decimal(data["amount"]) <= 0):
        return False
    if "term_months" in data and (type(data["term_months"]) is not int or not 1 <= data["term_months"] <= 600):
        return False
    if "purpose" in data and (not isinstance(data["purpose"], str) or not 1 <= len(data["purpose"]) <= 2000):
        return False
    if "financials" in data:
        financials = data["financials"]
        required_financials = {"as_of_date", "monthly_revenue", "monthly_expenses", "outstanding_debt", "monthly_debt_payments"}
        if not isinstance(financials, dict) or set(financials) != required_financials:
            return False
        try:
            date.fromisoformat(financials["as_of_date"])
        except (TypeError, ValueError):
            return False
        if any(not _valid_amount(financials[k]) for k in required_financials - {"as_of_date"}):
            return False
    if any(not isinstance(item, str) for item in documents) or documents != sorted(set(documents)):
        return False
    if not isinstance(company.get("inn"), str) or not re.fullmatch(r"[0-9]{10}|[0-9]{12}", company["inn"]):
        return False
    if any(not isinstance(x, str) or not x for x in documents) or len(documents) != len(set(documents)):
        return False
    if source == "scoring":
        checks = payload.get("checks")
        if not isinstance(checks, dict) or set(checks) != {"fns", "bki"}:
            return False
        try:
            from credit.adapters import _validate_result
            _validate_result(checks["fns"], "fns")
            _validate_result(checks["bki"], "bki")
        except Exception:
            return False
    return True


def _contains_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_null(x) for x in value.values())
    if isinstance(value, list):
        return any(_contains_null(x) for x in value)
    return False


def _valid_amount(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,14})\.[0-9]{2}", value):
        return False
    return Decimal(value).is_finite()


def _amount(financials: Any, key: str) -> str:
    value = financials.get(key, "0.00") if isinstance(financials, dict) else "0.00"
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,14})\.[0-9]{2}", value):
        return "0.00"
    return value


async def _compute(payload: dict[str, Any], source: str) -> tuple[int, dict[str, Any], bool]:
    request_id = payload["request_id"]
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    async with state.lock:
        cached = state.cache.get(request_id)
        prior_digest = state.digests.get(request_id)
        if prior_digest is not None and prior_digest != digest:
            return 409, make_envelope(payload, "technical_error", error_code="REQUEST_INPUT_CONFLICT", retryable=False), True
        if cached:
            return 200, cached[1], True
        state.digests.setdefault(request_id, digest)
        inn = payload["company"]["inn"]
        counter_key = (inn, source)
        call_number = state.calls.get(counter_key, 0) + 1
        state.calls[counter_key] = call_number
        response: dict[str, Any]
        status_code = 200
        cacheable = True
        if source == "fns":
            if inn == "0000000007" and call_number == 1:
                status_code, cacheable = 503, False
                response = make_envelope(payload, "technical_error", error_code="SOURCE_UNAVAILABLE", retryable=True)
            elif inn == "0000000008" and call_number <= 3:
                # The response is deliberately delayed beyond adapter_timeout to model a real timeout.
                timeout = max(5.5, float(os.getenv("MOCK_TIMEOUT_DELAY", "5.5")))
                response = {}
                cacheable = False
            elif inn == "0000000002":
                result = {"company_found": False, "business_status": "unknown", "registration_matches": False, "unreliable_data": False, "reason_codes": ["COMPANY_NOT_FOUND"]}
                response = make_envelope(payload, "succeeded", result=result)
            elif inn == "0000000003":
                result = {"company_found": True, "business_status": "inactive", "registration_matches": True, "unreliable_data": False, "reason_codes": ["REGISTRATION_INACTIVE"]}
                response = make_envelope(payload, "succeeded", result=result)
            elif inn == "0000000004":
                result = {"company_found": True, "business_status": "active", "registration_matches": False, "unreliable_data": True, "reason_codes": ["REGISTRATION_MISMATCH", "UNRELIABLE_DATA"]}
                response = make_envelope(payload, "succeeded", result=result)
            else:
                result = {"company_found": True, "business_status": "active", "registration_matches": True, "unreliable_data": False, "reason_codes": []}
                response = make_envelope(payload, "succeeded", result=result)
        elif source == "bki":
            financials = payload["data"].get("financials")
            if inn == "0000000009" and "financial_statement" not in payload["document_types"]:
                response = make_envelope(payload, "needs_info", requested_fields=["/financials"], required_document_types=["financial_statement"], message="Предоставьте финансовые данные и финансовую отчётность.")
            elif inn == "0000000005":
                result = {"history_found": True, "outstanding_debt": _amount(financials, "outstanding_debt"), "monthly_debt_payments": _amount(financials, "monthly_debt_payments"), "overdue_amount": "100000.00", "max_overdue_days": 45, "reason_codes": ["OVERDUE_OBLIGATIONS"]}
                response = make_envelope(payload, "succeeded", result=result)
            elif inn == "0000000006":
                result = {"history_found": False, "outstanding_debt": "0.00", "monthly_debt_payments": "0.00", "overdue_amount": "0.00", "max_overdue_days": 0, "reason_codes": ["NO_CREDIT_HISTORY"]}
                response = make_envelope(payload, "succeeded", result=result)
            else:
                result = {"history_found": True, "outstanding_debt": _amount(financials, "outstanding_debt"), "monthly_debt_payments": _amount(financials, "monthly_debt_payments"), "overdue_amount": "0.00", "max_overdue_days": 0, "reason_codes": []}
                response = make_envelope(payload, "succeeded", result=result)
        else:
            checks = payload["checks"]
            try:
                result = score_stub(payload["data"], checks["fns"], checks["bki"])
            except AdapterNeedsInfo as needs_info:
                info = needs_info.envelope
                response = make_envelope(payload, "needs_info", requested_fields=info["requested_fields"], required_document_types=info["required_document_types"], message=info["message"])
            else:
                response = make_envelope(payload, "succeeded", result=result)
        if cacheable:
            state.cache[request_id] = (digest, response)
    if source == "fns" and payload["company"]["inn"] == "0000000008" and call_number <= 3:
        await asyncio.sleep(timeout)
        return 503, make_envelope(payload, "technical_error", error_code="TIMEOUT", retryable=True), False
    return status_code, response, False


async def _endpoint(request: Request, source: str) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"message": "Invalid JSON"}, status_code=400)
    if not _valid_payload(payload, source):
        return JSONResponse({"message": "Invalid integration request"}, status_code=400)
    status, envelope, _ = await _compute(payload, source)
    return JSONResponse(envelope, status_code=status)


@app.post("/v1/checks/fns")
async def fns(request: Request) -> JSONResponse:
    return await _endpoint(request, "fns")


@app.post("/v1/checks/bki")
async def bki(request: Request) -> JSONResponse:
    return await _endpoint(request, "bki")


@app.post("/v1/scoring")
async def scoring(request: Request) -> JSONResponse:
    return await _endpoint(request, "scoring")


@app.get("/_health")
async def health() -> dict[str, str]:
    """Liveness check for the private Compose-only integration mock."""
    return {"status": "ok"}
