from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from credit.common import ApiError
from credit.documents import DocumentService, safe_filename, sniff_file
from credit.storage import MAX_OBJECT_BYTES, S3Storage


def test_sniffing_uses_file_signature_and_safe_filename():
    assert sniff_file(b"%PDF-1.7\nbody") == ("application/pdf", ".pdf")
    assert sniff_file(b"\xff\xd8\xffbody") == ("image/jpeg", ".jpg")
    assert sniff_file(b"\x89PNG\r\n\x1a\nbody") == ("image/png", ".png")
    with pytest.raises(ApiError) as error:
        sniff_file(b"not a pdf")
    assert error.value.status == 415
    with pytest.raises(ApiError) as error:
        sniff_file(b"")
    assert error.value.status == 415
    assert safe_filename("../../evil\nname.exe", ".pdf") == "evilname.pdf"


class MemoryStore:
    def __init__(self):
        self.tables = {
            "applications": [{"id": "app-1", "company_id": "co-1", "status": "Draft", "version": 1}],
            "companies": [{"id": "co-1"}],
            "company_members": [{"id": "mem-1", "company_id": "co-1", "user_id": "user-1", "role": "owner"}],
        }

    async def get(self, table, object_id, lock=False):
        return next((x for x in self.tables.get(table, []) if x["id"] == str(object_id)), None)

    async def list(self, table, **filters):
        return [x for x in self.tables.get(table, []) if all(x.get(k) == v for k, v in filters.items())]

    async def insert(self, table, item):
        self.tables.setdefault(table, []).append(item.copy())
        return item.copy()

    async def save(self, table, item):
        rows = self.tables.setdefault(table, [])
        for i, row in enumerate(rows):
            if row["id"] == item["id"]:
                rows[i] = item.copy()
                return item.copy()
        rows.append(item.copy())
        return item.copy()

    async def delete(self, table, object_id):
        self.tables[table] = [x for x in self.tables.get(table, []) if x["id"] != str(object_id)]


class MemoryDb:
    def __init__(self, store):
        self.store = store

    @asynccontextmanager
    async def transaction(self):
        yield self.store


class MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.fail_put = False
        self.fail_get = False
        self.fail_delete = False

    async def put(self, key, data, content_type):
        if self.fail_put:
            raise OSError("unavailable")
        self.objects[key] = data

    async def get(self, key):
        if self.fail_get:
            raise OSError("unavailable")
        return self.objects[key]

    async def delete(self, key):
        if self.fail_delete:
            raise OSError("unavailable")
        self.objects.pop(key, None)

    async def list_objects(self):
        return []


CLIENT = {"id": "user-1", "role": "client"}
PDF = b"%PDF-1.7\nsynthetic"


def test_upload_download_and_delete_respects_snapshot_immutability():
    async def scenario():
        store, s3 = MemoryStore(), MemoryStorage()
        service = DocumentService(MemoryDb(store), s3)
        created = await service.upload_document(CLIENT, "app-1", {
            "expected_version": 1, "document_type": "financial_statement",
            "file": PDF, "filename": "../../report.pdf", "content_type": "text/plain",
        })
        assert created["application_version"] == 2
        document = created["document"]
        assert document["filename"] == "report.pdf"
        assert document["content_type"] == "application/pdf"
        internal = store.tables["documents"][0]
        assert "s3_key" not in document
        assert "sha256" not in document
        result = await service.download_document(CLIENT, "app-1", document["id"])
        assert result.data == PDF
        assert result.filename == "report.pdf"

        store.tables["application_snapshots"] = [{"id": "snap-1", "application_id": "app-1", "document_ids": [document["id"]]}]
        with pytest.raises(ApiError) as error:
            await service.delete_document(CLIENT, "app-1", document["id"], 2)
        assert error.value.status == 409
        assert internal["s3_key"] in s3.objects

    asyncio.run(scenario())


def test_upload_version_race_compensates_s3_object():
    async def scenario():
        store = MemoryStore()

        class RacingStorage(MemoryStorage):
            async def put(self, key, data, content_type):
                await super().put(key, data, content_type)
                store.tables["applications"][0]["version"] = 2

        s3 = RacingStorage()
        service = DocumentService(MemoryDb(store), s3)
        with pytest.raises(ApiError) as error:
            await service.upload_document(CLIENT, "app-1", {
                "expected_version": 1, "document_type": "tax", "file": PDF,
            })
        assert error.value.status == 409
        assert s3.objects == {}
        assert not store.tables.get("documents")

    asyncio.run(scenario())


def test_delete_commits_tombstone_and_cleanup_retries_s3_failure():
    async def scenario():
        store, s3 = MemoryStore(), MemoryStorage()
        service = DocumentService(MemoryDb(store), s3)
        created = await service.upload_document(CLIENT, "app-1", {
            "expected_version": 1, "document_type": "tax", "file": PDF, "filename": "tax.pdf",
        })
        key = store.tables["documents"][0]["s3_key"]
        s3.fail_delete = True
        result = await service.delete_document(CLIENT, "app-1", created["document"]["id"], 2)
        assert result["version"] == 3
        assert store.tables["documents"][0]["deleted_at"] is not None
        assert key in s3.objects
        assert await service.list_documents(CLIENT, "app-1", {}) == {
            "items": [], "limit": 20, "offset": 0, "total": 0,
        }

        s3.fail_delete = False
        await service.cleanup()
        assert not store.tables["documents"]
        assert key not in s3.objects

    asyncio.run(scenario())


def test_foreign_application_hidden_and_s3_failures_are_503():
    async def scenario():
        store, s3 = MemoryStore(), MemoryStorage()
        service = DocumentService(MemoryDb(store), s3)
        with pytest.raises(ApiError) as error:
            await service.list_documents(CLIENT, "missing", {})
        assert error.value.status == 404

        store.tables["documents"] = [{
            "id": "doc-1", "application_id": "app-1", "document_type": "tax",
            "filename": "tax.pdf", "content_type": "application/pdf", "size_bytes": len(PDF),
            "uploaded_at": "2026-10-25T00:00:00Z", "s3_key": "documents/app-1/key.pdf", "deleted_at": None,
        }]
        s3.fail_get = True
        with pytest.raises(ApiError) as error:
            await service.download_document(CLIENT, "app-1", "doc-1")
        assert error.value.status == 503

    asyncio.run(scenario())


def test_cleanup_keeps_recent_orphan_and_removes_only_old_orphans():
    async def scenario():
        store, s3 = MemoryStore(), MemoryStorage()
        old_key, new_key, outside_key = "documents/orphan-old", "documents/orphan-new", "other-service/object"
        s3.objects.update({old_key: b"x", new_key: b"y", outside_key: b"z"})
        old = datetime(2026, 10, 24, tzinfo=timezone.utc)
        recent = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)

        async def list_objects():
            return [
                {"key": old_key, "last_modified": old},
                {"key": new_key, "last_modified": recent},
                {"key": outside_key, "last_modified": old},
            ]

        s3.list_objects = list_objects
        result = await DocumentService(MemoryDb(store), s3).cleanup(
            at=datetime(2026, 10, 25, 1, tzinfo=timezone.utc),
            grace=timedelta(minutes=10),
        )
        assert result["orphan_objects"] == 1
        assert old_key not in s3.objects
        assert new_key in s3.objects
        assert outside_key in s3.objects

    asyncio.run(scenario())


def test_cleanup_cannot_delete_orphan_outside_upload_scope_or_active_reservation():
    async def scenario():
        store, s3 = MemoryStore(), MemoryStorage()
        key, outside = "documents/app-1/active.pdf", "reports/old-file.pdf"
        created_at = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)
        store.tables["upload_reservations"] = [{
            "id": "reservation-1", "s3_key": key, "application_id": "app-1",
            "status": "pending", "created_at": created_at.isoformat(),
        }]
        s3.objects.update({key: PDF, outside: PDF})
        s3.list_objects = lambda: asyncio.sleep(0, result=[
            {"key": key, "last_modified": created_at},
            {"key": outside, "last_modified": created_at - timedelta(hours=1)},
        ])
        result = await DocumentService(MemoryDb(store), s3).cleanup(
            at=created_at, grace=timedelta(minutes=10),
        )
        assert result["orphan_objects"] == 0
        assert key in s3.objects
        assert outside in s3.objects
        assert store.tables["upload_reservations"][0]["status"] == "pending"

    asyncio.run(scenario())


def test_cleanup_rechecks_db_after_object_listing_before_deleting():
    async def scenario():
        store, s3 = MemoryStore(), MemoryStorage()
        key = "documents/app-1/upload-committed-during-cleanup.pdf"
        old = datetime(2026, 10, 24, tzinfo=timezone.utc)
        s3.objects[key] = PDF

        async def list_objects():
            # Simulate upload finalization between the cleanup's initial DB
            # snapshot and its S3 inventory response.
            store.tables["documents"] = [{
                "id": "doc-committed", "application_id": "app-1",
                "document_type": "financial_statement", "filename": "statement.pdf",
                "content_type": "application/pdf", "size_bytes": len(PDF),
                "uploaded_at": datetime.now(timezone.utc).isoformat(), "s3_key": key,
                "deleted_at": None,
            }]
            return [{"key": key, "last_modified": old}]

        s3.list_objects = list_objects
        result = await DocumentService(MemoryDb(store), s3).cleanup(
            at=datetime(2026, 10, 25, 1, tzinfo=timezone.utc),
            grace=timedelta(minutes=10),
        )
        assert result["orphan_objects"] == 0
        assert key in s3.objects
        assert store.tables["documents"][0]["deleted_at"] is None

    asyncio.run(scenario())


def test_cleanup_expiring_upload_reservation_prevents_late_metadata_commit():
    async def scenario():
        store = MemoryStore()
        entered_put = asyncio.Event()
        release_put = asyncio.Event()

        class DelayedStorage(MemoryStorage):
            async def put(self, key, data, content_type):
                self.objects[key] = data
                self.keys = [key]
                entered_put.set()
                await release_put.wait()

            async def list_objects(self):
                return [{"key": key, "last_modified": datetime(2026, 10, 25, tzinfo=timezone.utc)} for key in self.keys]

        s3 = DelayedStorage()
        service = DocumentService(MemoryDb(store), s3)
        upload = asyncio.create_task(service.upload_document(CLIENT, "app-1", {
            "expected_version": 1, "document_type": "tax", "file": PDF,
        }))
        await entered_put.wait()
        reservation = store.tables["upload_reservations"][0]
        original_time = datetime.fromisoformat(reservation["created_at"].replace("Z", "+00:00"))
        await service.cleanup(at=original_time + timedelta(minutes=11))
        assert not store.tables["upload_reservations"]
        assert not s3.objects

        release_put.set()
        with pytest.raises(ApiError) as error:
            await upload
        assert error.value.status == 409
        assert not store.tables.get("documents")
        assert not s3.objects

    asyncio.run(scenario())


def test_bank_cannot_list_or_download_draft_documents():
    async def scenario():
        store, s3 = MemoryStore(), MemoryStorage()
        store.tables["applications"][0]["status"] = "Draft"
        service = DocumentService(MemoryDb(store), s3)
        bank = {"id": "bank-1", "role": "bank_employee"}
        for operation in (
            service.list_documents(bank, "app-1", {}, bank=True),
            service.download_document(bank, "app-1", "document-1"),
        ):
            with pytest.raises(ApiError) as error:
                await operation
            assert error.value.status == 404

    asyncio.run(scenario())


def test_storage_stream_limit_and_async_client_close():
    class FakeBody:
        def __init__(self, content):
            self.content = content
            self.offset = 0
            self.closed = False

        def read(self, size):
            result = self.content[self.offset:self.offset + size]
            self.offset += len(result)
            return result

        def close(self):
            self.closed = True

    class FakeClient:
        def __init__(self, content):
            self.body = FakeBody(content)
            self.closed = False

        def get_object(self, **kwargs):
            return {"Body": self.body}

        def close(self):
            self.closed = True

    async def scenario():
        oversized = FakeClient(b"x" * (MAX_OBJECT_BYTES + 1))
        storage = S3Storage(type("Settings", (), {
            "s3_endpoint": "http://localstack", "s3_bucket": "documents", "s3_region": "us-east-1",
            "s3_access_key": "test", "s3_secret_key": "test",
        })())
        storage._client = oversized
        with pytest.raises(ValueError, match="size limit"):
            await storage.get("documents/too-large")
        assert oversized.body.closed

        await storage.close()
        assert oversized.closed
        assert storage._client is None

    asyncio.run(scenario())
