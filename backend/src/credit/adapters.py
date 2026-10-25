"""Typed boundary to the local integration mock and the deterministic scoring stub."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit


@dataclass(slots=True)
class AdapterFailure(Exception):
    code: str
    retryable: bool


class AdapterNeedsInfo(Exception):
    def __init__(self, envelope: dict[str, Any]):
        self.envelope = envelope
        super().__init__("The integration needs more information")


class AdapterConflict(AdapterFailure):
    def __init__(self) -> None:
        super().__init__("REQUEST_INPUT_CONFLICT", False)


def canonical_json(value: Any) -> bytes:
    """Canonical request bytes shared by adapter and mock idempotency checks."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def validate_envelope(value: Any, request: dict[str, Any], source: str) -> dict[str, Any]:
    """Reject malformed or mis-correlated mock responses as technical failures."""
    if not isinstance(value, dict) or value.get("status") not in {
        "succeeded",
        "needs_info",
        "technical_error",
    }:
        raise AdapterFailure("INVALID_RESPONSE", False)
    for key in ("request_id", "snapshot_id", "schema_version"):
        if value.get(key) != request.get(key):
            raise AdapterFailure("INVALID_RESPONSE", False)
    status = value["status"]
    base = {"status", "request_id", "snapshot_id", "schema_version"}
    if status == "succeeded":
        if set(value) != base | {"result"} or not isinstance(value["result"], dict):
            raise AdapterFailure("INVALID_RESPONSE", False)
        _validate_result(value["result"], source)
    elif status == "needs_info":
        expected = base | {"requested_fields", "required_document_types", "message"}
        if set(value) != expected:
            raise AdapterFailure("INVALID_RESPONSE", False)
        fields, docs, message = (
            value["requested_fields"],
            value["required_document_types"],
            value["message"],
        )
        if (
            not isinstance(fields, list)
            or not isinstance(docs, list)
            or not isinstance(message, str)
            or not message.strip()
            or not (fields or docs)
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
        if any(
            not isinstance(p, str) or not _allowed_pointer(p) for p in fields
        ) or len(fields) != len(set(fields)):
            raise AdapterFailure("INVALID_RESPONSE", False)
        if any(
            not isinstance(d, str) or not d.strip() or len(d) > 100 for d in docs
        ) or len(set(docs)) != len(docs):
            raise AdapterFailure("INVALID_RESPONSE", False)
    else:
        code = value.get("error_code")
        if (
            set(value) != base | {"error_code", "retryable"}
            or not isinstance(code, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
            or not isinstance(value.get("retryable"), bool)
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
    return value


def _allowed_pointer(value: str) -> bool:
    from re import fullmatch

    return (
        fullmatch(
            r"/(amount|term_months|purpose|financials(?:/(?:as_of_date|monthly_revenue|monthly_expenses|outstanding_debt|monthly_debt_payments))?)",
            value,
        )
        is not None
    )


def _validate_result(result: dict[str, Any], source: str) -> None:
    if source == "fns":
        required = {
            "company_found",
            "business_status",
            "registration_matches",
            "unreliable_data",
            "reason_codes",
        }
        if (
            set(result) != required
            or not isinstance(result["company_found"], bool)
            or result["business_status"] not in {"active", "inactive", "unknown"}
            or not isinstance(result["registration_matches"], bool)
            or not isinstance(result["unreliable_data"], bool)
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
        if (
            not isinstance(result["reason_codes"], list)
            or any(not isinstance(x, str) or not x for x in result["reason_codes"])
            or len(set(result["reason_codes"])) != len(result["reason_codes"])
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
        if not result["company_found"] and (
            result["business_status"] != "unknown" or result["registration_matches"]
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
    elif source == "bki":
        required = {
            "history_found",
            "outstanding_debt",
            "monthly_debt_payments",
            "overdue_amount",
            "max_overdue_days",
            "reason_codes",
        }
        if (
            set(result) != required
            or not isinstance(result["history_found"], bool)
            or type(result["max_overdue_days"]) is not int
            or result["max_overdue_days"] < 0
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
        for key in ("outstanding_debt", "monthly_debt_payments", "overdue_amount"):
            if not _is_amount(result[key]):
                raise AdapterFailure("INVALID_RESPONSE", False)
        if (
            not isinstance(result["reason_codes"], list)
            or any(not isinstance(x, str) or not x for x in result["reason_codes"])
            or len(set(result["reason_codes"])) != len(result["reason_codes"])
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
        if not result["history_found"] and any(
            Decimal(result[k]) != 0
            for k in ("outstanding_debt", "monthly_debt_payments", "overdue_amount")
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
    elif source == "scoring":
        required = {"model_version", "score", "scale", "factors"}
        if (
            set(result) != required
            or result.get("model_version") != "stub-v1"
            or type(result.get("score")) is not int
            or not 0 <= result["score"] <= 100
            or result.get("scale") != "risk_0_100_higher_is_riskier"
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
        if (
            not isinstance(result["factors"], list)
            or any(not isinstance(x, str) or not x for x in result["factors"])
            or len(set(result["factors"])) != len(result["factors"])
            or result["factors"] != sorted(result["factors"])
        ):
            raise AdapterFailure("INVALID_RESPONSE", False)
    else:
        raise AdapterFailure("INVALID_RESPONSE", False)


def _is_amount(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return False
    return amount.is_finite() and amount >= 0 and value == f"{amount:.2f}"


def score_stub(
    data: dict[str, Any], fns: dict[str, Any], bki: dict[str, Any]
) -> dict[str, Any]:
    """Apply BACKEND_RULES.md stub-v1 scoring rules with exact Decimal arithmetic."""
    financials = data.get("financials")
    if not isinstance(financials, dict):
        raise AdapterNeedsInfo(
            {
                "status": "needs_info",
                "requested_fields": ["/financials"],
                "required_document_types": [],
                "message": "Предоставьте финансовые данные для оценки.",
            }
        )
    try:
        revenue = Decimal(financials["monthly_revenue"])
        expenses = Decimal(financials["monthly_expenses"])
        debt = Decimal(financials["outstanding_debt"])
        payments = Decimal(financials["monthly_debt_payments"])
        overdue = Decimal(bki["overdue_amount"])
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise AdapterFailure("INVALID_INPUT", False) from exc
    points = 20
    factors: set[str] = set()

    def add(condition: bool, points_to_add: int, code: str) -> None:
        nonlocal points
        if condition:
            points += points_to_add
            factors.add(code)

    add(revenue <= expenses, 30, "NON_POSITIVE_OPERATING_MARGIN")
    add(payments > revenue * Decimal("0.30"), 20, "HIGH_DEBT_PAYMENTS")
    add(debt > revenue * Decimal(6), 15, "HIGH_OUTSTANDING_DEBT")
    add(not fns["company_found"], 40, "COMPANY_NOT_FOUND")
    add(
        fns["company_found"] and fns["business_status"] != "active",
        30,
        "REGISTRATION_INACTIVE",
    )
    add(
        fns["company_found"] and not fns["registration_matches"],
        20,
        "REGISTRATION_MISMATCH",
    )
    add(fns["unreliable_data"], 20, "UNRELIABLE_DATA")
    add(overdue > 0 or bki["max_overdue_days"] > 0, 30, "OVERDUE_OBLIGATIONS")
    add(not bki["history_found"], 10, "NO_CREDIT_HISTORY")
    return {
        "model_version": "stub-v1",
        "score": min(points, 100),
        "scale": "risk_0_100_higher_is_riskier",
        "factors": sorted(factors),
    }


async def post_mock(
    base_url: str, path: str, payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """POST JSON without holding a database transaction during network I/O."""
    source = {
        "/v1/checks/fns": "fns",
        "/v1/checks/bki": "bki",
        "/v1/scoring": "scoring",
    }.get(path)
    if source is None or not _local_mock_url(base_url):
        raise AdapterFailure("INVALID_MOCK_ENDPOINT", False)
    body = canonical_json(payload)

    def do_request() -> tuple[int, bytes]:
        request = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=timeout
            ) as response:
                return response.status, response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(1024 * 1024)

    try:
        status, data = await asyncio.wait_for(
            asyncio.to_thread(do_request), timeout=timeout + 0.25
        )
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None)
        timed_out = (
            isinstance(exc, TimeoutError)
            or isinstance(reason, (TimeoutError,))
            or "timed out" in str(exc).lower()
        )
        raise AdapterFailure(
            "TIMEOUT" if timed_out else "SOURCE_UNAVAILABLE", True
        ) from exc
    try:
        envelope = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AdapterFailure("INVALID_RESPONSE", False) from exc
    if status in {200, 409, 503}:
        validate_envelope(envelope, payload, source)
    if status == 409:
        if (
            envelope.get("status") == "technical_error"
            and envelope.get("error_code") == "REQUEST_INPUT_CONFLICT"
            and envelope.get("retryable") is False
        ):
            raise AdapterConflict()
        raise AdapterFailure("INVALID_RESPONSE", False)
    if status == 503:
        if (
            isinstance(envelope, dict)
            and envelope.get("status") == "technical_error"
            and envelope.get("retryable") is True
        ):
            raise AdapterFailure(
                str(envelope.get("error_code") or "SOURCE_UNAVAILABLE"), True
            )
        raise AdapterFailure("SOURCE_UNAVAILABLE", True)
    if status != 200:
        raise AdapterFailure("INVALID_RESPONSE", False)
    return envelope


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a response from the local mock to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_mock_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "http"
        and host in {"localhost", "127.0.0.1", "::1", "integration-mock"}
        and (port is None or 1 <= port <= 65535)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def make_envelope(
    request: dict[str, Any], status: str, **fields: Any
) -> dict[str, Any]:
    return {
        "status": status,
        "request_id": request["request_id"],
        "snapshot_id": request["snapshot_id"],
        "schema_version": "1",
        **fields,
    }
