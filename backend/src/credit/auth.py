"""JWT authentication and rotating refresh sessions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from credit.common import ApiError, audit, new_id, now

ACCESS_LIFETIME = timedelta(minutes=2)
SESSION_LIFETIME = timedelta(days=2)
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LENGTH = 32


def hash_password(password: str) -> str:
    """Hash a seed/user password using a salted, parameterized scrypt record."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_SCRYPT_LENGTH,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, hash_text = encoded.split("$")
        if algorithm != "scrypt":
            return False
        if (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        def decode(value: str) -> bytes:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        expected = decode(hash_text)
        if len(expected) != _SCRYPT_LENGTH:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=decode(salt_text), n=int(n), r=int(r),
            p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError, MemoryError):
        return False


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _now_dt() -> datetime:
    return _utc(now())


def _time_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _refresh_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _refresh_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(self, db: Any, settings: Any):
        self.db = db
        self.settings = settings
        secret = settings.jwt_secret
        self.jwt_secret = secret if isinstance(secret, bytes) else base64.b64decode(secret, validate=True)

    def _issue_access(self, user: dict[str, Any], session_id: str) -> str:
        issued = int(_now_dt().timestamp())
        return jwt.encode(
            {
                "sub": user["id"], "sid": session_id, "jti": new_id(),
                "iss": self.settings.jwt_issuer, "aud": self.settings.jwt_audience,
                "iat": issued, "nbf": issued, "exp": issued + int(ACCESS_LIFETIME.total_seconds()),
            },
            self.jwt_secret,
            algorithm="HS256",
        )

    def _tokens(self, user: dict[str, Any], session_id: str, refresh: str) -> dict[str, str]:
        return {"access_token": self._issue_access(user, session_id), "refresh_token": refresh}

    async def login(self, body: dict[str, Any]) -> dict[str, str]:
        login = body.get("login", "")
        password = body.get("password", "")
        denied = False
        async with self.db.transaction() as store:
            users = await store.list("users", login=login)
            user = users[0] if users else None
            valid_password = bool(user and user.get("active")) and await asyncio.to_thread(
                _verify_password, password, user.get("password_hash", "")
            )
            if not valid_password:
                await audit(store, None, "login", "auth_session", "unknown", result="denied")
                denied = True
            else:
                timestamp = _now_dt()
                session_id = new_id()
                refresh = _refresh_secret()
                await store.insert("auth_sessions", {
                    "id": session_id, "user_id": user["id"],
                    "expires_at": _time_text(timestamp + SESSION_LIFETIME),
                    "revoked": False, "created_at": _time_text(timestamp),
                })
                await store.insert("refresh_tokens", {
                    "id": new_id(), "session_id": session_id, "token_hash": _refresh_hash(refresh),
                    "used": False, "created_at": _time_text(timestamp),
                })
                await audit(store, user, "login", "auth_session", session_id)
                response = self._tokens(user, session_id, refresh)
        if denied:
            raise ApiError(401, "Неверный логин или пароль.", [])
        return response

    async def refresh(self, body: dict[str, Any]) -> dict[str, str]:
        token = body.get("refresh_token", "")
        token_hash = _refresh_hash(token) if isinstance(token, str) else ""
        outcome = "invalid"
        response: dict[str, str] | None = None
        async with self.db.transaction() as store:
            matches = await store.list("refresh_tokens", token_hash=token_hash)
            record = matches[0] if matches else None
            session = await store.get("auth_sessions", record["session_id"], lock=True) if record else None
            # Serialize with logout/other refresh calls, then reread the rotating record.
            current = await store.list("refresh_tokens", token_hash=token_hash) if session else []
            record = current[0] if current else None
            if session and record and record.get("used"):
                if not session.get("revoked"):
                    session["revoked"] = True
                    await store.save("auth_sessions", session)
                await audit(store, None, "refresh_reuse", "auth_session", session["id"], result="denied")
                outcome = "reused"
            elif session and record and not session.get("revoked") and _utc(session["expires_at"]) > _now_dt():
                user = await store.get("users", session["user_id"])
                if user and user.get("active"):
                    record["used"] = True
                    await store.save("refresh_tokens", record)
                    refreshed = _refresh_secret()
                    await store.insert("refresh_tokens", {
                        "id": new_id(), "session_id": session["id"], "token_hash": _refresh_hash(refreshed),
                        "used": False, "created_at": now(),
                    })
                    await audit(store, user, "refresh", "auth_session", session["id"])
                    response = self._tokens(user, session["id"], refreshed)
                    outcome = "success"
                else:
                    session["revoked"] = True
                    await store.save("auth_sessions", session)
            if outcome == "invalid":
                await audit(store, None, "refresh", "auth_session", session["id"] if session else "unknown", result="denied")
        # The reuse revocation (or inactive-user revocation) has committed before 401.
        if outcome == "reused":
            raise ApiError(401, "Повторное использование refresh token.", [])
        if outcome == "invalid" or response is None:
            raise ApiError(401, "Refresh token недействителен или истёк.", [])
        return response

    async def logout(self, body: dict[str, Any]) -> None:
        token = body.get("refresh_token", "")
        token_hash = _refresh_hash(token) if isinstance(token, str) else ""
        async with self.db.transaction() as store:
            records = await store.list("refresh_tokens", token_hash=token_hash)
            if not records:
                await audit(store, None, "logout", "auth_session", "unknown", result="denied")
                return
            record = records[0]
            session = await store.get("auth_sessions", record["session_id"], lock=True)
            records = await store.list("refresh_tokens", token_hash=token_hash)
            record = records[0] if records else None
            if not session or not record:
                await audit(store, None, "logout", "auth_session", "unknown", result="denied")
                return
            if not session.get("revoked"):
                session["revoked"] = True
                await store.save("auth_sessions", session)
                users = await store.list("users", id=session["user_id"])
                await audit(store, users[0] if users else None, "logout", "auth_session", session["id"])

    async def authenticate(self, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token, self.jwt_secret, algorithms=["HS256"],
                issuer=self.settings.jwt_issuer, audience=self.settings.jwt_audience,
                options={"require": ["sub", "sid", "jti", "iss", "aud", "iat", "nbf", "exp"]},
            )
        except (jwt.PyJWTError, TypeError, ValueError):
            raise ApiError(401, "Токен доступа недействителен или истёк.", []) from None
        try:
            for key in ("sub", "sid", "jti"):
                if not isinstance(claims[key], str):
                    raise ValueError("Invalid UUID claim")
                UUID(claims[key])
            for key in ("iat", "nbf", "exp"):
                if type(claims[key]) is not int:
                    raise ValueError("Invalid NumericDate claim")
            if claims["nbf"] != claims["iat"] or claims["exp"] - claims["iat"] != int(ACCESS_LIFETIME.total_seconds()):
                raise ValueError("Invalid access token lifetime")
        except (KeyError, TypeError, ValueError):
            raise ApiError(401, "Токен доступа недействителен или истёк.", []) from None
        async with self.db.transaction() as store:
            session = await store.get("auth_sessions", claims["sid"])
            user = await store.get("users", claims["sub"])
            if (
                not session or session.get("revoked") or session["user_id"] != claims["sub"]
                or _utc(session["expires_at"]) <= _now_dt() or not user or not user.get("active")
            ):
                raise ApiError(401, "Токен доступа недействителен или истёк.", [])
            return user

    async def me(self, actor: dict[str, Any]) -> dict[str, Any]:
        async with self.db.transaction() as store:
            memberships = await store.list("company_members", user_id=actor["id"])
            return {
                "user": {key: actor[key] for key in ("id", "display_name", "role")},
                "memberships": [
                    {"company_id": item["company_id"], "role": item["role"]}
                    for item in memberships
                ],
            }
