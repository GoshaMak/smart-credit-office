"""Private S3-compatible document storage.

The synchronous boto3 client is isolated in worker threads so file operations
never block FastAPI's event loop.  The SDK is imported lazily to keep module
imports usable in tooling that does not configure S3.
"""

from __future__ import annotations

import anyio
from botocore.config import Config
import threading
from typing import Any


MAX_OBJECT_BYTES = 10 * 1024 * 1024


class S3Storage:
    def __init__(self, settings: Any):
        self.endpoint_url = settings.s3_endpoint
        self.bucket = settings.s3_bucket
        self.region = settings.s3_region
        self.access_key = settings.s3_access_key
        self.secret_key = settings.s3_secret_key
        self._client: Any | None = None
        self._client_lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    import boto3

                    self._client = boto3.client(
                        "s3",
                        endpoint_url=self.endpoint_url or None,
                        region_name=self.region,
                        aws_access_key_id=self.access_key,
                        aws_secret_access_key=self.secret_key,
                        config=Config(
                            connect_timeout=3,
                            read_timeout=15,
                            retries={"max_attempts": 2, "mode": "standard"},
                            s3={"addressing_style": "path"},
                        ),
                    )
        return self._client

    def _close_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def close(self) -> None:
        await anyio.to_thread.run_sync(self._close_client)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await anyio.to_thread.run_sync(
            lambda: self._get_client().put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
            )
        )

    async def get(self, key: str) -> bytes:
        def fetch() -> bytes:
            response = self._get_client().get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = body.read(min(64 * 1024, MAX_OBJECT_BYTES + 1 - total))
                    if not chunk:
                        return b"".join(chunks)
                    total += len(chunk)
                    if total > MAX_OBJECT_BYTES:
                        raise ValueError("Stored document exceeds the configured file size limit")
                    chunks.append(chunk)
            finally:
                body.close()

        return await anyio.to_thread.run_sync(fetch)

    async def delete(self, key: str) -> None:
        await anyio.to_thread.run_sync(
            lambda: self._get_client().delete_object(Bucket=self.bucket, Key=key)
        )

    async def ensure_bucket(self) -> None:
        def ensure() -> None:
            client = self._get_client()
            try:
                client.head_bucket(Bucket=self.bucket)
                return
            except Exception as exc:
                response = getattr(exc, "response", {})
                status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                code = response.get("Error", {}).get("Code")
                if status != 404 and code not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
            kwargs = {"Bucket": self.bucket}
            if self.region and self.region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            client.create_bucket(**kwargs)

        await anyio.to_thread.run_sync(ensure)

    async def list_objects(self) -> list[dict[str, Any]]:
        def fetch() -> list[dict[str, Any]]:
            client = self._get_client()
            paginator = client.get_paginator("list_objects_v2")
            objects: list[dict[str, Any]] = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix="documents/"):
                for item in page.get("Contents", []):
                    objects.append(
                        {
                            "key": item["Key"],
                            "size": item.get("Size", 0),
                            "last_modified": item.get("LastModified"),
                        }
                    )
            return objects

        return await anyio.to_thread.run_sync(fetch)
