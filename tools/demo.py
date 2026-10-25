"""Run the seeded client/bank happy path against a live local backend.

    uv run --project backend --locked python tools/demo.py

Only the Python standard library is used. This is a demonstration script, not
an application data reset utility: every run creates a new company if needed
and a new application, and it never deletes database or S3 records.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal
from typing import Any


PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class DemoError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        upload: tuple[dict[str, str], bytes, str, str] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if upload is not None:
            fields, content, filename, content_type = upload
            boundary = f"credit-demo-{uuid.uuid4().hex}"
            chunks: list[bytes] = []
            for name, value in fields.items():
                chunks.extend([
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
                    value.encode(), b"\r\n",
                ])
            chunks.extend([
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n".encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(), content,
                f"\r\n--{boundary}--\r\n".encode(),
            ])
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            data = b"".join(chunks)
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if not body:
                    return None
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            # Avoid printing request bodies, tokens, or document contents.
            body = exc.read(64 * 1024)
            try:
                error = json.loads(body).get("message", "")
            except (json.JSONDecodeError, AttributeError):
                error = ""
            raise DemoError(f"{method} {path} returned HTTP {exc.code}: {error}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DemoError(f"Cannot reach backend at {self.base_url}: {type(exc).__name__}") from None


def login(client: Client, login_name: str, password: str) -> str:
    result = client.call("POST", "/auth/login", payload={"login": login_name, "password": password})
    return result["access_token"]


def current_application(client: Client, token: str, application_id: str) -> dict[str, Any]:
    return client.call("GET", f"/applications/{application_id}", token=token)


def upload_document(client: Client, token: str, application: dict, document_type: str) -> dict[str, Any]:
    return client.call(
        "POST", f"/applications/{application['id']}/documents", token=token,
        upload=(
            {"expected_version": str(application["version"]), "document_type": document_type},
            PDF, f"synthetic-{document_type}.pdf", "application/pdf",
        ),
    )


def wait_until(client: Client, token: str, application_id: str, statuses: set[str], deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        application = current_application(client, token, application_id)
        if application["status"] in statuses:
            return application
        if application["status"] in {"Rejected", "Declined", "Disbursed"}:
            return application
        time.sleep(0.4)
    return current_application(client, token, application_id)


def answer_system_requests(client: Client, token: str, application: dict[str, Any]) -> dict[str, Any]:
    requests = client.call(
        "GET", f"/applications/{application['id']}/information-requests?limit=100", token=token,
    )["items"]
    active = [item for item in requests if item["status"] == "open"]
    if len(active) != 1:
        raise DemoError("Application needs information but has no unique open request")
    request = active[0]
    for document_type in request["required_document_types"]:
        application = current_application(client, token, application["id"])
        mutation = upload_document(client, token, application, document_type)
        application["version"] = mutation["application_version"]
    documents = client.call("GET", f"/applications/{application['id']}/documents?limit=100", token=token)["items"]
    current_data = current_application(client, token, application["id"])["data"]
    data: dict[str, Any] = {}
    for pointer in request["requested_fields"]:
        if pointer == "/financials":
            data["financials"] = current_data["financials"]
        elif pointer.startswith("/financials/"):
            # The public contract requires Financials as a complete object.
            data["financials"] = current_data["financials"]
        else:
            key = pointer[1:]
            data[key] = current_data[key]
    application = current_application(client, token, application["id"])
    answer = {
        "comment": "Переданы синтетические данные для демонстрации backend.",
        "data": data,
        "document_ids": [document["id"] for document in documents],
    }
    client.call(
        "POST", f"/applications/{application['id']}/information-requests/{request['id']}/answer",
        token=token, payload={"expected_version": application["version"], "answer": answer},
    )
    return current_application(client, token, application["id"])


def run(args: argparse.Namespace) -> None:
    client = Client(args.base_url, args.timeout)
    client_token = login(client, args.client_login, args.client_password)
    bank_token = login(client, args.bank_login, args.bank_password)

    companies = client.call("GET", "/companies", token=client_token)
    if companies:
        company = companies[0]
    else:
        suffix = uuid.uuid4().int % 10**10
        company = client.call("POST", "/companies", token=client_token, payload={
            "legal_type": "legal_entity", "inn": f"{suffix:010d}",
            "name": "Синтетическая компания для демонстрации", "registration_date": "2020-01-15",
            "okved": "47.91", "address": "Тестовый адрес",
        })

    product_page = client.call("GET", "/products?limit=100", token=client_token)
    products = product_page["items"]
    if not products:
        raise DemoError("The demo catalog has no active loan product")
    product = products[0]
    amount = Decimal(str(product["min_amount"]))
    max_amount = Decimal(str(product["max_amount"]))
    desired_amount = min(max(amount, Decimal("500000.00")), max_amount)
    term = min(max(12, product["min_term_months"]), product["max_term_months"])
    financials = {
        "as_of_date": "2026-10-01", "monthly_revenue": "1500000.00",
        "monthly_expenses": "900000.00", "outstanding_debt": "500000.00",
        "monthly_debt_payments": "50000.00",
    }
    application = client.call("POST", "/applications", token=client_token, payload={
        "company_id": company["id"], "data": {"product_id": product["id"]},
    })
    update = client.call(
        "PATCH", f"/applications/{application['id']}", token=client_token,
        payload={"expected_version": application["version"], "data": {
            "amount": f"{desired_amount:.2f}", "term_months": term,
            "purpose": "Закупка товарных запасов и пополнение оборотных средств",
            "financials": financials,
        }},
    )
    application = update
    doc_types = set(product["required_document_types"])
    for document_type in sorted(doc_types):
        mutation = upload_document(client, client_token, application, document_type)
        application = current_application(client, client_token, application["id"])

    submitted = client.call(
        "POST", f"/applications/{application['id']}/submit", token=client_token,
        payload={"expected_version": application["version"], "consents": {"credit_history": True, "data_processing": True}},
    )
    application_id = submitted["id"]
    deadline = time.monotonic() + args.wait_seconds
    application = wait_until(client, client_token, application_id, {"ManualReview", "NeedsInfo"}, deadline)
    while application["status"] == "NeedsInfo" and time.monotonic() < deadline:
        application = answer_system_requests(client, client_token, application)
        application = wait_until(client, client_token, application_id, {"ManualReview", "NeedsInfo"}, deadline)
    if application["status"] != "ManualReview":
        bank_view = client.call("GET", f"/bank/applications/{application_id}", token=bank_token)
        processing = bank_view.get("processing", {})
        raise DemoError(f"Application reached {application['status']}; processing={processing.get('status', 'n/a')}. Inspect the bank queue for technical failures.")

    application = current_application(client, client_token, application_id)
    bank_view = client.call("GET", f"/bank/applications/{application_id}", token=bank_token)
    terms_amount = max(amount, Decimal(str(application["data"]["amount"])) * Decimal("0.8"))
    offer_term = max(product["min_term_months"], term - 1)
    client.call("POST", f"/bank/applications/{application_id}/approve", token=bank_token, payload={
        "expected_version": bank_view["version"],
        "terms": {"product_id": product["id"], "amount": f"{terms_amount:.2f}", "currency": product["currency"],
                  "term_months": offer_term, "annual_rate_percent": "18.50",
                  "conditions": ["Имитация предложения для демонстрации"]},
        "reason_codes": ["MANUAL_APPROVAL", "REDUCED_AMOUNT"],
        "internal_comment": "Синтетическое ручное решение демонстрационного сценария.",
        "client_message": "Предлагаем демонстрационные условия финансирования.",
    })

    application = current_application(client, client_token, application_id)
    offers = client.call("GET", f"/applications/{application_id}/offers?limit=100", token=client_token)["items"]
    pending = next(item for item in offers if item["status"] == "pending")
    client.call("POST", f"/applications/{application_id}/offers/{pending['id']}/accept", token=client_token,
                payload={"expected_version": application["version"]})
    application = current_application(client, client_token, application_id)
    client.call("POST", f"/applications/{application_id}/sign", token=client_token,
                payload={"expected_version": application["version"]})
    application = wait_until(client, client_token, application_id, {"Disbursed"}, time.monotonic() + args.wait_seconds)
    if application["status"] != "Disbursed":
        raise DemoError(f"Application reached {application['status']}; inspect the bank processing status")
    print(f"Demo passed: application_id={application_id}, final_status={application['status']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000/api/v1")
    parser.add_argument("--client-login", default="client")
    parser.add_argument("--client-password", default="demo-client-password")
    parser.add_argument("--bank-login", default="banker")
    parser.add_argument("--bank-password", default="demo-bank-password")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--wait-seconds", type=float, default=60)
    args = parser.parse_args()
    if args.timeout <= 0 or args.wait_seconds <= 0:
        parser.error("--timeout and --wait-seconds must be positive")
    try:
        run(args)
    except (DemoError, KeyError, IndexError, StopIteration, ValueError) as exc:
        print(f"Demo failed: {exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
