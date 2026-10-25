"""Application and bank-review commands.

All state changes are performed under the repository transaction and application
row lock. The service deliberately owns workflow rules; HTTP routing only maps
operation ids to this class.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from credit.common import (
    ApiError,
    application_view,
    audit,
    check_version,
    enqueue,
    event,
    new_id,
    now,
    page,
    require_application,
    require_company,
    require_role,
    touch,
)


_CLIENT_ROLES = ("client",)
_BANK_ROLE = ("bank_employee",)
_FIELD_POINTERS = {
    "/amount", "/term_months", "/purpose", "/financials",
    "/financials/as_of_date", "/financials/monthly_revenue",
    "/financials/monthly_expenses", "/financials/outstanding_debt",
    "/financials/monthly_debt_payments",
}


def _public(record: dict, fields: tuple[str, ...]) -> dict:
    return {key: deepcopy(record[key]) for key in fields if key in record}


def _merge(base: dict, updates: dict) -> dict:
    merged = deepcopy(base)
    for key, value in updates.items():
        if value is None:
            raise ApiError(400, "Значение null не поддерживается.", [{"field": f"/data/{key}", "message": "Поле не может быть null."}])
        merged[key] = deepcopy(value)
    return merged


def _pointer_value(value: dict, pointer: str) -> Any:
    node: Any = value
    for key in pointer.lstrip("/").split("/"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


class ApplicationService:
    def __init__(self, db: Any, settings: Any):
        self.db = db
        self.settings = settings

    async def execute(self, operation_id: str, *, actor: dict, path: dict, query: dict, body: dict | None) -> Any:
        handlers = {
            "createApplication": self._create,
            "listApplications": self._list,
            "getApplication": self._get,
            "updateApplication": self._update,
            "submitApplication": self._submit,
            "listApplicationEvents": self._events,
            "signApplication": self._sign,
            "listOffers": self._offers,
            "acceptOffer": lambda a, p, q, b: self._offer_response("acceptOffer", a, p, q, b),
            "declineOffer": lambda a, p, q, b: self._offer_response("declineOffer", a, p, q, b),
            "listInformationRequests": self._info_requests,
            "answerInformationRequest": self._answer_information,
            "listBankApplications": self._bank_list,
            "getBankApplication": self._bank_get,
            "listApplicationSnapshots": self._snapshots,
            "getApplicationSnapshot": self._snapshot,
            "listBankInformationRequests": self._bank_info_requests,
            "requestInformation": self._request_information,
            "approveApplication": self._approve,
            "rejectApplication": self._reject,
            "retryApplicationProcessing": self._retry,
        }
        handler = handlers.get(operation_id)
        if handler is None:
            raise ApiError(404, "Операция не поддерживается.")
        return await handler(actor, path, query, body or {})

    def _tx(self):
        return self.db.transaction()

    async def _create(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        async with self._tx() as store:
            await require_company(store, actor, body["company_id"])
            data = deepcopy(body.get("data", {}))
            _validate_loan(data, partial=True)
            await _validate_product(store, data, complete=False)
            app = {"id": new_id(), "company_id": body["company_id"], "status": "Draft", "version": 1,
                   "data": data, "created_at": now(), "updated_at": now()}
            await store.insert("applications", app)
            await event(store, app, actor, "application_created", "Черновик заявки создан.")
            return await self._client_view(store, app)

    async def _list(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        async with self._tx() as store:
            memberships = await store.list("company_members", user_id=actor["id"])
            company_ids = {m.get("company_id") for m in memberships}
            rows = await store.list("applications")
            rows = [r for r in rows if r.get("company_id") in company_ids]
            if query.get("company_id"):
                if query["company_id"] not in company_ids:
                    raise ApiError(404, "Компания не найдена.")
                rows = [r for r in rows if r["company_id"] == query["company_id"]]
            if query.get("status"):
                rows = [r for r in rows if r["status"] == query["status"]]
            rows.sort(key=lambda r: (r.get("created_at", ""), r["id"]), reverse=True)
            return page([await self._client_view(store, r) for r in rows], query)

    async def _get(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"])
            return await self._client_view(store, app)

    async def _update(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], lock=True)
            check_version(app, body["expected_version"])
            if app["status"] != "Draft":
                raise ApiError(409, "Изменять можно только черновик.")
            changes = body["data"]
            _validate_loan(changes, partial=True)
            merged = _merge(app.get("data", {}), changes)
            await _validate_product(store, merged, complete=False)
            app["data"] = merged
            await touch(store, app)
            await event(store, app, actor, "application_updated", "Данные черновика изменены.")
            return await self._client_view(store, app)

    async def _submit(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], lock=True)
            check_version(app, body["expected_version"])
            if app["status"] != "Draft":
                raise ApiError(409, "Подать можно только черновик.")
            data = deepcopy(app.get("data", {}))
            _validate_loan(data, partial=False)
            product = await _validate_product(store, data, complete=True)
            company = await store.get("companies", app["company_id"])
            # Logical deletions may still be queued for physical S3 cleanup. A
            # tombstone must never become part of an immutable review snapshot.
            docs = [
                document for document in await store.list("documents", application_id=app["id"])
                if document.get("deleted_at") is None
            ]
            ids = [d["id"] for d in docs]
            _check_required_docs(product, data, docs)
            consents = body.get("consents", {})
            if consents.get("credit_history") is not True or consents.get("data_processing") is not True:
                raise ApiError(400, "Для подачи необходимо принять все согласия.")
            snap = self._make_snapshot(app, company, data, product, ids, actor, consents, None)
            await store.insert("application_snapshots", snap)
            app.update(status="Submitted", current_snapshot_id=snap["id"])
            app["processing"] = {"stage": "checks", "status": "queued", "attempt": 0, "retry_available": False}
            await touch(store, app)
            await event(store, app, actor, "application_submitted", "Заявка подана и направлена на проверки.")
            await enqueue(store, app, "checks", "fns")
            return await self._client_view(store, app)

    def _make_snapshot(self, app: dict, company: dict, data: dict, product: dict, document_ids: list[str], actor: dict, consents: dict, revision: int | None) -> dict:
        # Product constraints are copied into the snapshot so later catalog edits cannot
        # change validation of an offer or an information-request response.
        consent_record = deepcopy(consents) if revision is not None else {
            "credit_history": True, "data_processing": True,
            "actor_id": actor["id"], "accepted_at": now(),
        }
        return {"id": new_id(), "application_id": app["id"], "revision": revision or 1,
                "company": deepcopy(company), "data": deepcopy(data), "document_ids": sorted(document_ids),
                "product": _product_view(product), "consents": consent_record, "created_at": now()}

    async def _client_view(self, store: Any, app: dict) -> dict:
        result = application_view(app)
        actions = result["available_actions"]
        docs = await store.list("documents", application_id=app["id"])
        snapshots = await store.list("application_snapshots", application_id=app["id"])
        frozen_ids = {doc_id for snap in snapshots for doc_id in snap.get("document_ids", [])}
        if not any(doc.get("deleted_at") is None and doc["id"] not in frozen_ids for doc in docs):
            actions[:] = [action for action in actions if action != "delete_document"]
        if app.get("status") == "NeedsInfo":
            requests = await store.list("information_requests", application_id=app["id"], status="open")
            if not requests:
                actions[:] = [action for action in actions if action != "answer_information_request"]
        if app.get("status") == "Offered":
            offers = await store.list("offers", application_id=app["id"], status="pending")
            if not offers:
                actions[:] = [action for action in actions if action not in ("accept_offer", "decline_offer")]
        return result

    async def _events(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        async with self._tx() as store:
            await require_application(store, actor, path["application_id"])
            rows = await store.list("application_events", application_id=path["application_id"])
            rows.sort(key=lambda r: (r.get("created_at", ""), r["id"]))
            return page([_public(x, ("id", "application_id", "type", "status", "message", "actor_type", "created_at")) for x in rows], query)

    async def _sign(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], lock=True)
            check_version(app, body["expected_version"])
            if app["status"] != "Accepted":
                raise ApiError(409, "Подписать можно только принятую заявку.")
            app["status"] = "Signed"
            app["processing"] = {"stage": "disbursement", "status": "queued", "attempt": 0, "retry_available": False}
            await touch(store, app)
            await event(store, app, actor, "application_signed", "Заявка подписана; запущена имитация выдачи.")
            await enqueue(store, app, "disbursement", "disbursement")
            return await self._client_view(store, app)

    async def _offers(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        async with self._tx() as store:
            await require_application(store, actor, path["application_id"])
            rows = await store.list("offers", application_id=path["application_id"])
            rows.sort(key=lambda x: (x.get("created_at", ""), x["id"]))
            return page([_public(x, ("id", "application_id", "decision_id", "status", "terms", "created_at", "responded_at")) for x in rows], query)

    async def _offer_response(self, operation_id: str, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        accept = operation_id == "acceptOffer"
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], lock=True)
            check_version(app, body["expected_version"])
            offer = await store.get("offers", path["offer_id"], lock=True)
            if not offer or offer.get("application_id") != app["id"]:
                raise ApiError(404, "Предложение не найдено.")
            if app["status"] != "Offered" or offer.get("status") != "pending":
                raise ApiError(409, "Предложение больше недоступно.")
            offer.update(status="accepted" if accept else "declined", responded_at=now())
            await store.save("offers", offer)
            app["status"] = "Accepted" if accept else "Declined"
            await touch(store, app)
            await event(store, app, actor, "offer_accepted" if accept else "offer_declined", "Предложение принято." if accept else "Предложение отклонено.")
            return await self._client_view(store, app)

    async def _info_requests(self, actor: dict, path: dict, query: dict, body: dict, *, bank: bool = False) -> dict:
        require_role(actor, *_BANK_ROLE if bank else _CLIENT_ROLES)
        async with self._tx() as store:
            await require_application(store, actor, path["application_id"], bank=bank)
            rows = await store.list("information_requests", application_id=path["application_id"])
            rows.sort(key=lambda x: (x.get("created_at", ""), x["id"]))
            return page([_public(x, ("id", "application_id", "status", "message", "requested_fields", "required_document_types", "answer", "created_at", "answered_at")) for x in rows], query)

    async def _answer_information(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_CLIENT_ROLES)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], lock=True)
            check_version(app, body["expected_version"])
            if app["status"] != "NeedsInfo":
                raise ApiError(409, "Заявка не ожидает уточнений.")
            req = await store.get("information_requests", path["request_id"], lock=True)
            if not req or req.get("application_id") != app["id"]:
                raise ApiError(404, "Запрос уточнений не найден.")
            if req.get("status") != "open":
                raise ApiError(409, "Запрос уточнений уже закрыт.")
            answer = deepcopy(body["answer"])
            data = _merge(app.get("data", {}), answer.get("data", {}))
            _validate_loan(data, partial=False)
            product = await _snapshot_product(store, app)
            _validate_product_limits(product, data)
            for pointer in product.get("required_application_fields", []):
                if _pointer_value(data, pointer) in (None, ""):
                    raise ApiError(400, f"Не заполнено обязательное поле {pointer}.")
            docs = await store.list("documents", application_id=app["id"])
            by_id = {d["id"]: d for d in docs if d.get("deleted_at") is None}
            selected = answer["document_ids"]
            if len(set(selected)) != len(selected) or any(i not in by_id for i in selected):
                raise ApiError(400, "Набор документов содержит повтор или чужой документ.")
            old_snap = await store.get("application_snapshots", req.get("snapshot_id") or app.get("current_snapshot_id"))
            old_ids = set(old_snap.get("document_ids", [])) if old_snap else set()
            new_docs = [by_id[i] for i in selected]
            _check_required_docs(product, data, new_docs)
            for kind in req.get("required_document_types", []):
                if not any(d.get("document_type") == kind and d["id"] not in old_ids for d in new_docs):
                    raise ApiError(400, f"Требуется новый документ типа {kind}.")
            for pointer in req.get("requested_fields", []):
                explicit = answer.get("data", {})
                if _pointer_value(explicit, pointer) is None:
                    raise ApiError(400, f"Не передано запрошенное поле {pointer}.")
            req.update(status="answered", answer=answer, answered_at=now())
            await store.save("information_requests", req)
            company = await store.get("companies", app["company_id"])
            previous = await store.list("application_snapshots", application_id=app["id"])
            if old_snap is None:
                raise RuntimeError("Missing information request source snapshot")
            snap = self._make_snapshot(app, company, data, product, selected, actor,
                                       old_snap["consents"], len(previous)+1)
            await store.insert("application_snapshots", snap)
            app.update(data=data, status="Checking", current_snapshot_id=snap["id"],
                       processing={"stage": "checks", "status": "queued", "attempt": 0, "retry_available": False})
            await touch(store, app)
            await event(store, app, actor, "information_answered", "Ответ на запрос уточнений принят.")
            await enqueue(store, app, "checks", "fns")
            return await self._client_view(store, app)

    async def _bank_list(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_BANK_ROLE)
        async with self._tx() as store:
            rows = await store.list("applications")
            rows = [r for r in rows if r.get("status") != "Draft"]
            if query.get("company_id"):
                rows = [r for r in rows if r.get("company_id") == query["company_id"]]
            if query.get("status"):
                rows = [r for r in rows if r.get("status") == query["status"]]
            if query.get("processing_status"):
                rows = [r for r in rows if r.get("processing", {}).get("status") == query["processing_status"]]
            rows.sort(key=lambda r: (r.get("updated_at", ""), r["id"]))
            return page([_queue_view(r) for r in rows], query)

    async def _bank_get(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_BANK_ROLE)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], bank=True)
            return await _bank_view(store, app)

    async def _snapshots(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_BANK_ROLE)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], bank=True)
            rows = await store.list("application_snapshots", application_id=app["id"])
            rows.sort(key=lambda x: x.get("revision", 0))
            return page([_snapshot_view(x) for x in rows], query)

    async def _snapshot(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_BANK_ROLE)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], bank=True)
            item = await store.get("application_snapshots", path["snapshot_id"])
            if not item or item.get("application_id") != app["id"]:
                raise ApiError(404, "Снимок не найден.")
            return _snapshot_view(item)

    async def _bank_info_requests(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        return await self._info_requests(actor, path, query, body, bank=True)

    async def _request_information(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_BANK_ROLE)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], bank=True, lock=True)
            check_version(app, body["expected_version"])
            if app["status"] != "ManualReview":
                raise ApiError(409, "Запрос уточнений доступен только на ручном рассмотрении.")
            fields = body.get("requested_fields", [])
            types = body.get("required_document_types", [])
            if (not fields and not types) or any(f not in _FIELD_POINTERS for f in fields):
                raise ApiError(400, "Укажите допустимые поля или типы новых документов.")
            existing = await store.list("information_requests", application_id=app["id"], status="open")
            if existing:
                raise ApiError(409, "У заявки уже есть открытый запрос уточнений.")
            req = {"id": new_id(), "application_id": app["id"], "snapshot_id": app["current_snapshot_id"],
                   "status": "open", "message": body["message"], "requested_fields": fields,
                   "required_document_types": types, "created_at": now()}
            await store.insert("information_requests", req)
            app["status"] = "NeedsInfo"
            app.pop("processing", None)
            await touch(store, app)
            await event(store, app, actor, "information_requested", "Банк запросил уточнения.")
            return {"information_request": _public(req, ("id", "application_id", "status", "message", "requested_fields", "required_document_types", "created_at")), "application": await self._client_view(store, app)}

    async def _approve(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_BANK_ROLE)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], bank=True, lock=True)
            check_version(app, body["expected_version"])
            await self._check_manual_review(store, app)
            snap = await store.get("application_snapshots", app["current_snapshot_id"])
            if snap is None or "product" not in snap:
                raise RuntimeError("Missing current snapshot product")
            product = snap["product"]
            terms = deepcopy(body["terms"])
            if terms.get("product_id") != app.get("data", {}).get("product_id"):
                raise ApiError(400, "Предложение должно относиться к выбранному продукту.")
            if terms.get("currency") != product.get("currency", "RUB"):
                raise ApiError(400, "Валюта предложения не соответствует продукту.")
            _validate_product_limits(product, {"amount": terms["amount"], "term_months": terms["term_months"]})
            decision = self._decision(app, actor, body, "offer")
            await store.insert("credit_decisions", decision)
            offer = {"id": new_id(), "application_id": app["id"], "decision_id": decision["id"], "status": "pending", "terms": terms, "created_at": now()}
            await store.insert("offers", offer)
            app.update(status="Offered", decision={"id": decision["id"], "outcome": "offer", "message": decision["client_message"], "decided_at": decision["created_at"]}, processing={"stage": "decision", "status": "completed", "attempt": 0, "retry_available": False})
            await touch(store, app)
            await event(store, app, actor, "application_approved", "Банк сформировал предложение.")
            return await _bank_view(store, app)

    async def _reject(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_BANK_ROLE)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], bank=True, lock=True)
            check_version(app, body["expected_version"])
            await self._check_manual_review(store, app)
            decision = self._decision(app, actor, body, "reject")
            await store.insert("credit_decisions", decision)
            app.update(status="Rejected", decision={"id": decision["id"], "outcome": "reject", "message": decision["client_message"], "decided_at": decision["created_at"]}, processing={"stage": "decision", "status": "completed", "attempt": 0, "retry_available": False})
            await touch(store, app)
            await event(store, app, actor, "application_rejected", "Заявка отклонена банком.")
            return await _bank_view(store, app)

    async def _check_manual_review(self, store: Any, app: dict) -> None:
        if app["status"] != "ManualReview":
            raise ApiError(409, "Решение доступно только при ручном рассмотрении.")
        open_requests = await store.list("information_requests", application_id=app["id"], status="open")
        jobs = await store.list("jobs", application_id=app["id"])
        jobs = [j for j in jobs if j.get("status") in ("queued", "running")]
        if open_requests or jobs:
            raise ApiError(409, "Нельзя принять решение при открытом запросе или активной обработке.")
        snap_id = app.get("current_snapshot_id")
        for table in ("check_runs", "scoring_results"):
            rows = await store.list(table, application_id=app["id"], snapshot_id=snap_id)
            sources = {r.get("source") for r in rows if r.get("status") == "succeeded"}
            if table == "check_runs" and not {"fns", "bki"}.issubset(sources):
                raise ApiError(409, "Проверки текущего снимка ещё не завершены.")
            if table == "scoring_results" and not rows:
                raise ApiError(409, "Скоринг текущего снимка ещё не завершён.")

    def _decision(self, app: dict, actor: dict, body: dict, outcome: str) -> dict:
        policy = self.settings.credit_policy_version
        return {"id": new_id(), "application_id": app["id"], "snapshot_id": app["current_snapshot_id"],
                "outcome": outcome, "source": "manual", "policy_version": policy,
                "reason_codes": sorted(set(body.get("reason_codes", []))), "internal_comment": body.get("internal_comment"),
                "client_message": body["client_message"], "author_id": actor["id"], "created_at": now()}

    async def _retry(self, actor: dict, path: dict, query: dict, body: dict) -> dict:
        require_role(actor, *_BANK_ROLE)
        async with self._tx() as store:
            app = await require_application(store, actor, path["application_id"], bank=True, lock=True)
            check_version(app, body["expected_version"])
            proc = app.get("processing", {})
            if app.get("status") not in ("Checking", "Scoring", "Signed") or proc.get("status") != "blocked" or not proc.get("retry_available"):
                raise ApiError(409, "Обработка сейчас не требует ручного возобновления.")
            jobs = await store.list("jobs", application_id=app["id"])
            jobs = [j for j in jobs if j.get("status") in ("queued", "running")]
            if jobs:
                raise ApiError(409, "Для заявки уже есть активное задание.")
            old = await store.list("jobs", application_id=app["id"], snapshot_id=app["current_snapshot_id"], status="blocked")
            if not old:
                raise ApiError(409, "Не найдено задание, которое можно возобновить.")
            job = sorted(old, key=lambda j: j.get("created_at", ""))[-1]
            job.update(status="queued", cycle_attempt=0, run_at=now(), lease_until=None, claim_id=None)
            await store.save("jobs", job)
            proc.update(status="queued", retry_available=False)
            proc.pop("error_code", None)
            proc.pop("next_retry_at", None)
            app["processing"] = proc
            await touch(store, app)
            await event(store, app, actor, "processing_retried", "Сотрудник возобновил обработку.")
            await audit(store, actor, "application.retry_processing", "application", app["id"], "success", job_id=job["id"], snapshot_id=job.get("snapshot_id"))
            return await _bank_view(store, app)


async def _validate_product(store: Any, data: dict, complete: bool) -> dict | None:
    product_id = data.get("product_id")
    if not product_id:
        if complete:
            raise ApiError(400, "Выберите кредитный продукт.", [{"field": "/data/product_id", "message": "Обязательное поле."}])
        return None
    product = await store.get("loan_products", product_id)
    if product is None:
        raise ApiError(404, "Кредитный продукт не найден.")
    if complete:
        if not product.get("active", True):
            raise ApiError(400, "Выбранный кредитный продукт больше не доступен.")
        _validate_product_limits(product, data)
        for pointer in product.get("required_application_fields", []):
            value = _pointer_value(data, pointer)
            if value is None or value == "":
                raise ApiError(400, f"Не заполнено обязательное поле {pointer}.", [{"field": f"/data{pointer}", "message": "Обязательное поле продукта."}])
    return product


def _validate_product_limits(product: dict, data: dict) -> None:
    amount, term = data.get("amount"), data.get("term_months")
    if amount is not None:
        try:
            value = Decimal(amount)
            if value <= 0:
                raise ApiError(400, "Сумма должна быть положительной.", [{"field": "/data/amount", "message": "Сумма должна быть положительной."}])
            if value < Decimal(str(product["min_amount"])) or value > Decimal(str(product["max_amount"])):
                raise ApiError(400, "Сумма вне диапазона продукта.", [{"field": "/data/amount", "message": "Сумма вне диапазона продукта."}])
        except (InvalidOperation, TypeError, KeyError):
            raise ApiError(400, "Некорректная сумма заявки.")
    if term is not None and not (int(product["min_term_months"]) <= int(term) <= int(product["max_term_months"])):
        raise ApiError(400, "Срок вне диапазона продукта.", [{"field": "/data/term_months", "message": "Срок вне диапазона продукта."}])
    if data.get("currency", product.get("currency", "RUB")) != product.get("currency", "RUB"):
        raise ApiError(400, "Валюта не соответствует продукту.")


def _validate_loan(data: dict, partial: bool) -> None:
    allowed = {"product_id", "amount", "term_months", "purpose", "financials"}
    extra = data.keys() - allowed
    if extra:
        raise ApiError(400, "Неизвестные поля данных заявки.", [{"field": f"/data/{k}", "message": "Неизвестное поле."} for k in sorted(extra)])
    if any(v is None for v in data.values()):
        raise ApiError(400, "Значение null не поддерживается.")
    if not partial:
        for key in ("amount", "term_months"):
            if key not in data:
                raise ApiError(400, f"Не заполнено поле {key}.", [{"field": f"/data/{key}", "message": "Обязательное поле."}])


def _check_required_docs(product: dict, data: dict, docs: list[dict]) -> None:
    types = {d.get("document_type") for d in docs}
    missing = set(product.get("required_document_types", [])) - types
    if missing:
        raise ApiError(400, "Не приложены обязательные документы.", [{"field": "/documents", "message": f"Требуется документ типа {kind}."} for kind in sorted(missing)])


async def _snapshot_product(store: Any, app: dict) -> dict:
    if not app.get("current_snapshot_id"):
        raise RuntimeError("Submitted application has no current snapshot")
    snap = await store.get("application_snapshots", app["current_snapshot_id"])
    if not snap or not snap.get("product"):
        raise RuntimeError("Current snapshot has no frozen product rules")
    return snap["product"]


def _snapshot_view(snap: dict) -> dict:
    return {"id": snap["id"], "application_id": snap["application_id"], "revision": snap["revision"],
            "company": _company_view(snap.get("company", {})), "data": snap.get("data", {}),
            "document_ids": snap.get("document_ids", []), "created_at": snap["created_at"]}


def _queue_view(app: dict) -> dict:
    result = application_view(app, bank=True)
    # The queue response is ApplicationPage, not BankApplication: no internal
    # processing, checks, decisions, or scoring data belongs in list items.
    return {key: value for key, value in result.items() if key in {
        "id", "company_id", "status", "version", "data", "current_snapshot_id",
        "decision", "created_at", "updated_at", "available_actions",
    }}


def _company_view(company: dict) -> dict:
    fields = ("id", "version", "legal_type", "inn", "name", "registration_date", "okved", "address", "created_at", "updated_at")
    return _public(company, fields)


def _product_view(product: dict) -> dict:
    fields = ("id", "code", "name", "description", "currency", "min_amount", "max_amount",
              "min_term_months", "max_term_months", "required_document_types",
              "required_application_fields", "active")
    return _public(product, fields)


async def _bank_view(store: Any, app: dict) -> dict:
    result = application_view(app, bank=True)
    snap_id = app.get("current_snapshot_id")
    runs = await store.list("check_runs", snapshot_id=snap_id) if snap_id else []
    scoring = await store.list("scoring_results", snapshot_id=snap_id) if snap_id else []
    decisions = await store.list("credit_decisions", application_id=app["id"])
    result["checks"] = [_public(r, ("id", "snapshot_id", "source", "status", "attempt", "result", "error_code", "retryable", "started_at", "finished_at")) for r in runs]
    result["decisions"] = [_public(d, ("id", "snapshot_id", "outcome", "source", "policy_version", "reason_codes", "internal_comment", "client_message", "author_id", "created_at")) for d in decisions]
    if scoring:
        result["scoring_result"] = _public(scoring[-1], ("id", "snapshot_id", "request_id", "model_version", "score", "scale", "factors", "created_at"))
    if app.get("processing"):
        result["processing"] = _public(app["processing"], ("stage", "status", "attempt", "next_retry_at", "error_code", "retry_available"))
    result["available_bank_actions"] = _bank_actions(app)
    return result


def _bank_actions(app: dict) -> list[str]:
    status, processing = app.get("status"), app.get("processing", {})
    if status == "ManualReview":
        return ["request_information", "approve", "reject"]
    if status in ("Checking", "Scoring", "Signed") and processing.get("status") == "blocked" and processing.get("retry_available"):
        return ["retry_processing"]
    return []
