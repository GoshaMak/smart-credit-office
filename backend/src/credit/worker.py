"""Lease-based background processing for checks, stub scoring, and simulated payout."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from credit.adapters import AdapterFailure, AdapterNeedsInfo, canonical_json, post_mock, validate_envelope
from credit.common import audit, enqueue, event, new_id, now, touch
from credit.config import Settings
from credit.database import Database

LOG = logging.getLogger(__name__)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _lease_is_valid(job: dict[str, Any], claim_id: str, at: datetime | None = None) -> bool:
    expiry = _parse_time(job.get("lease_until"))
    return job.get("status") == "running" and job.get("claim_id") == claim_id and expiry is not None and expiry > (at or datetime.now(timezone.utc))


class Worker:
    def __init__(self, database: Any, settings: Any):
        self.db = database
        self.settings = settings
        self._stopping = asyncio.Event()

    async def run_once(self) -> bool:
        """Claim and process at most one due job. Returns whether a job was claimed."""
        async with self.db.transaction() as store:
            job = await store.claim_job(self.settings.lease_seconds)
        if not job:
            return False
        claim_id = job.get("claim_id")
        if not claim_id:
            LOG.error("Claimed job has no claim id", extra={"job_id": job.get("id")})
            return True
        try:
            context = await self._start(job, claim_id)
            if context is None:
                return True
            if context.get("failure"):
                await self._finish_failure(job, claim_id, context["failure"])
                return True
            if job["source"] in {"fns", "bki", "scoring"}:
                await self._process_adapter(job, claim_id, context)
            elif job["source"] == "disbursement":
                await self._process_disbursement(job, claim_id)
            else:
                await self._finish_failure(job, claim_id, AdapterFailure("INVALID_JOB_SOURCE", False))
        except Exception:
            LOG.exception("Unexpected worker failure", extra={"job_id": job.get("id"), "application_id": job.get("application_id")})
            # A code defect or database failure must not be mislabeled as a successful result.
            # If the lease remains ours, retry within the configured bounded cycle.
            try:
                await self._finish_failure(job, claim_id, AdapterFailure("WORKER_ERROR", True))
            except Exception:
                LOG.exception("Could not record worker failure", extra={"job_id": job.get("id")})
        return True

    async def _start(self, job: dict[str, Any], claim_id: str) -> dict[str, Any] | None:
        async with self.db.transaction() as store:
            # All mutating worker paths lock application, then job. Claiming the
            # job itself has already committed in a short, independent transaction.
            app = await store.get("applications", job["application_id"], lock=True)
            current_job = await store.get("jobs", job["id"], lock=True)
            if not current_job or not _lease_is_valid(current_job, claim_id):
                return None
            if not app or app.get("current_snapshot_id") != job.get("snapshot_id"):
                self._done_job(current_job)
                await store.save("jobs", current_job)
                return None
            if not _job_allowed(app, job):
                self._done_job(current_job)
                await store.save("jobs", current_job)
                return None
            stamp = now()
            attempt = int(current_job.get("attempt", 1))
            app_status = app.get("status")
            if job["source"] == "fns" and app_status == "Submitted":
                app["status"] = "Checking"
            if job["source"] == "scoring":
                app["status"] = "Scoring"
                app["processing"] = {"stage": "scoring", "status": "running", "attempt": attempt, "retry_available": False}
            else:
                app.setdefault("processing", {}).update(stage=job["stage"], status="running", attempt=attempt, retry_available=False)
            app["processing"].pop("error_code", None)
            app["processing"].pop("next_retry_at", None)
            if job["source"] in {"fns", "bki"}:
                rows = await store.list("check_runs", application_id=app["id"], snapshot_id=job["snapshot_id"], source=job["source"])
                for prior in rows:
                    if prior.get("status") == "running" and int(prior.get("attempt", 0)) < attempt:
                        prior.update(status="failed", error_code="LEASE_EXPIRED", retryable=True, finished_at=stamp)
                        prior.pop("result", None)
                        await store.save("check_runs", prior)
                if not any(int(row.get("attempt", 0)) == attempt for row in rows):
                    await store.insert("check_runs", {
                        "id": new_id(), "application_id": app["id"],
                        "snapshot_id": job["snapshot_id"], "source": job["source"],
                        "request_id": current_job["request_id"], "status": "running",
                        "attempt": attempt, "started_at": stamp,
                    })
            await touch(store, app)
            await event(store, app, None, "processing_started", "Заявка передана на автоматическую обработку.")
            await audit(store, None, "worker.step.started", "application", app["id"], job_id=job["id"], snapshot_id=job["snapshot_id"])
            snapshots = await store.list("application_snapshots", application_id=app["id"])
            snapshot = next((item for item in snapshots if item.get("id") == job["snapshot_id"]), None)
            if not snapshot:
                raise RuntimeError("Job snapshot no longer exists")
            if job["source"] not in {"fns", "bki", "scoring"}:
                return {"app": app, "snapshot": snapshot, "attempt": attempt}
            payload = current_job.get("request_payload")
            if payload is None:
                payload = await self._build_request(store, current_job, snapshot)
                if payload is not None:
                    current_job["request_payload"] = payload
                    current_job["request_hash"] = hashlib.sha256(canonical_json(payload)).hexdigest()
                    await store.save("jobs", current_job)
                else:
                    return {"app": app, "snapshot": snapshot, "attempt": attempt, "failure": AdapterFailure("MISSING_CHECK_RESULTS", False)}
            elif not self._valid_stored_request(current_job, payload):
                return {"app": app, "snapshot": snapshot, "attempt": attempt, "failure": AdapterFailure("INVALID_REQUEST", False)}
            return {"app": app, "snapshot": snapshot, "attempt": attempt, "request_payload": payload}

    async def _process_adapter(self, job: dict[str, Any], claim_id: str, context: dict[str, Any]) -> None:
        request = context.get("request_payload")
        if request is None:
            await self._finish_failure(job, claim_id, AdapterFailure("MISSING_CHECK_RESULTS", False))
            return
        source = job["source"]
        endpoint = "/v1/scoring" if source == "scoring" else f"/v1/checks/{source}"
        try:
            raw = await post_mock(self.settings.mock_url, endpoint, request, self.settings.adapter_timeout)
            envelope = validate_envelope(raw, request, source)
            if envelope["status"] == "technical_error":
                await self._finish_failure(job, claim_id, AdapterFailure(envelope["error_code"], envelope["retryable"]))
            elif envelope["status"] == "needs_info":
                await self._finish_needs_info(job, claim_id, envelope)
            else:
                await self._finish_success(job, claim_id, request, envelope["result"])
        except AdapterFailure as failure:
            await self._finish_failure(job, claim_id, failure)
        except AdapterNeedsInfo as needs_info:
            await self._finish_needs_info(job, claim_id, needs_info.envelope)

    async def _build_request(self, store: Any, job: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
        document_types = []
        for document_id in snapshot.get("document_ids", []):
            document = await store.get("documents", document_id)
            if not document or document.get("application_id") != job["application_id"]:
                return None
            document_types.append(document["document_type"])
        product = dict(snapshot["product"])
        for field in ("required_document_types", "required_application_fields"):
            if isinstance(product.get(field), list):
                product[field] = sorted(set(product[field]))
        request: dict[str, Any] = {
            "request_id": job["request_id"], "snapshot_id": job["snapshot_id"], "schema_version": "1",
            "company": snapshot["company"], "data": snapshot["data"], "product": product,
            "document_types": sorted(set(document_types)),
        }
        if job["source"] == "scoring":
            runs = await store.list("check_runs", application_id=job["application_id"], snapshot_id=job["snapshot_id"])
            latest: dict[str, dict[str, Any]] = {}
            for run in runs:
                source = run.get("source")
                if source in {"fns", "bki"} and (
                    source not in latest or int(run.get("attempt", 0)) > int(latest[source].get("attempt", 0))
                ):
                    latest[source] = run
            if set(latest) != {"fns", "bki"} or any(latest[s].get("status") != "succeeded" for s in ("fns", "bki")):
                return None
            request["checks"] = {source: latest[source]["result"] for source in ("fns", "bki")}
        return request

    @staticmethod
    def _valid_stored_request(job: dict[str, Any], payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        required = {"request_id", "snapshot_id", "schema_version", "company", "data", "product", "document_types"}
        if job.get("source") == "scoring":
            required.add("checks")
        try:
            digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        except (TypeError, ValueError):
            return False
        return (
            set(payload) == required
            and payload.get("request_id") == job.get("request_id")
            and payload.get("snapshot_id") == job.get("snapshot_id")
            and payload.get("schema_version") == "1"
            and job.get("request_hash") == digest
        )

    async def _finish_success(self, job: dict[str, Any], claim_id: str, request: dict[str, Any], result: dict[str, Any]) -> None:
        async with self.db.transaction() as store:
            app, current = await self._owned_current(store, job, claim_id)
            if app is None:
                return
            stamp = now()
            if job["source"] in {"fns", "bki"}:
                rows = await store.list("check_runs", application_id=app["id"], snapshot_id=job["snapshot_id"], source=job["source"])
                run = _latest_attempt(rows)
                if not run or int(run.get("attempt", 0)) != int(current.get("attempt", 0)):
                    raise RuntimeError("Current check attempt was not recorded")
                run.update(status="succeeded", result=result, finished_at=stamp)
                run.pop("error_code", None)
                run.pop("retryable", None)
                await store.save("check_runs", run)
                if job["source"] == "fns":
                    await self._done(store, current)
                    await enqueue(store, app, "checks", "bki")
                    app["status"] = "Checking"
                else:
                    await self._done(store, current)
                    app["status"] = "Scoring"
                    await enqueue(store, app, "scoring", "scoring")
            elif job["source"] == "scoring":
                record = {"id": new_id(), "application_id": app["id"], "snapshot_id": job["snapshot_id"],
                          "request_id": job["request_id"], **result, "created_at": stamp}
                existing = await store.list("scoring_results", application_id=app["id"], snapshot_id=job["snapshot_id"])
                if not existing:
                    await store.insert("scoring_results", record)
                self._done_job(current)
                await store.save("jobs", current)
                app["status"] = "ManualReview"
                app["processing"] = {"stage": "decision", "status": "completed", "attempt": 0, "retry_available": False}
            await touch(store, app)
            await event(store, app, None, "processing_succeeded", "Автоматическая обработка заявки завершена.")
            await audit(store, None, "worker.step.succeeded", "application", app["id"], job_id=job["id"], snapshot_id=job["snapshot_id"])

    async def _finish_needs_info(self, job: dict[str, Any], claim_id: str, envelope: dict[str, Any]) -> None:
        async with self.db.transaction() as store:
            app, current = await self._owned_current(store, job, claim_id)
            if app is None:
                return
            if job["source"] in {"fns", "bki"}:
                rows = await store.list("check_runs", application_id=app["id"], snapshot_id=job["snapshot_id"], source=job["source"])
                run = _latest_attempt(rows)
                if not run or int(run.get("attempt", 0)) != int(current.get("attempt", 0)):
                    raise RuntimeError("Current check attempt was not recorded")
                run.update(status="needs_info", finished_at=now())
                for key in ("result", "error_code", "retryable"):
                    run.pop(key, None)
                await store.save("check_runs", run)
            existing = await store.list("information_requests", application_id=app["id"], status="open")
            if not existing:
                request = {"id": new_id(), "application_id": app["id"], "snapshot_id": job["snapshot_id"], "status": "open",
                           "message": envelope["message"], "requested_fields": sorted(set(envelope["requested_fields"])),
                           "required_document_types": sorted(set(envelope["required_document_types"])), "created_at": now()}
                await store.insert("information_requests", request)
            self._done_job(current)
            await store.save("jobs", current)
            app["status"] = "NeedsInfo"
            app.pop("processing", None)
            await touch(store, app)
            await event(store, app, None, "information_requested", "Для продолжения заявки нужны дополнительные сведения.")
            await audit(store, None, "worker.needs_info", "application", app["id"], job_id=job["id"], snapshot_id=job["snapshot_id"])

    async def _finish_failure(self, job: dict[str, Any], claim_id: str, failure: AdapterFailure) -> None:
        async with self.db.transaction() as store:
            app, current = await self._owned_current(store, job, claim_id)
            if app is None:
                return
            stamp = datetime.now(timezone.utc)
            cycle = int(current.get("cycle_attempt", 1))
            retry = failure.retryable and cycle < self.settings.max_attempts
            processing = app.setdefault("processing", {
                "stage": job.get("stage", "checks"),
                "status": "running",
                "attempt": int(current.get("attempt", 0)),
                "retry_available": False,
            })
            if retry:
                delay_index = min(max(cycle - 1, 0), len(self.settings.retry_delays) - 1)
                retry_at = stamp + timedelta(seconds=self.settings.retry_delays[delay_index])
                current.update(status="queued", run_at=retry_at.isoformat().replace("+00:00", "Z"), lease_until=None, claim_id=None)
                processing.update(status="retry_scheduled", attempt=int(current.get("attempt", cycle)), next_retry_at=current["run_at"], error_code=failure.code, retry_available=False)
            else:
                current.update(status="blocked", lease_until=None, claim_id=None, error_code=failure.code)
                can_manual_retry = bool(failure.retryable)
                processing.update(status="blocked", attempt=int(current.get("attempt", cycle)), error_code=failure.code, retry_available=can_manual_retry)
                processing.pop("next_retry_at", None)
            if job["source"] in {"fns", "bki"}:
                rows = await store.list("check_runs", application_id=app["id"], snapshot_id=job["snapshot_id"], source=job["source"])
                run = _latest_attempt(rows)
                if run and int(run.get("attempt", 0)) == int(current.get("attempt", 0)):
                    run.update(status="failed", error_code=failure.code, retryable=failure.retryable, finished_at=now())
                    run.pop("result", None)
                    await store.save("check_runs", run)
            await store.save("jobs", current)
            await touch(store, app)
            message = "Обработка заявки поставлена на повтор." if retry else "Техническая обработка приостановлена до восстановления сотрудником банка."
            await event(store, app, None, "processing_retry_scheduled" if retry else "processing_blocked", message)
            await audit(store, None, "worker.step.failed", "application", app["id"], job_id=job["id"], snapshot_id=job["snapshot_id"], error_code=failure.code, retryable=failure.retryable)

    async def _process_disbursement(self, job: dict[str, Any], claim_id: str) -> None:
        async with self.db.transaction() as store:
            app, current = await self._owned_current(store, job, claim_id)
            if app is None:
                return
            if app.get("status") != "Signed":
                self._done_job(current)
                await store.save("jobs", current)
                return
            # The app row lock makes this the unique result for the (application, snapshot, step).
            app["status"] = "Disbursed"
            app["processing"] = {"stage": "disbursement", "status": "completed", "attempt": 0, "retry_available": False}
            self._done_job(current)
            await store.save("jobs", current)
            await touch(store, app)
            await event(store, app, None, "disbursement_simulated", "Имитация выдачи завершена.")
            await audit(store, None, "worker.disbursement.succeeded", "application", app["id"], job_id=job["id"], snapshot_id=job["snapshot_id"])

    async def _owned_current(self, store: Any, job: dict[str, Any], claim_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        app = await store.get("applications", job["application_id"], lock=True)
        current = await store.get("jobs", job["id"], lock=True)
        if not current or not _lease_is_valid(current, claim_id):
            return None, None
        if not app or app.get("current_snapshot_id") != job.get("snapshot_id"):
            self._done_job(current)
            await store.save("jobs", current)
            return None, None
        if not _job_allowed(app, job):
            self._done_job(current)
            await store.save("jobs", current)
            return None, None
        return app, current

    @staticmethod
    def _done_job(job: dict[str, Any]) -> None:
        job.update(status="done", lease_until=None, claim_id=None)
        job.pop("error_code", None)
        job.pop("run_at", None)

    async def _done(self, store: Any, job: dict[str, Any]) -> None:
        self._done_job(job)
        await store.save("jobs", job)

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.settings.poll_interval)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopping.set()


def _job_allowed(app: dict[str, Any], job: dict[str, Any]) -> bool:
    if job.get("source") == "fns":
        return app.get("status") in {"Submitted", "Checking"}
    if job.get("source") == "bki":
        return app.get("status") == "Checking"
    if job.get("source") == "scoring":
        return app.get("status") in {"Checking", "Scoring"}
    if job.get("source") == "disbursement":
        return app.get("status") == "Signed"
    return False


def _latest_attempt(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the latest immutable check attempt without trusting storage order."""
    if not rows:
        return None
    return max(rows, key=lambda row: (int(row.get("attempt", 0)), row.get("id", "")))


async def main() -> None:
    import signal
    settings = Settings()
    database = Database(settings.database_url)
    worker = Worker(database, settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            pass
    try:
        await worker.run_forever()
    finally:
        await database.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
