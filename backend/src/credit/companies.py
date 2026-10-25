"""Company profile and loan product catalog services."""

from __future__ import annotations

from typing import Any

from psycopg import IntegrityError
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError

from credit.common import ApiError, audit, check_version, new_id, now, require_company, require_role

COMPANY_FIELDS = (
    "id", "version", "legal_type", "inn", "name", "registration_date", "okved",
    "address", "created_at", "updated_at",
)
PRODUCT_FIELDS = (
    "id", "code", "name", "description", "currency", "min_amount", "max_amount",
    "min_term_months", "max_term_months", "required_document_types",
    "required_application_fields", "active",
)


def _public(entity: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: entity[field] for field in fields if field in entity}


def _utc_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        result = value.isoformat()
        return result.replace("+00:00", "Z")
    return str(value)


def _inn_matches_type(legal_type: str, inn: str) -> bool:
    return (legal_type == "legal_entity" and len(inn) == 10) or (
        legal_type == "individual_entrepreneur" and len(inn) == 12
    )


class CompanyService:
    def __init__(self, db: Any):
        self.db = db

    async def list_companies(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        require_role(actor, "client")
        async with self.db.transaction() as store:
            memberships = await store.list("company_members", user_id=actor["id"])
            companies = []
            for member in memberships:
                company = await store.get("companies", member["company_id"])
                if company:
                    companies.append(_public(company, COMPANY_FIELDS))
            return sorted(companies, key=lambda item: (item["created_at"], item["id"]), reverse=True)

    async def get_company(self, actor: dict[str, Any], company_id: str) -> dict[str, Any]:
        require_role(actor, "client")
        async with self.db.transaction() as store:
            company = await require_company(store, actor, company_id)
            return _public(company, COMPANY_FIELDS)

    async def create_company(self, actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        require_role(actor, "client")
        if not _inn_matches_type(body["legal_type"], body["inn"]):
            raise ApiError(400, "ИНН не соответствует типу компании.", [{"field": "/inn", "message": "Длина ИНН не соответствует legal_type."}])
        timestamp = now()
        company = {
            "id": new_id(), "version": 1,
            "legal_type": body["legal_type"], "inn": body["inn"], "name": body["name"],
            "registration_date": body["registration_date"], "okved": body["okved"],
            "address": body["address"],
            "created_at": _utc_text(timestamp), "updated_at": _utc_text(timestamp),
        }
        async with self.db.transaction() as store:
            try:
                created = await store.insert("companies", company)
                await store.insert("company_members", {
                    "id": new_id(), "company_id": created["id"], "user_id": actor["id"],
                    "role": "owner", "created_at": _utc_text(timestamp),
                })
                await audit(store, actor, "company_created", "company", created["id"])
            except (IntegrityError, SQLAlchemyIntegrityError) as exc:
                raise ApiError(409, "Компания с таким ИНН уже существует.", []) from exc
            return _public(created, COMPANY_FIELDS)

    async def update_company(
        self, actor: dict[str, Any], company_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        require_role(actor, "client")
        async with self.db.transaction() as store:
            # Check membership before locking the row: a foreign company ID
            # must not let a client hold up another company's updates.
            await require_company(store, actor, company_id, owner=True)
            # Lock before comparing version so concurrent PATCH requests cannot both win.
            company = await store.get("companies", company_id, lock=True)
            if company is None:
                raise ApiError(404, "Компания не найдена.", [])
            check_version(company, body["expected_version"])
            changes = {key: value for key, value in body.items() if key != "expected_version"}
            if not changes:
                raise ApiError(400, "Не указаны поля для изменения.", [])
            if "legal_type" in changes and not _inn_matches_type(changes["legal_type"], company["inn"]):
                raise ApiError(400, "ИНН не соответствует типу компании.", [{"field": "/legal_type", "message": "Тип не соответствует длине неизменяемого ИНН."}])
            company.update(changes)
            company["version"] += 1
            company["updated_at"] = _utc_text(now())
            updated = await store.save("companies", company)
            await audit(store, actor, "company_updated", "company", company_id)
            return _public(updated, COMPANY_FIELDS)

    async def list_products(self, actor: dict[str, Any], limit: int = 20, offset: int = 0) -> dict[str, Any]:
        require_role(actor, "client", "bank_employee")
        async with self.db.transaction() as store:
            products = await store.list("loan_products", active=True)
            products.sort(key=lambda item: (item.get("code", ""), item["id"]))
            total = len(products)
            return {
                "items": [_public(item, PRODUCT_FIELDS) for item in products[offset:offset + limit]],
                "limit": limit, "offset": offset, "total": total,
            }

    async def get_product(self, actor: dict[str, Any], product_id: str) -> dict[str, Any]:
        require_role(actor, "client", "bank_employee")
        async with self.db.transaction() as store:
            product = await store.get("loan_products", product_id)
            if not product or not product.get("active"):
                raise ApiError(404, "Продукт не найден.", [])
            return _public(product, PRODUCT_FIELDS)

    async def execute(
        self, operation_id: str, *, actor: dict[str, Any], path: dict[str, Any],
        query: dict[str, Any], body: dict[str, Any] | None,
    ) -> Any:
        if operation_id == "listCompanies":
            return await self.list_companies(actor)
        if operation_id == "getCompany":
            return await self.get_company(actor, path["company_id"])
        if operation_id == "createCompany":
            return await self.create_company(actor, body or {})
        if operation_id == "updateCompany":
            return await self.update_company(actor, path["company_id"], body or {})
        if operation_id == "listProducts":
            return await self.list_products(actor, query.get("limit", 20), query.get("offset", 0))
        if operation_id == "getProduct":
            return await self.get_product(actor, path["product_id"])
        raise ApiError(404, "Операция не найдена.", [])
