"""Idempotent synthetic users, companies and product catalog for local demos."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid5

from credit.auth import hash_password
from credit.config import Settings
from credit.database import Database
from credit.storage import S3Storage

SEED_NAMESPACE = UUID('a89cd502-4119-4c62-90f0-d4610187a0cf')
CLIENT_ID = '10000000-0000-4000-8000-000000000001'
REPRESENTATIVE_ID = '10000000-0000-4000-8000-000000000002'
OTHER_ID = '10000000-0000-4000-8000-000000000003'
BANKER_ID = '10000000-0000-4000-8000-000000000004'
COMPANY_ID = '20000000-0000-4000-8000-000000000001'
OTHER_COMPANY_ID = '20000000-0000-4000-8000-000000000002'
WORKING_CAPITAL_ID = '30000000-0000-4000-8000-000000000001'
EQUIPMENT_ID = '30000000-0000-4000-8000-000000000002'

DEMO_CREDENTIALS = (
    (CLIENT_ID, 'client', 'demo-client-password', 'Представитель ООО Демобизнес', 'client'),
    (REPRESENTATIVE_ID, 'representative', 'demo-representative-password', 'Бухгалтер ООО Демобизнес', 'client'),
    (OTHER_ID, 'other', 'demo-other-password', 'Представитель другой компании', 'client'),
    (BANKER_ID, 'banker', 'demo-bank-password', 'Сотрудник демонстрационного банка', 'bank_employee'),
)


def _id(value: str) -> str:
    return str(uuid5(SEED_NAMESPACE, value))


def product_records() -> list[dict]:
    return [
        {
            'id': WORKING_CAPITAL_ID, 'code': 'working_capital', 'name': 'Оборотные средства',
            'description': 'Синтетический продукт для основного демонстрационного пути.',
            'currency': 'RUB', 'min_amount': '10000.00', 'max_amount': '5000000.00',
            'min_term_months': 3, 'max_term_months': 36,
            'required_document_types': [],
            'required_application_fields': ['/amount', '/term_months', '/purpose', '/financials'],
            'active': True,
        },
        {
            'id': EQUIPMENT_ID, 'code': 'equipment', 'name': 'Оборудование',
            'description': 'Синтетический продукт с обязательным документом и иными лимитами.',
            'currency': 'RUB', 'min_amount': '100000.00', 'max_amount': '10000000.00',
            'min_term_months': 6, 'max_term_months': 60,
            'required_document_types': ['financial_statement'],
            'required_application_fields': ['/amount', '/term_months', '/purpose'],
            'active': True,
        },
    ]


def validate_product(product: dict) -> None:
    """Reject catalog requirements that cannot be represented by this API."""
    from credit.validation import validate

    errors = validate({'$ref': '#/components/schemas/Product'}, product)
    if errors:
        raise ValueError(f'Invalid catalog product: {errors}')
    allowed = {
        '/product_id', '/amount', '/term_months', '/purpose', '/financials',
        '/financials/as_of_date', '/financials/monthly_revenue', '/financials/monthly_expenses',
        '/financials/outstanding_debt', '/financials/monthly_debt_payments',
    }
    fields = product['required_application_fields']
    types = product['required_document_types']
    if len(fields) != len(set(fields)) or any(field not in allowed for field in fields):
        raise ValueError('Product has duplicate or unknown application-field requirements')
    if len(types) != len(set(types)) or any(not kind or len(kind) > 100 for kind in types):
        raise ValueError('Product has invalid or duplicate document-type requirements')


async def seed_records(database: Database) -> None:
    """Create missing demo records; reruns do not reset passwords or user data."""
    created = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    # Password hashing is CPU-bound, and seed may also be called by async tests.
    users = []
    for id_, login, password, name, role in DEMO_CREDENTIALS:
        users.append({
            'id': id_, 'login': login, 'display_name': name, 'role': role,
            'password_hash': await asyncio.to_thread(hash_password, password),
            'active': True, 'created_at': created,
        })
    companies = [
        {
            'id': COMPANY_ID, 'version': 1, 'legal_type': 'legal_entity', 'inn': '0000000001',
            'name': 'ООО Демобизнес', 'registration_date': '2020-01-15', 'okved': '47.91',
            'address': 'Синтетический город, Демонстрационная улица, д. 1',
            'created_at': created, 'updated_at': created,
        },
        {
            'id': OTHER_COMPANY_ID, 'version': 1, 'legal_type': 'legal_entity', 'inn': '0000000010',
            'name': 'ООО Другая демокомпания', 'registration_date': '2021-05-20', 'okved': '62.01',
            'address': 'Синтетический город, Тестовая улица, д. 2',
            'created_at': created, 'updated_at': created,
        },
    ]
    members = [
        {'id': _id('member/client'), 'user_id': CLIENT_ID, 'company_id': COMPANY_ID, 'role': 'owner', 'created_at': created},
        {'id': _id('member/representative'), 'user_id': REPRESENTATIVE_ID, 'company_id': COMPANY_ID, 'role': 'representative', 'created_at': created},
        {'id': _id('member/other'), 'user_id': OTHER_ID, 'company_id': OTHER_COMPANY_ID, 'role': 'owner', 'created_at': created},
    ]
    products = product_records()
    for product in products:
        validate_product(product)
    async with database.transaction() as store:
        for table, records in [('users', users), ('companies', companies), ('company_members', members), ('loan_products', products)]:
            for record in records:
                if not await store.get(table, record['id']):
                    await store.insert(table, record)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-storage', action='store_true', help='Seed only PostgreSQL; skip local S3 bucket initialization')
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings.database_url)
    try:
        if not await database.check_ready():
            raise SystemExit('Database migrations are not current; run python -m credit.migrate first')
        if not args.skip_storage:
            await S3Storage(settings).ensure_bucket()
        await seed_records(database)
    finally:
        await database.close()
    print('Synthetic demo accounts are ready (local development only).')
    print('See README.md for local usernames and credentials.')


if __name__ == '__main__':
    asyncio.run(main())
