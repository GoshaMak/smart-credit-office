"""Boundary checks for the published HTTP contract."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx


class FakeDatabase:
    def __init__(self):
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        except Exception:
            self.rolled_back = True
            raise

    async def check_ready(self):
        return True

    async def close(self):
        pass


def _client(monkeypatch, db):
    monkeypatch.setenv("JWT_SECRET", base64.b64encode(b"transport-test-secret" * 4).decode())
    from credit import applications, auth, companies, documents
    from credit.common import ApiError
    from credit.main import create_app

    class FakeAuth:
        def __init__(self, database, settings):
            pass

        async def authenticate(self, token):
            if token != "valid":
                raise ApiError(401, "Недействительный токен.")
            return {"id": "actor", "role": "client"}

        async def login(self, body):
            return {"access_token": "access", "refresh_token": "refresh"}

    class FakeCompany:
        def __init__(self, database):
            self.db = database

        async def execute(self, operation_id, *, actor, path, query, body):
            async with self.db.transaction():
                raise ApiError(409, "Конфликт версии.")

    class FakeOther:
        def __init__(self, *args):
            pass

        async def execute(self, operation_id, *, actor, path, query, body):
            raise AssertionError("unexpected service dispatch")

    monkeypatch.setattr(auth, "AuthService", FakeAuth)
    monkeypatch.setattr(companies, "CompanyService", FakeCompany)
    monkeypatch.setattr(applications, "ApplicationService", FakeOther)
    monkeypatch.setattr(documents, "DocumentService", FakeOther)
    settings = SimpleNamespace(database_url="unused")
    app = create_app(settings=settings, database=db, storage=object())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_health_and_openapi_contract(monkeypatch):
    async def scenario():
        async with _client(monkeypatch, FakeDatabase()) as client:
            response = await client.get("/api/v1/health/live")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            document = (await client.get("/openapi.json")).json()
            assert document["openapi"] == "3.1.0"
            assert len({operation["operationId"] for methods in document["paths"].values() for operation in methods.values()}) == 38

    asyncio.run(scenario())


def test_validation_error_uses_error_schema_and_auth_no_store(monkeypatch):
    async def scenario():
        async with _client(monkeypatch, FakeDatabase()) as client:
            response = await client.post("/api/v1/auth/login", json={"login": "demo"})
            assert response.status_code == 400
            assert response.headers["cache-control"] == "no-store"
            assert set(response.json()) == {"message", "details"}
            assert response.json()["details"]

    asyncio.run(scenario())


def test_service_failure_rolls_back_and_keeps_error_shape(monkeypatch):
    async def scenario():
        db = FakeDatabase()
        async with _client(monkeypatch, db) as client:
            response = await client.get("/api/v1/companies", headers={"Authorization": "Bearer valid"})
            assert response.status_code == 409
            assert response.json() == {"message": "Конфликт версии.", "details": []}
            assert db.rolled_back

    asyncio.run(scenario())
