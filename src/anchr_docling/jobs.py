import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from anchr_docling.config import Settings
from anchr_docling.errors import DoclingParseError, SourceDownloadError
from anchr_docling.schemas import (
    JobError,
    JobResponse,
    JobStatus,
    ParseRequest,
    ParseResponse,
)

logger = logging.getLogger(__name__)


class Parser(Protocol):
    def parse(self, request: ParseRequest) -> ParseResponse: ...


@dataclass
class JobRecord:
    job_id: str
    request: ParseRequest
    fingerprint: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: ParseResponse | None = None
    error: JobError | None = None

    def response(self) -> JobResponse:
        return JobResponse(
            jobId=self.job_id,
            requestId=self.request.request_id,
            status=self.status,
            createdAt=self.created_at,
            startedAt=self.started_at,
            finishedAt=self.finished_at,
            result=self.result,
            error=self.error,
        )


class QueueFullError(RuntimeError):
    pass


class RequestConflictError(RuntimeError):
    pass


class JobNotFoundError(RuntimeError):
    pass


class ActiveJobError(RuntimeError):
    pass


class JobManager:
    def __init__(self, parser: Parser, settings: Settings) -> None:
        self._parser = parser
        self._settings = settings
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_capacity)
        self._jobs: dict[str, JobRecord] = {}
        self._request_jobs: dict[str, str] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._cleanup_task: asyncio.Task[None] | None = None
        self._accepting = False

    async def start(self) -> None:
        self._accepting = True
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"docling-worker-{index}")
            for index in range(self._settings.workers)
        ]
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="docling-cleanup")

    async def stop(self) -> None:
        self._accepting = False
        tasks = [*self._workers]
        if self._cleanup_task is not None:
            tasks.append(self._cleanup_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._cleanup_task = None

    def submit(self, request: ParseRequest) -> tuple[JobRecord, bool]:
        fingerprint = self._fingerprint(request)
        existing_id = self._request_jobs.get(request.request_id)
        if existing_id is not None:
            existing = self._jobs.get(existing_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise RequestConflictError("requestId is already used by another payload")
                return existing, False
            self._request_jobs.pop(request.request_id, None)

        if not self._accepting or self._queue.full():
            raise QueueFullError("docling queue is full")

        record = JobRecord(
            job_id=str(uuid4()),
            request=request,
            fingerprint=fingerprint,
            status=JobStatus.QUEUED,
            created_at=self._now(),
        )
        self._jobs[record.job_id] = record
        self._request_jobs[request.request_id] = record.job_id
        self._queue.put_nowait(record.job_id)
        logger.info(
            "docling job queued jobId=%s requestId=%s queueDepth=%d",
            record.job_id,
            request.request_id,
            self.queued_count,
        )
        return record, True

    def get(self, job_id: str) -> JobRecord:
        record = self._jobs.get(job_id)
        if record is None:
            raise JobNotFoundError(job_id)
        return record

    def delete(self, job_id: str) -> bool:
        record = self._jobs.get(job_id)
        if record is None:
            return False
        if record.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            raise ActiveJobError(job_id)
        self._remove(record)
        return True

    @property
    def queued_count(self) -> int:
        return sum(record.status == JobStatus.QUEUED for record in self._jobs.values())

    @property
    def running_count(self) -> int:
        return sum(record.status == JobStatus.RUNNING for record in self._jobs.values())

    @property
    def capacity(self) -> int:
        return self._settings.queue_capacity

    async def _worker(self, worker_index: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                record = self._jobs.get(job_id)
                if record is None or record.status != JobStatus.QUEUED:
                    continue
                queue_seconds = (self._now() - record.created_at).total_seconds()
                if queue_seconds > self._settings.queue_wait_timeout_seconds:
                    self._fail(record, "QUEUE_TIMEOUT", "job exceeded the queue wait limit")
                    continue

                record.status = JobStatus.RUNNING
                record.started_at = self._now()
                logger.info(
                    "docling job started jobId=%s requestId=%s worker=%d queueSeconds=%.3f",
                    record.job_id,
                    record.request.request_id,
                    worker_index,
                    queue_seconds,
                )
                try:
                    record.result = await asyncio.to_thread(self._parser.parse, record.request)
                    record.status = JobStatus.SUCCEEDED
                    record.finished_at = self._now()
                    elapsed = (record.finished_at - record.started_at).total_seconds()
                    logger.info(
                        "docling job succeeded jobId=%s requestId=%s parseSeconds=%.3f",
                        record.job_id,
                        record.request.request_id,
                        elapsed,
                    )
                except SourceDownloadError as exc:
                    self._fail(record, "SOURCE_DOWNLOAD_ERROR", str(exc))
                except DoclingParseError as exc:
                    self._fail(record, "DOCLING_PARSE_ERROR", str(exc))
                except Exception:
                    logger.exception(
                        "docling job failed unexpectedly jobId=%s requestId=%s",
                        record.job_id,
                        record.request.request_id,
                    )
                    self._fail(record, "INTERNAL_ERROR", "document parsing failed")
                self._prune_terminal_jobs()
            finally:
                self._queue.task_done()

    def _fail(self, record: JobRecord, code: str, message: str) -> None:
        record.status = JobStatus.FAILED
        record.error = JobError(code=code, message=message[:300])
        record.finished_at = self._now()
        logger.warning(
            "docling job failed jobId=%s requestId=%s code=%s",
            record.job_id,
            record.request.request_id,
            code,
        )

    async def _cleanup_loop(self) -> None:
        interval = min(60.0, max(1.0, self._settings.result_ttl_seconds / 2))
        while True:
            await asyncio.sleep(interval)
            self._prune_terminal_jobs()

    def _prune_terminal_jobs(self) -> None:
        now = self._now()
        terminal = sorted(
            (
                record
                for record in self._jobs.values()
                if record.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)
                and record.finished_at is not None
            ),
            key=lambda record: record.finished_at or record.created_at,
        )
        expired = {
            record.job_id
            for record in terminal
            if (now - (record.finished_at or now)).total_seconds()
            > self._settings.result_ttl_seconds
        }
        retained = [record for record in terminal if record.job_id not in expired]
        overflow = max(0, len(retained) - self._settings.max_retained_results)
        expired.update(record.job_id for record in retained[:overflow])
        for job_id in expired:
            record = self._jobs.get(job_id)
            if record is not None:
                self._remove(record)

    def _remove(self, record: JobRecord) -> None:
        self._jobs.pop(record.job_id, None)
        if self._request_jobs.get(record.request.request_id) == record.job_id:
            self._request_jobs.pop(record.request.request_id, None)

    @staticmethod
    def _fingerprint(request: ParseRequest) -> str:
        if request.contract_version == 2:
            stable_output = None
            if request.oss is not None:
                stable_output = {
                    "endpoint": request.oss.endpoint,
                    "bucket": request.oss.bucket,
                    "basePath": request.oss.base_path,
                }
            payload = {
                "contractVersion": 2,
                "requestId": request.request_id,
                "sourceRevision": request.source_revision,
                "fileName": request.file_name,
                "options": request.options.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=False,
                ),
                "output": stable_output,
            }
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            # Keep the original full-payload fingerprint for legacy requests so a rolling
            # deployment cannot silently reinterpret an already accepted requestId.
            serialized = request.model_dump_json(by_alias=True, exclude_none=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
