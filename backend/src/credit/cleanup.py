"""Delete expired upload reservations and inaccessible/orphaned S3 objects.

Run periodically as a deployment job with the same database and S3 settings as
the API. It only deletes objects under ``documents/`` and older than the
reservation grace period in ``BACKEND_RULES.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from credit.config import Settings
from credit.database import Database
from credit.documents import DocumentService
from credit.storage import S3Storage


LOG = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings()
    database = Database(settings.database_url)
    storage = S3Storage(settings)
    try:
        grace = timedelta(seconds=float(os.getenv("DOCUMENT_CLEANUP_GRACE_SECONDS", "600")))
        if grace.total_seconds() <= 0:
            raise ValueError("DOCUMENT_CLEANUP_GRACE_SECONDS must be positive")
        result = await DocumentService(database, storage).cleanup(grace=grace)
        LOG.info("Document cleanup completed", extra=result)
    finally:
        await storage.close()
        await database.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
