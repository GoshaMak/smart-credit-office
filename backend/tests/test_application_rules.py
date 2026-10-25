from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from credit.applications import (
    ApplicationService,
    _bank_actions,
    _check_required_docs,
    _snapshot_view,
    _validate_product_limits,
)
from credit.common import ApiError, require_application


def _product():
    return {
        "id": "product-1",
        "active": True,
        "currency": "RUB",
        "min_amount": "10000.00",
        "max_amount": "5000000.00",
        "min_term_months": 3,
        "max_term_months": 60,
        "required_document_types": ["financial_statement"],
        "required_application_fields": ["/purpose"],
    }


def test_product_bounds_accept_and_reject_amount_and_term():
    _validate_product_limits(_product(), {"amount": "10000.00", "term_months": 3})

    with pytest.raises(ApiError) as error:
        _validate_product_limits(_product(), {"amount": "5000000.01", "term_months": 3})
    assert error.value.status_code == 400

    with pytest.raises(ApiError):
        _validate_product_limits(_product(), {"amount": "10000.00", "term_months": 61})


def test_submission_requires_document_type_from_product():
    with pytest.raises(ApiError, match="Не приложены"):
        _check_required_docs(_product(), {}, [{"id": "doc-1", "document_type": "other"}])

    _check_required_docs(_product(), {}, [{"id": "doc-2", "document_type": "financial_statement"}])


def test_snapshot_public_view_drops_internal_product_and_consent_data():
    view = _snapshot_view({
        "id": "snapshot-1",
        "application_id": "app-1",
        "revision": 1,
        "company": {"id": "company-1", "inn": "1234567890", "secret": "private"},
        "data": {"amount": "12000.00"},
        "document_ids": ["doc-1"],
        "product": {"private_policy": True},
        "consents": {"actor_id": "user-secret"},
        "created_at": "2026-10-01T00:00:00Z",
    })
    assert set(view) == {"id", "application_id", "revision", "company", "data", "document_ids", "created_at"}
    assert "secret" not in view["company"]


def test_bank_retry_action_requires_blocked_retriable_application_state():
    assert _bank_actions({"status": "Checking", "processing": {"status": "blocked", "retry_available": True}}) == ["retry_processing"]
    assert _bank_actions({"status": "ManualReview", "processing": {}}) == ["request_information", "approve", "reject"]
    assert _bank_actions({"status": "Checking", "processing": {"status": "running", "retry_available": False}}) == []


class TransactionalMemoryStore:
    def __init__(self, tables):
        self.tables = deepcopy(tables)

    async def get(self, table, item_id, lock=False):
        return deepcopy(next((row for row in self.tables[table] if row["id"] == item_id), None))

    async def list(self, table, **filters):
        return deepcopy([row for row in self.tables[table] if all(row.get(key) == value for key, value in filters.items())])

    async def insert(self, table, item):
        self.tables[table].append(deepcopy(item))
        return deepcopy(item)

    async def save(self, table, item):
        for index, current in enumerate(self.tables[table]):
            if current["id"] == item["id"]:
                self.tables[table][index] = deepcopy(item)
                return deepcopy(item)
        raise AssertionError("row not found")


class TransactionalMemoryDB:
    def __init__(self, tables):
        self.store = TransactionalMemoryStore(tables)

    @asynccontextmanager
    async def transaction(self):
        before = deepcopy(self.store.tables)
        try:
            yield self.store
        except Exception:
            self.store.tables = before
            raise


def _tables_for_application(status="Draft"):
    return {
        "users": [], "company_members": [{"id": "membership-1", "company_id": "company-1", "user_id": "client-1", "role": "owner"}],
        "companies": [{"id": "company-1", "name": "Demo", "inn": "0000000001"}],
        "loan_products": [_product()],
        "applications": [{"id": "application-1", "company_id": "company-1", "status": status, "version": 1,
                          "data": {"product_id": "product-1", "term_months": 12}, "created_at": "2026-10-24T09:00:00Z", "updated_at": "2026-10-24T09:00:00Z"}],
        "application_snapshots": [], "documents": [], "information_requests": [], "offers": [], "jobs": [],
        "application_events": [], "audit_events": [], "check_runs": [], "scoring_results": [], "credit_decisions": [],
    }


def test_submit_missing_base_amount_rolls_back_without_job_or_snapshot():
    async def scenario():
        db = TransactionalMemoryDB(_tables_for_application())
        service = ApplicationService(db, SimpleNamespace(credit_policy_version="demo-manual-v1"))
        with pytest.raises(ApiError) as error:
            await service.execute("submitApplication", actor={"id": "client-1", "role": "client"},
                                  path={"application_id": "application-1"}, query={},
                                  body={"expected_version": 1, "consents": {"credit_history": True, "data_processing": True}})
        assert error.value.status_code == 400
        assert db.store.tables["applications"][0]["status"] == "Draft"
        assert db.store.tables["application_snapshots"] == []
        assert db.store.tables["jobs"] == []

    asyncio.run(scenario())


def test_tombstoned_required_document_cannot_enter_a_submission_snapshot():
    async def scenario():
        tables = _tables_for_application()
        app = tables["applications"][0]
        app["data"].update(amount="100000.00", purpose="Working capital")
        tables["documents"] = [{
            "id": "deleted-doc", "application_id": app["id"],
            "document_type": "financial_statement", "s3_key": "documents/app/deleted.pdf",
            "deleted_at": "2026-10-25T00:00:00Z",
        }]
        db = TransactionalMemoryDB(tables)
        service = ApplicationService(db, SimpleNamespace(credit_policy_version="demo-manual-v1"))
        with pytest.raises(ApiError, match="Не приложены обязательные документы") as error:
            await service.execute(
                "submitApplication", actor={"id": "client-1", "role": "client"},
                path={"application_id": app["id"]}, query={},
                body={"expected_version": 1, "consents": {"credit_history": True, "data_processing": True}},
            )
        assert error.value.status_code == 400
        assert db.store.tables["application_snapshots"] == []
        assert db.store.tables["jobs"] == []

    asyncio.run(scenario())


def test_client_view_hides_delete_action_when_only_deleted_documents_remain():
    async def scenario():
        tables = _tables_for_application()
        tables["documents"] = [{
            "id": "deleted-doc", "application_id": "application-1",
            "document_type": "tax", "deleted_at": "2026-10-25T00:00:00Z",
        }]
        service = ApplicationService(TransactionalMemoryDB(tables), SimpleNamespace())
        result = await service._client_view(service.db.store, tables["applications"][0])
        assert "delete_document" not in result["available_actions"]

    asyncio.run(scenario())


def test_answer_information_preserves_original_consents_and_frozen_product():
    async def scenario():
        tables = _tables_for_application("NeedsInfo")
        app = tables["applications"][0]
        app["version"] = 5
        app["current_snapshot_id"] = "snapshot-1"
        app["data"].update(amount="12000.00", purpose="Inventory")
        financials = {
            "as_of_date": "2026-10-01", "monthly_revenue": "1500.00",
            "monthly_expenses": "1000.00", "outstanding_debt": "0.00",
            "monthly_debt_payments": "0.00",
        }
        app["data"]["financials"] = financials
        product = {**_product(), "required_document_types": []}
        tables["application_snapshots"] = [{
            "id": "snapshot-1", "application_id": app["id"], "revision": 1,
            "company": deepcopy(tables["companies"][0]), "product": product,
            "data": deepcopy(app["data"]), "document_ids": [],
            "consents": {"credit_history": True, "data_processing": True, "actor_id": "original-owner", "accepted_at": "2026-10-20T00:00:00Z"},
            "created_at": "2026-10-20T00:00:00Z",
        }]
        tables["information_requests"] = [{
            "id": "request-1", "application_id": app["id"], "snapshot_id": "snapshot-1",
            "status": "open", "message": "Debt", "requested_fields": ["/financials/outstanding_debt"],
            "required_document_types": [], "created_at": "2026-10-24T10:00:00Z",
        }]
        tables["loan_products"][0]["min_amount"] = "1000000.00"  # Catalog changed since submission.
        db = TransactionalMemoryDB(tables)
        service = ApplicationService(db, SimpleNamespace(credit_policy_version="demo-manual-v1"))
        command = {"expected_version": 5, "answer": {"comment": "Updated debt", "document_ids": [],
                   "data": {"financials": {**financials, "outstanding_debt": "100.00"}}}}
        result = await service.execute("answerInformationRequest", actor={"id": "client-1", "role": "client"},
                                       path={"application_id": "application-1", "request_id": "request-1"}, query={}, body=command)
        assert result["status"] == "Checking"
        assert result["version"] == 6
        assert len(db.store.tables["jobs"]) == 1
        assert db.store.tables["information_requests"][0]["status"] == "answered"
        snapshots = db.store.tables["application_snapshots"]
        assert snapshots[0]["data"]["financials"]["outstanding_debt"] == "0.00"
        assert snapshots[1]["data"]["financials"]["outstanding_debt"] == "100.00"
        assert snapshots[1]["consents"] == snapshots[0]["consents"]
        assert snapshots[1]["product"]["min_amount"] == "10000.00"

    asyncio.run(scenario())


@pytest.mark.parametrize("operation_id", ["listApplicationEvents", "listOffers", "listInformationRequests"])
def test_bank_role_cannot_use_client_only_listing_routes(operation_id):
    async def scenario():
        db = TransactionalMemoryDB(_tables_for_application("Submitted"))
        service = ApplicationService(db, SimpleNamespace(credit_policy_version="demo-manual-v1"))
        with pytest.raises(ApiError) as error:
            await service.execute(operation_id, actor={"id": "bank-1", "role": "bank_employee"},
                                  path={"application_id": "application-1"}, query={}, body={})
        assert error.value.status_code == 403

    asyncio.run(scenario())


def test_foreign_application_is_rejected_before_row_lock():
    async def scenario():
        class SpyStore(TransactionalMemoryStore):
            def __init__(self, tables):
                super().__init__(tables)
                self.locked = []

            async def get(self, table, item_id, lock=False):
                if lock:
                    self.locked.append((table, item_id))
                return await super().get(table, item_id, lock=lock)

        store = SpyStore(_tables_for_application())
        with pytest.raises(ApiError) as error:
            await require_application(store, {"id": "unrelated-client", "role": "client"},
                                      "application-1", lock=True)
        assert error.value.status_code == 404
        assert store.locked == []

    asyncio.run(scenario())
