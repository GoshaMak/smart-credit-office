"""Focused auth checks for credential, rotation, and session revocation paths."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest

from credit.auth import AuthService, hash_password


class MemoryStore:
    def __init__(self):
        self.tables = {name: [] for name in ("users", "auth_sessions", "refresh_tokens", "company_members", "audit_events")}

    async def get(self, table, item_id, lock=False):
        return next((item for item in self.tables[table] if item["id"] == item_id), None)

    async def list(self, table, **filters):
        return [item for item in self.tables[table] if all(item.get(key) == value for key, value in filters.items())]

    async def insert(self, table, item):
        self.tables[table].append(dict(item))
        return item

    async def save(self, table, item):
        for idx, existing in enumerate(self.tables[table]):
            if existing["id"] == item["id"]:
                self.tables[table][idx] = dict(item)
                return item
        raise AssertionError("cannot save missing row")

    async def delete(self, table, item_id):
        self.tables[table] = [item for item in self.tables[table] if item["id"] != item_id]


class MemoryDB:
    def __init__(self):
        self.store = MemoryStore()

    @asynccontextmanager
    async def transaction(self):
        yield self.store


def service_fixture():
    db = MemoryDB()
    secret = secrets.token_bytes(32)
    settings = SimpleNamespace(
        jwt_secret=secret,
        jwt_issuer="credit-backend",
        jwt_audience="credit-api",
    )
    user_id = "9ff20798-6fdb-4c18-ae4b-833a8fbe0f99"
    db.store.tables["users"].append({
        "id": user_id,
        "login": "demo",
        "password_hash": hash_password("correct horse battery staple"),
        "display_name": "Demo Client",
        "role": "client",
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return AuthService(db, settings), db, settings, user_id


def test_scrypt_password_hash_is_salted_and_not_plaintext():
    first = hash_password("password")
    second = hash_password("password")
    assert first.startswith("scrypt$")
    assert first != second
    assert "password" not in first


def test_login_and_authenticate_reads_current_active_user():
    async def scenario():
        service, db, settings, user_id = service_fixture()
        tokens = await service.login({"login": "demo", "password": "correct horse battery staple"})
        claims = jwt.decode(tokens["access_token"], settings.jwt_secret, algorithms=["HS256"], audience="credit-api", issuer="credit-backend")
        assert claims["sub"] == user_id
        assert claims["sid"] in {row["id"] for row in db.store.tables["auth_sessions"]}
        actor = await service.authenticate(tokens["access_token"])
        assert actor["id"] == user_id and actor["role"] == "client"
        assert len(tokens["refresh_token"]) >= 40

    asyncio.run(scenario())


def test_refresh_rotation_and_reuse_revoke_session_before_401():
    async def scenario():
        service, db, _, _ = service_fixture()
        original = await service.login({"login": "demo", "password": "correct horse battery staple"})
        rotated = await service.refresh({"refresh_token": original["refresh_token"]})
        session = db.store.tables["auth_sessions"][0]
        assert session["revoked"] is False
        old_hash, new_hash = (row["token_hash"] for row in db.store.tables["refresh_tokens"])
        assert old_hash != new_hash
        assert db.store.tables["refresh_tokens"][0]["used"] is True
        assert db.store.tables["refresh_tokens"][1]["used"] is False

        with pytest.raises(Exception) as error:
            await service.refresh({"refresh_token": original["refresh_token"]})
        assert getattr(error.value, "status", getattr(error.value, "status_code", None)) == 401
        assert session["revoked"] is True
        with pytest.raises(Exception) as auth_error:
            await service.authenticate(rotated["access_token"])
        assert getattr(auth_error.value, "status", getattr(auth_error.value, "status_code", None)) == 401

    asyncio.run(scenario())


def test_unknown_refresh_does_not_revoke_another_session():
    async def scenario():
        service, db, _, _ = service_fixture()
        first = await service.login({"login": "demo", "password": "correct horse battery staple"})
        second = await service.login({"login": "demo", "password": "correct horse battery staple"})
        with pytest.raises(Exception) as error:
            await service.refresh({"refresh_token": "unknown-token"})
        assert getattr(error.value, "status_code", None) == 401
        assert all(session["revoked"] is False for session in db.store.tables["auth_sessions"])
        assert (await service.authenticate(first["access_token"]))["role"] == "client"
        assert (await service.authenticate(second["access_token"]))["role"] == "client"

    asyncio.run(scenario())


def test_authenticated_token_rejects_nonstandard_lifetime():
    async def scenario():
        service, db, settings, user_id = service_fixture()
        tokens = await service.login({"login": "demo", "password": "correct horse battery staple"})
        claims = jwt.decode(tokens["access_token"], settings.jwt_secret, algorithms=["HS256"], audience="credit-api", issuer="credit-backend")
        claims["exp"] += 3600
        forged = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
        with pytest.raises(Exception) as error:
            await service.authenticate(forged)
        assert getattr(error.value, "status_code", None) == 401

    asyncio.run(scenario())
