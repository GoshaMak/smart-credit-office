"""HTTP boundary for the published 38-operation credit cabinet contract."""

from __future__ import annotations

import logging
import inspect
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from credit.validation import contract, operation_parameters, validate


LOG = logging.getLogger(__name__)
API_PREFIX = "/api/v1"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
AUTH_PUBLIC = {"login", "refreshTokens", "logout", "liveness", "readiness"}
COMPANY_OPERATIONS = {
    "listProducts", "getProduct", "listCompanies", "createCompany", "getCompany", "updateCompany"
}
DOCUMENT_OPERATIONS = {
    "listDocuments", "uploadDocument", "deleteDocument", "downloadDocument", "listBankDocuments"
}


def _error(status: int, message: str, details: list[dict[str, str]] | None = None) -> JSONResponse:
    return JSONResponse({"message": message, "details": details or []}, status_code=status)


async def _read_limited_multipart(request: Request) -> dict[str, Any]:
    from credit.common import ApiError

    length = request.headers.get("content-length")
    if length and length.isdecimal() and int(length) > MAX_MULTIPART_BYTES:
        raise ApiError(413, "Файл превышает допустимый размер.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_MULTIPART_BYTES:
            raise ApiError(413, "Файл превышает допустимый размер.")
        chunks.append(chunk)
    payload = b"".join(chunks)
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    clone = Request(request.scope, receive)
    try:
        async with clone.form(max_files=1, max_fields=2, max_part_size=MAX_MULTIPART_BYTES) as form:
            for field in ("expected_version", "document_type", "file"):
                if len(form.getlist(field)) != 1:
                    raise ApiError(400, "Неверные поля файла.", [{"field": "/" + field, "message": "Поле должно быть передано один раз."}])
            upload = form["file"]
            if not isinstance(upload, UploadFile):
                raise ApiError(400, "Неверное поле файла.", [{"field": "/file", "message": "Ожидается файл."}])
            file_content = await upload.read(MAX_UPLOAD_BYTES + 1)
            if len(file_content) > MAX_UPLOAD_BYTES:
                raise ApiError(413, "Файл превышает допустимый размер.")
            raw_version = form["expected_version"]
            if not isinstance(raw_version, str) or not raw_version.isdecimal():
                raise ApiError(400, "Некорректная версия.", [{"field": "/expected_version", "message": "Ожидается целое число."}])
            doc_type = form["document_type"]
            if not isinstance(doc_type, str):
                raise ApiError(400, "Некорректный тип документа.")
            return {
                "expected_version": int(raw_version),
                "document_type": doc_type,
                "file": file_content,
                "filename": upload.filename or "document",
                "content_type": upload.content_type or "application/octet-stream",
            }
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(400, "Некорректный multipart запрос.") from exc


async def _read_body(request: Request, operation: dict[str, Any]) -> Any:
    from credit.common import ApiError

    body_def = operation.get("requestBody")
    if body_def is None:
        return None
    content = body_def["content"]
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime not in content:
        status = 415 if "multipart/form-data" in content else 400
        raise ApiError(status, "Неподдерживаемый тип запроса.")
    if mime == "multipart/form-data":
        value = await _read_limited_multipart(request)
        schema_value = {k: value[k] for k in ("expected_version", "document_type")}
        schema_value["file"] = "binary"
    else:
        try:
            value = await request.json()
        except Exception as exc:
            raise ApiError(400, "Некорректный JSON.") from exc
        schema_value = value
    details = validate(content[mime]["schema"], schema_value)
    if details:
        raise ApiError(400, "Некорректные данные запроса.", details)
    return value


def _download_response(download: Any) -> Response:
    data = getattr(download, "data")
    filename = getattr(download, "filename")
    content_type = getattr(download, "content_type")
    allowed_types = {"application/pdf", "image/jpeg", "image/png"}
    if content_type not in allowed_types or not isinstance(data, bytes):
        raise ValueError("Document service returned invalid content")
    safe_name = quote(filename, safe="")
    return Response(
        data,
        media_type=content_type,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + safe_name,
            "Cache-Control": "no-store",
        },
    )


def create_app(settings: Any = None, database: Any = None, storage: Any = None) -> FastAPI:
    from credit.auth import AuthService
    from credit.companies import CompanyService
    from credit.applications import ApplicationService
    from credit.documents import DocumentService
    from credit.common import ApiError
    from credit.config import Settings
    from credit.database import Database
    from credit.storage import S3Storage

    settings = settings or Settings()
    database = database or Database(settings.database_url)
    storage = storage or S3Storage(settings)
    auth = AuthService(database, settings)
    companies = CompanyService(database)
    applications = ApplicationService(database, settings)
    documents = DocumentService(database, storage)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database = database
        app.state.storage = storage
        try:
            yield
        finally:
            try:
                close_storage = getattr(storage, "close", None)
                if close_storage is not None:
                    if inspect.iscoroutinefunction(close_storage):
                        await close_storage()
                    else:
                        import anyio

                        await anyio.to_thread.run_sync(close_storage)
            finally:
                await database.close()

    app = FastAPI(title="Кредитный кабинет", openapi_url="/openapi.json", docs_url="/docs", lifespan=lifespan)
    app.openapi = contract  # Serve the reviewed contract, rather than an inferred FastAPI variant.

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            LOG.exception("Unhandled HTTP error", extra={"request_id": request.state.request_id})
            response = _error(500, "Внутренняя ошибка сервера.")
        if request.url.path.startswith(API_PREFIX + "/auth/"):
            response.headers["Cache-Control"] = "no-store"
        LOG.info("HTTP request", extra={"request_id": request.state.request_id, "method": request.method, "path": request.url.path, "status": response.status_code})
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        status = getattr(exc, "status", getattr(exc, "status_code", 500))
        message = getattr(exc, "message", str(exc))
        details = getattr(exc, "details", [])
        if status in (403, 404) and getattr(request.state, "actor", None):
            # The service transaction has rolled back before this handler runs.
            # Keep a denial audit in a separate transaction without changing the response.
            try:
                from credit.common import audit

                object_id = next(
                    (str(request.path_params[name]) for name in ("application_id", "company_id", "document_id", "offer_id") if name in request.path_params),
                    "unknown",
                )
                async with database.transaction() as store:
                    await audit(store, request.state.actor, f"http.{request.method.lower()}.{getattr(request.state, 'operation_id', 'unknown')}", "request", object_id, result="denied", request_id=request.state.request_id)
            except Exception:
                LOG.exception("Failed to audit access denial", extra={"request_id": request.state.request_id})
        return _error(status, message, details)

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        message = "Маршрут не найден." if exc.status_code == 404 else "Метод не поддерживается." if exc.status_code == 405 else "Ошибка запроса."
        return _error(exc.status_code, message)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        LOG.exception("Unexpected API error", extra={"request_id": request.state.request_id})
        if isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__module__.startswith(("sqlalchemy.", "psycopg.", "botocore.")):
            return _error(503, "Сервис временно недоступен.")
        return _error(500, "Внутренняя ошибка сервера.")

    async def handle_operation(request: Request, operation: dict[str, Any], path_names: set[str]) -> Response:
        operation_id = operation["operationId"]
        request.state.operation_id = operation_id
        actor = None
        if operation_id not in AUTH_PUBLIC:
            authorization = request.headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise ApiError(401, "Требуется access token.")
            actor = await auth.authenticate(token.strip())
        request.state.actor = actor
        if len(request.query_params.multi_items()) != len(request.query_params):
            raise ApiError(400, "Повторяющийся параметр запроса.")
        raw = {**dict(request.query_params), **request.path_params}
        parsed, details = operation_parameters(operation, raw)
        if details:
            raise ApiError(400, "Некорректные параметры запроса.", details)
        path = {k: parsed[k] for k in path_names}
        query = {k: v for k, v in parsed.items() if k not in path_names}
        body = await _read_body(request, operation)
        if operation_id == "login":
            result = await auth.login(body)
        elif operation_id == "refreshTokens":
            result = await auth.refresh(body)
        elif operation_id == "logout":
            result = await auth.logout(body)
        elif operation_id == "getCurrentUser":
            result = await auth.me(actor)
        elif operation_id == "liveness":
            result = {"status": "ok"}
        elif operation_id == "readiness":
            try:
                ready = await database.check_ready()
            except Exception as exc:
                LOG.warning("Readiness check failed", exc_info=exc, extra={"request_id": request.state.request_id})
                ready = False
            if not ready:
                raise ApiError(503, "Сервис не готов.")
            result = {"status": "ok"}
        elif operation_id in COMPANY_OPERATIONS:
            result = await companies.execute(operation_id, actor=actor, path=path, query=query, body=body)
        elif operation_id in DOCUMENT_OPERATIONS:
            result = await documents.execute(operation_id, actor=actor, path=path, query=query, body=body)
        else:
            result = await applications.execute(operation_id, actor=actor, path=path, query=query, body=body)
        successful = [(int(code), definition) for code, definition in operation["responses"].items() if code.startswith("2")]
        if len(successful) != 1:
            raise RuntimeError(f"Ambiguous successful response for {operation_id}")
        status, definition = successful[0]
        if status == 204:
            return Response(status_code=204)
        if operation_id == "downloadDocument":
            response = _download_response(result)
            response.status_code = status
            return response
        content = definition.get("content", {})
        if "application/json" not in content:
            raise RuntimeError(f"Unsupported response media for {operation_id}")
        schema = content["application/json"]["schema"]
        issues = validate(schema, result)
        if issues:
            LOG.error("Service response violates contract", extra={"request_id": request.state.request_id, "operation_id": operation_id, "violation_count": len(issues)})
            raise RuntimeError(f"Invalid service result for {operation_id}")
        return JSONResponse(result, status_code=status)

    def make_endpoint(operation: dict[str, Any], path_names: set[str]):
        async def endpoint(request: Request) -> Response:
            return await handle_operation(request, operation, path_names)

        return endpoint

    for path_template, operations in contract()["paths"].items():
        path_names = {part[1:-1] for part in path_template.split("/") if part.startswith("{") and part.endswith("}")}
        for method, operation in operations.items():
            app.add_api_route(
                API_PREFIX + path_template,
                make_endpoint(operation, path_names),
                methods=[method.upper()],
                name=operation["operationId"],
                include_in_schema=False,
            )
    return app


# ASGI server entrypoint. Importing does not connect to PostgreSQL or S3.
app = create_app()
