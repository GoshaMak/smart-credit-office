"""Application document authorization, metadata, and S3 object lifecycle."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any
from uuid import uuid4

from credit.common import (
    ApiError,
    audit,
    check_version,
    client_application_view,
    event,
    new_id,
    now,
    require_application,
    touch,
)

LOG = logging.getLogger(__name__)
MAX_FILE_BYTES = 10 * 1024 * 1024
UPLOAD_GRACE = timedelta(minutes=10)
ALLOWED_SIGNATURES = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
)
_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class Download:
    data: bytes
    filename: str
    content_type: str


def sniff_file(data: bytes) -> tuple[str, str]:
    """Return the canonical MIME and extension based on the file signature."""
    if not data:
        raise ApiError(415, "Файл пуст или имеет неподдерживаемый формат.", [])
    for signature, content_type, extension in ALLOWED_SIGNATURES:
        if data.startswith(signature):
            return content_type, extension
    raise ApiError(415, "Поддерживаются только PDF, JPEG и PNG.", [])


def safe_filename(value: Any, extension: str) -> str:
    """Remove path and control characters; never use this value in an S3 key."""
    text = unicodedata.normalize("NFKC", str(value or "document"))
    text = text.replace("\\", "/").split("/")[-1]
    text = _UNSAFE_FILENAME.sub("", text).strip().strip(".")
    stem = PurePath(text).stem
    stem = re.sub(r"[^\w .()-]", "_", stem, flags=re.UNICODE).strip(" .")[:120]
    if not stem:
        stem = "document"
    return stem + extension


def _text_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _public(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": document["id"],
        "application_id": document["application_id"],
        "document_type": document["document_type"],
        "filename": document["filename"],
        "content_type": document["content_type"],
        "size_bytes": document["size_bytes"],
        "uploaded_at": _text_time(document["uploaded_at"]),
    }


def _unavailable(exc: Exception) -> ApiError:
    LOG.exception("Document object storage operation failed")
    return ApiError(503, "Хранилище документов временно недоступно.", [])


def _can_mutate(app: dict[str, Any]) -> None:
    if app.get("status") not in {"Draft", "NeedsInfo"}:
        raise ApiError(409, "В текущем состоянии нельзя изменять документы.", [])


def _check_upload_fields(body: dict[str, Any]) -> tuple[bytes, str, str]:
    data = body.get("file")
    if not isinstance(data, bytes):
        raise ApiError(415, "Ожидается бинарный файл.", [])
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(413, "Файл превышает допустимый размер 10 MiB.", [])
    content_type, extension = sniff_file(data)
    doc_type = body.get("document_type")
    if not isinstance(doc_type, str) or not doc_type.strip() or len(doc_type) > 100:
        raise ApiError(400, "Некорректный тип документа.", [{"field": "/document_type", "message": "Ожидается непустая строка длиной до 100 символов."}])
    return data, content_type, extension


class DocumentService:
    def __init__(self, db: Any, storage: Any):
        self.db = db
        self.storage = storage

    async def list_documents(self, actor: dict[str, Any], application_id: str, query: dict[str, Any], *, bank: bool = False) -> dict[str, Any]:
        async with self.db.transaction() as store:
            await require_application(store, actor, application_id, bank=bank)
            records = await store.list("documents", application_id=application_id)
            records = [item for item in records if item.get("deleted_at") is None]
            records.sort(key=lambda item: (item.get("uploaded_at", ""), item["id"]), reverse=True)
            total = len(records)
            limit, offset = query.get("limit", 20), query.get("offset", 0)
            return {
                "items": [_public(item) for item in records[offset:offset + limit]],
                "limit": limit, "offset": offset, "total": total,
            }

    async def upload_document(self, actor: dict[str, Any], application_id: str, body: dict[str, Any]) -> dict[str, Any]:
        data, content_type, extension = _check_upload_fields(body)
        # Fail before external I/O for nonexistent/foreign objects and stale clients.
        async with self.db.transaction() as store:
            app = await require_application(store, actor, application_id, lock=True)
            check_version(app, body["expected_version"])
            _can_mutate(app)

        document_id = new_id()
        reservation_id = new_id()
        key = f"documents/{application_id}/{uuid4().hex}{extension}"
        reservation = {
            "id": reservation_id,
            "s3_key": key,
            "application_id": application_id,
            "document_id": document_id,
            "status": "pending",
            "created_at": now(),
        }
        # A durable reservation protects uploads even when cleanup runs while
        # an API process is paused between S3 put and database finalization.
        async with self.db.transaction() as store:
            app = await require_application(store, actor, application_id, lock=True)
            check_version(app, body["expected_version"])
            _can_mutate(app)
            await store.insert("upload_reservations", reservation)

        try:
            await self.storage.put(key, data, content_type)
        except Exception as exc:
            # put_object is idempotent for this unique key; a timed-out request
            # may have succeeded at S3, so compensate before releasing its key.
            try:
                await self.storage.delete(key)
            except Exception:
                LOG.exception("Failed to compensate incomplete S3 upload", extra={"document_id": document_id})
            else:
                await self._release_reservation(reservation_id)
            raise _unavailable(exc) from exc

        try:
            async with self.db.transaction() as store:
                app = await require_application(store, actor, application_id, lock=True)
                check_version(app, body["expected_version"])
                _can_mutate(app)
                current_reservation = await store.get("upload_reservations", reservation_id, lock=True)
                if (
                    not current_reservation
                    or current_reservation.get("status", "pending") != "pending"
                    or current_reservation.get("s3_key") != key
                ):
                    raise ApiError(409, "Срок операции загрузки истёк. Повторите загрузку.", [])
                uploaded_at = now()
                document = {
                    "id": document_id,
                    "application_id": application_id,
                    "document_type": body["document_type"].strip(),
                    "filename": safe_filename(body.get("filename"), extension),
                    "content_type": content_type,
                    "size_bytes": len(data),
                    "uploaded_at": uploaded_at,
                    "s3_key": key,
                    "deleted_at": None,
                }
                created = await store.insert("documents", document)
                await store.delete("upload_reservations", reservation_id)
                await touch(store, app)
                await event(store, app, actor, "document_uploaded", "Документ загружен.")
                await audit(store, actor, "document_uploaded", "document", document_id)
                return {"document": _public(created), "application_version": app["version"]}
        except Exception:
            # If compensation fails, the reservation remains until cleanup
            # safely expires it; late finalization then returns 409.
            try:
                await self.storage.delete(key)
            except Exception:
                LOG.exception("Failed to compensate orphaned S3 upload", extra={"document_id": document_id})
            else:
                await self._release_reservation(reservation_id)
            raise

    async def delete_document(self, actor: dict[str, Any], application_id: str, document_id: str, expected_version: int) -> dict[str, Any]:
        async with self.db.transaction() as store:
            app = await require_application(store, actor, application_id, lock=True)
            check_version(app, expected_version)
            _can_mutate(app)
            document = await store.get("documents", document_id, lock=True)
            if not document or document.get("application_id") != application_id or document.get("deleted_at") is not None:
                raise ApiError(404, "Документ не найден.", [])
            snapshots = await store.list("application_snapshots", application_id=application_id)
            if any(document_id in snapshot.get("document_ids", []) for snapshot in snapshots):
                raise ApiError(409, "Документ уже включён в снимок и не может быть удалён.", [])
            # Tombstone is committed before deleting from S3. Retaining the key
            # makes a transient S3 failure safely retryable by cleanup.
            document["deleted_at"] = now()
            await store.save("documents", document)
            await touch(store, app)
            await event(store, app, actor, "document_deleted", "Документ удалён.")
            await audit(store, actor, "document_deleted", "document", document_id)
            public_app = await self._application_response(store, app)

        try:
            await self.storage.delete(document["s3_key"])
        except Exception:
            # The logical deletion already committed. Keep the tombstone so a
            # scheduled cleanup can retry, while returning the committed state.
            LOG.exception("Physical document deletion deferred", extra={"document_id": document_id})
        else:
            async with self.db.transaction() as store:
                tombstone = await store.get("documents", document_id, lock=True)
                if tombstone and tombstone.get("deleted_at") is not None:
                    await store.delete("documents", document_id)
        return public_app

    async def _application_response(self, store: Any, app: dict[str, Any]) -> dict[str, Any]:
        return await client_application_view(store, app)

    async def _release_reservation(self, reservation_id: str) -> None:
        async with self.db.transaction() as store:
            reservation = await store.get("upload_reservations", reservation_id, lock=True)
            if reservation:
                await store.delete("upload_reservations", reservation_id)

    async def download_document(self, actor: dict[str, Any], application_id: str, document_id: str) -> Download:
        async with self.db.transaction() as store:
            await require_application(store, actor, application_id, bank=actor.get("role") == "bank_employee")
            document = await store.get("documents", document_id)
            if not document or document.get("application_id") != application_id or document.get("deleted_at") is not None:
                raise ApiError(404, "Документ не найден.", [])
            key = document["s3_key"]
            filename = document["filename"]
            content_type = document["content_type"]
        try:
            data = await self.storage.get(key)
        except Exception as exc:
            # Missing physical data is an operator-visible storage failure; do
            # not silently disguise it as an absent/unauthorized document.
            raise _unavailable(exc) from exc
        async with self.db.transaction() as store:
            await audit(store, actor, "document_downloaded", "document", document_id)
        return Download(data, filename, content_type)

    async def cleanup(self, *, grace: timedelta = UPLOAD_GRACE, at: datetime | None = None) -> dict[str, int]:
        """Retry soft-deleted objects and remove old S3 objects with no DB row.

        Intended for a scheduled worker command. Pending upload reservations
        protect active keys. After the grace interval, cleanup atomically marks
        a reservation abandoned before deleting its object, so a late API
        request cannot commit a document after cleanup. Historical snapshot
        documents remain protected by their metadata row.
        """
        cutoff = (at or datetime.now(timezone.utc)) - grace
        async with self.db.transaction() as store:
            all_documents = await store.list("documents")
        by_key = {item.get("s3_key"): item for item in all_documents if item.get("s3_key")}
        async with self.db.transaction() as store:
            reservations = await store.list("upload_reservations")
        deleted_count = 0
        orphan_count = 0
        for key, document in by_key.items():
            if document.get("deleted_at") is None:
                continue
            try:
                await self.storage.delete(key)
            except Exception:
                LOG.exception("Retrying document deletion failed", extra={"document_id": document["id"]})
                continue
            async with self.db.transaction() as store:
                current = await store.get("documents", document["id"], lock=True)
                if current and current.get("deleted_at") is not None and current.get("s3_key") == key:
                    await store.delete("documents", current["id"])
                    deleted_count += 1

        reserved_keys: set[str] = set()
        for reservation in reservations:
            key = reservation.get("s3_key")
            created_at = reservation.get("created_at")
            if not key:
                continue
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if not isinstance(created_at, datetime):
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at >= cutoff:
                reserved_keys.add(key)
                continue
            # Lock and mark the reservation first. The uploader takes the
            # application lock then this reservation lock before committing;
            # cleanup never takes an application lock, avoiding a lock cycle.
            async with self.db.transaction() as store:
                current = await store.get("upload_reservations", reservation["id"], lock=True)
                if not current or current.get("s3_key") != key:
                    continue
                if current.get("status", "pending") != "abandoned":
                    current["status"] = "abandoned"
                    await store.save("upload_reservations", current)
            try:
                await self.storage.delete(key)
            except Exception:
                LOG.exception("Abandoned upload cleanup deferred", extra={"reservation_id": reservation["id"]})
                reserved_keys.add(key)
                continue
            async with self.db.transaction() as store:
                current = await store.get("upload_reservations", reservation["id"], lock=True)
                if current and current.get("s3_key") == key and current.get("status") == "abandoned":
                    await store.delete("upload_reservations", current["id"])

        try:
            objects = await self.storage.list_objects()
        except Exception as exc:
            raise _unavailable(exc) from exc
        for item in objects:
            key, modified = item["key"], item.get("last_modified")
            if not key.startswith("documents/") or key in by_key or key in reserved_keys or not isinstance(modified, datetime):
                continue
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            if modified >= cutoff:
                continue
            # The first DB reads above are intentionally batched, but they are
            # stale snapshots by the time the S3 listing finishes. An upload
            # may have completed and removed its reservation in between. Check
            # the key again immediately before deleting it from S3; the upload
            # protocol always records a reservation before put_object and
            # atomically replaces it with metadata on completion.
            async with self.db.transaction() as store:
                live_documents = await store.list("documents", s3_key=key)
                live_reservations = await store.list("upload_reservations", s3_key=key)
            if live_documents or live_reservations:
                continue
            try:
                await self.storage.delete(key)
                orphan_count += 1
            except Exception:
                LOG.exception("Orphan document cleanup failed", extra={"object_key": key})
        return {"deleted_documents": deleted_count, "orphan_objects": orphan_count}

    async def execute(self, operation_id: str, *, actor: dict[str, Any], path: dict[str, Any], query: dict[str, Any], body: dict[str, Any] | None) -> Any:
        application_id = path["application_id"]
        if operation_id == "listDocuments":
            return await self.list_documents(actor, application_id, query)
        if operation_id == "listBankDocuments":
            return await self.list_documents(actor, application_id, query, bank=True)
        if operation_id == "uploadDocument":
            return await self.upload_document(actor, application_id, body or {})
        if operation_id == "deleteDocument":
            return await self.delete_document(actor, application_id, path["document_id"], query["expected_version"])
        if operation_id == "downloadDocument":
            return await self.download_document(actor, application_id, path["document_id"])
        raise ApiError(404, "Операция не найдена.", [])
