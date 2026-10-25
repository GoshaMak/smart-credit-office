"""PostgreSQL, S3, HTTP and real TCP mock integration acceptance checks.

The suite runs only when TEST_INTEGRATION=1. It requires a separate database
named `credit_test` and a dedicated S3 bucket beginning with `credit-tests-`.
It creates unique records and objects and never truncates or drops test data.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, AsyncIterator

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from credit.auth import hash_password
from credit.common import now
from credit.config import Settings
from credit.database import Database
from credit.storage import S3Storage
from credit.worker import Worker


PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
TEST_PRODUCT = "30000000-0000-4000-8000-"  # Completed with a unique suffix.


@dataclass
class IntegrationEnvironment:
    database: Database
    settings: Settings
    storage: S3Storage
    mock_url: str


@pytest.fixture
def integration_environment() -> IntegrationEnvironment:
    if os.getenv("TEST_INTEGRATION") != "1":
        pytest.skip("set TEST_INTEGRATION=1 to opt into PostgreSQL and S3 integration tests")

    required = ("TEST_DATABASE_URL", "TEST_DATABASE_CONFIRM", "TEST_S3_ENDPOINT", "TEST_DOCUMENT_BUCKET")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.fail("TEST_INTEGRATION=1 requires: " + ", ".join(missing))

    # credit.main exposes an ASGI app at module import time. Give that unused
    # module-level app a valid test-only key before importing it in _client.
    os.environ.setdefault(
        "JWT_SECRET",
        base64.b64encode(b"integration-only-key-not-used-by-test-app").decode("ascii"),
    )

    from sqlalchemy.engine import make_url

    database_url = os.environ["TEST_DATABASE_URL"]
    if make_url(database_url).database != "credit_test" or os.environ["TEST_DATABASE_CONFIRM"] != "credit_test":
        pytest.fail("Integration tests require an explicit database named credit_test and TEST_DATABASE_CONFIRM=credit_test")
    endpoint = os.environ["TEST_S3_ENDPOINT"]
    if not endpoint.startswith(("http://localhost:", "http://127.0.0.1:", "http://localstack:")):
        pytest.fail("TEST_S3_ENDPOINT must point to local LocalStack")
    bucket = os.environ["TEST_DOCUMENT_BUCKET"]
    if not bucket.startswith("credit-tests-"):
        pytest.fail("TEST_DOCUMENT_BUCKET must start with credit-tests-")

    engine = create_async_engine(database_url, poolclass=NullPool, pool_pre_ping=True)
    database = Database(database_url, engine=engine)
    settings = Settings(
        database_url=database_url,
        jwt_secret=b"stage-zero-integration-test-secret-32-bytes",
        jwt_issuer="credit-backend", jwt_audience="credit-api",
        s3_endpoint=endpoint, s3_bucket=bucket,
        s3_region=os.getenv("TEST_S3_REGION", "us-east-1"),
        s3_access_key=os.getenv("TEST_S3_ACCESS_KEY", "test"),
        s3_secret_key=os.getenv("TEST_S3_SECRET_KEY", "test"),
        adapter_timeout=1, lease_seconds=5, max_attempts=2,
        retry_delays=(0.0,), poll_interval=0.01,
    )
    storage = S3Storage(settings)

    async def check() -> None:
        if not await database.check_ready():
            pytest.fail("Apply the current migrations to the isolated credit_test database before running integration tests")
        await storage.ensure_bucket()

    try:
        asyncio.run(check())
    except Exception:
        asyncio.run(engine.dispose())
        raise

    try:
        yield IntegrationEnvironment(database, settings, storage, "")
    finally:
        # NullPool prevents connections crossing asyncio.run event loops.
        asyncio.run(engine.dispose())


@pytest.fixture
def integration_mock(integration_environment: IntegrationEnvironment) -> str:
    from credit import mock

    mock.state.reset()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mock.app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        pytest.fail("local integration mock did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


def _run(coro):
    return asyncio.run(coro)


@asynccontextmanager
async def _client(environment: IntegrationEnvironment) -> AsyncIterator[httpx.AsyncClient]:
    from credit.main import create_app

    app = create_app(settings=environment.settings, database=environment.database, storage=environment.storage)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backend.test"
        ) as client:
            yield client


async def _new_user(database: Database, role: str) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    login = f"integration-{role}-{uuid.uuid4().hex}"
    password = f"test-{uuid.uuid4().hex}-safe"
    async with database.transaction() as store:
        await store.insert("users", {
            "id": user_id, "login": login, "password_hash": hash_password(password),
            "display_name": f"Integration {role}", "role": role, "active": True,
            "created_at": now(),
        })
    return login, password


async def _access(client: httpx.AsyncClient, login: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"
    return response.json()["access_token"]


async def _create_product(database: Database) -> dict[str, Any]:
    product_id = TEST_PRODUCT + uuid.uuid4().hex[:12]
    product = {
        "id": product_id, "code": f"integration-{uuid.uuid4().hex[:12]}",
        "name": "Синтетический продукт интеграционного теста",
        "description": "Суммы, срок и документ ограничены тестовыми значениями.",
        "currency": "RUB", "min_amount": "10000.00", "max_amount": "500000.00",
        "min_term_months": 3, "max_term_months": 36,
        "required_document_types": ["financial_statement"],
        "required_application_fields": ["/purpose"], "active": True,
        "created_at": now(),
    }
    async with database.transaction() as store:
        await store.insert("loan_products", product)
    return product


async def _create_company(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    body = {
        "legal_type": "legal_entity", "inn": f"{uuid.uuid4().int % 10**10:010d}",
        "name": "Синтетическая компания интеграционного теста",
        "registration_date": "2020-01-15", "okved": "47.91",
        "address": "Тестовый адрес, не содержащий персональных данных",
    }
    response = await client.post("/api/v1/companies", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _create_application(
    client: httpx.AsyncClient,
    token: str,
    company: dict[str, Any],
    product: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post("/api/v1/applications", headers=headers, json={
        "company_id": company["id"], "data": {"product_id": product["id"]},
    })
    assert created.status_code == 201, created.text
    app = created.json()
    edited = await client.patch(f"/api/v1/applications/{app['id']}", headers=headers, json={
        "expected_version": app["version"],
        "data": {
            "amount": "100000.00", "term_months": 12,
            "purpose": "Закупка товарных запасов для интеграционного сценария",
            "financials": {
                "as_of_date": "2026-10-01", "monthly_revenue": "1500000.00",
                "monthly_expenses": "900000.00", "outstanding_debt": "500000.00",
                "monthly_debt_payments": "50000.00",
            },
        },
    })
    assert edited.status_code == 200, edited.text
    app = edited.json()
    uploaded = await client.post(
        f"/api/v1/applications/{app['id']}/documents", headers=headers,
        data={"expected_version": str(app["version"]), "document_type": "financial_statement"},
        files={"file": ("synthetic-financial-statement.pdf", PDF, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    app = await _get_application(client, token, app["id"])
    return app, uploaded.json()["document"]


async def _get_application(client: httpx.AsyncClient, token: str, app_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/applications/{app_id}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()


async def _submit_and_run_checks(
    environment: IntegrationEnvironment,
    client: httpx.AsyncClient,
    client_token: str,
    bank_token: str,
    app: dict[str, Any],
    mock_url: str,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {client_token}"}
    submitted = await client.post(
        f"/api/v1/applications/{app['id']}/submit", headers=headers,
        json={"expected_version": app["version"], "consents": {"credit_history": True, "data_processing": True}},
    )
    assert submitted.status_code == 202, submitted.text
    duplicate = await client.post(
        f"/api/v1/applications/{app['id']}/submit", headers=headers,
        json={"expected_version": app["version"], "consents": {"credit_history": True, "data_processing": True}},
    )
    assert duplicate.status_code == 409

    worker_settings = environment.settings
    worker_settings.mock_url = mock_url
    worker = Worker(environment.database, worker_settings)
    for _ in range(4):
        if not await worker.run_once():
            break
    current = await _get_application(client, client_token, app["id"])
    assert current["status"] == "ManualReview", current

    bank = await client.get(f"/api/v1/bank/applications/{app['id']}", headers={"Authorization": f"Bearer {bank_token}"})
    assert bank.status_code == 200, bank.text
    detail = bank.json()
    assert {row["source"] for row in detail["checks"] if row["status"] == "succeeded"} == {"fns", "bki"}
    assert detail["scoring_result"]["model_version"] == "stub-v1"
    assert detail["available_bank_actions"] == ["request_information", "approve", "reject"]
    return current


@pytest.mark.integration
def test_postgres_http_s3_mock_and_worker_complete_a_credit_application(
    integration_environment: IntegrationEnvironment, integration_mock: str,
) -> None:
    async def scenario() -> None:
        env = integration_environment
        client_login, client_password = await _new_user(env.database, "client")
        bank_login, bank_password = await _new_user(env.database, "bank_employee")
        product = await _create_product(env.database)
        async with _client(env) as client:
            client_token = await _access(client, client_login, client_password)
            bank_token = await _access(client, bank_login, bank_password)
            company = await _create_company(client, client_token)
            app, document = await _create_application(client, client_token, company, product)

            bank_draft = await client.get(
                f"/api/v1/bank/applications/{app['id']}", headers={"Authorization": f"Bearer {bank_token}"}
            )
            assert bank_draft.status_code == 404

            app = await _submit_and_run_checks(env, client, client_token, bank_token, app, integration_mock)
            app_headers = {"Authorization": f"Bearer {client_token}"}
            download = await client.get(
                f"/api/v1/applications/{app['id']}/documents/{document['id']}/content", headers=app_headers
            )
            assert download.status_code == 200
            assert download.content == PDF
            assert download.headers.get("cache-control") == "no-store"
            assert "<script>" not in download.headers.get("content-disposition", "").lower()

            bank_headers = {"Authorization": f"Bearer {bank_token}"}
            bank = await client.get(f"/api/v1/bank/applications/{app['id']}", headers=bank_headers)
            current_version = bank.json()["version"]
            decision = await client.post(f"/api/v1/bank/applications/{app['id']}/approve", headers=bank_headers, json={
                "expected_version": current_version,
                "terms": {
                    "product_id": product["id"], "amount": "90000.00", "currency": "RUB",
                    "term_months": 11, "annual_rate_percent": "18.50",
                    "conditions": ["Синтетическое условие"],
                },
                "reason_codes": ["MANUAL_APPROVAL", "REDUCED_AMOUNT"],
                "internal_comment": "Тестовый внутренний комментарий.",
                "client_message": "Синтетическое предложение для интеграционного теста.",
            })
            assert decision.status_code == 200, decision.text
            assert decision.json()["decisions"][0]["author_id"] is not None

            public = await _get_application(client, client_token, app["id"])
            assert public["decision"]["message"] == "Синтетическое предложение для интеграционного теста."
            assert "processing" not in public and "scoring_result" not in public and "checks" not in public
            assert "internal_comment" not in str(public)
            offers = await client.get(f"/api/v1/applications/{app['id']}/offers", headers=app_headers)
            offer_id = offers.json()["items"][0]["id"]
            accept = await client.post(
                f"/api/v1/applications/{app['id']}/offers/{offer_id}/accept", headers=app_headers,
                json={"expected_version": public["version"]},
            )
            assert accept.status_code == 200, accept.text
            signed = await _get_application(client, client_token, app["id"])
            sign = await client.post(
                f"/api/v1/applications/{app['id']}/sign", headers=app_headers,
                json={"expected_version": signed["version"]},
            )
            assert sign.status_code == 202, sign.text
            worker = Worker(env.database, env.settings)
            assert await worker.run_once()
            completed = await _get_application(client, client_token, app["id"])
            assert completed["status"] == "Disbursed"

    _run(scenario())


@pytest.mark.integration
def test_postgres_auth_reuse_company_isolation_and_stale_writes(
    integration_environment: IntegrationEnvironment,
) -> None:
    async def scenario() -> None:
        env = integration_environment
        owner_login, owner_password = await _new_user(env.database, "client")
        stranger_login, stranger_password = await _new_user(env.database, "client")
        async with _client(env) as client:
            owner = await _access(client, owner_login, owner_password)
            stranger = await _access(client, stranger_login, stranger_password)
            company = await _create_company(client, owner)
            company_response = await client.get(
                f"/api/v1/companies/{company['id']}", headers={"Authorization": f"Bearer {stranger}"}
            )
            assert company_response.status_code == 404

            product = await _create_product(env.database)
            created = await client.post("/api/v1/applications", headers={"Authorization": f"Bearer {owner}"}, json={
                "company_id": company["id"], "data": {"product_id": product["id"]},
            })
            assert created.status_code == 201, created.text
            app = created.json()
            private = await client.get(f"/api/v1/applications/{app['id']}", headers={"Authorization": f"Bearer {stranger}"})
            assert private.status_code == 404

            changed = await client.patch(
                f"/api/v1/applications/{app['id']}", headers={"Authorization": f"Bearer {owner}"},
                json={"expected_version": app["version"], "data": {"amount": "100000.00"}},
            )
            assert changed.status_code == 200, changed.text
            stale = await client.patch(
                f"/api/v1/applications/{app['id']}", headers={"Authorization": f"Bearer {owner}"},
                json={"expected_version": app["version"], "data": {"purpose": "устаревшая команда"}},
            )
            assert stale.status_code == 409

            original = await client.post("/api/v1/auth/login", json={"login": owner_login, "password": owner_password})
            assert original.status_code == 200
            pair = original.json()
            rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
            assert rotated.status_code == 200
            reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
            assert reuse.status_code == 401
            assert reuse.headers.get("cache-control") == "no-store"
            revoked = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {rotated.json()['access_token']}"})
            assert revoked.status_code == 401
            assert revoked.headers.get("cache-control") == "no-store"

    _run(scenario())


@pytest.mark.integration
def test_postgres_unique_inn_conflict_rolls_back_and_concurrent_refresh_revokes_reused_session(
    integration_environment: IntegrationEnvironment,
) -> None:
    async def scenario() -> None:
        env = integration_environment
        client_login, client_password = await _new_user(env.database, "client")
        async with _client(env) as client:
            token = await _access(client, client_login, client_password)
            inn = f"{uuid.uuid4().int % 10**10:010d}"
            company = {
                "id": str(uuid.uuid4()), "version": 1, "legal_type": "legal_entity", "inn": inn,
                "name": "Company for unique constraint check", "registration_date": "2020-01-15",
                "okved": "47.91", "address": "Test address", "created_at": now(), "updated_at": now(),
            }
            async with env.database.transaction() as store:
                await store.insert("companies", company)
            duplicate = {**company, "id": str(uuid.uuid4()), "name": "Duplicate INN"}
            with pytest.raises(Exception) as conflict:
                async with env.database.transaction() as store:
                    await store.insert("companies", duplicate)
            assert getattr(conflict.value, "status", getattr(conflict.value, "status_code", None)) == 409
            async with env.database.transaction() as store:
                assert await store.get("companies", duplicate["id"]) is None

            initial = await client.post(
                "/api/v1/auth/login", json={"login": client_login, "password": client_password}
            )
            refresh_token = initial.json()["refresh_token"]
            responses = await asyncio.gather(
                client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
                client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
            )
            assert sorted(response.status_code for response in responses) == [200, 401]
            success = next(response for response in responses if response.status_code == 200)
            denial = next(response for response in responses if response.status_code == 401)
            assert denial.headers.get("cache-control") == "no-store"
            access = await client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {success.json()['access_token']}"}
            )
            assert access.status_code == 401

    _run(scenario())


@pytest.mark.integration
def test_postgres_skip_locked_claims_each_job_once(integration_environment: IntegrationEnvironment) -> None:
    async def scenario() -> None:
        env = integration_environment
        product = await _create_product(env.database)
        company_id, app_id, snap_id, job_id = (str(uuid.uuid4()) for _ in range(4))
        timestamp = now()
        company = {
            "id": company_id, "version": 1, "legal_type": "legal_entity",
            "inn": f"{uuid.uuid4().int % 10**10:010d}", "name": "Synthetic concurrent worker company",
            "registration_date": "2020-01-15", "okved": "47.91", "address": "Integration test",
            "created_at": timestamp, "updated_at": timestamp,
        }
        application = {
            "id": app_id, "company_id": company_id, "status": "Submitted", "version": 1,
            "data": {"product_id": product["id"]}, "current_snapshot_id": None,
            "created_at": timestamp, "updated_at": timestamp,
        }
        snapshot = {
            "id": snap_id, "application_id": app_id, "revision": 1,
            "company": company, "data": application["data"], "product": product,
            "document_ids": [], "consents": {"credit_history": True, "data_processing": True},
            "created_at": timestamp,
        }
        job = {
            "id": job_id, "application_id": app_id, "snapshot_id": snap_id,
            "stage": "checks", "source": "fns", "status": "queued", "attempt": 0,
            "cycle_attempt": 0, "run_at": timestamp, "request_id": str(uuid.uuid4()),
            "created_at": timestamp,
        }
        async with env.database.transaction() as store:
            await store.insert("companies", company)
            await store.insert("applications", application)
            await store.insert("application_snapshots", snapshot)
            application["current_snapshot_id"] = snap_id
            await store.save("applications", application)
            await store.insert("jobs", job)

        async def take():
            async with env.database.transaction() as store:
                return await store.claim_job(5)

        winners = await asyncio.gather(take(), take())
        claimed = [result for result in winners if result is not None]
        assert len(claimed) == 1
        assert claimed[0]["id"] == job_id
        assert claimed[0]["status"] == "running"

    _run(scenario())
