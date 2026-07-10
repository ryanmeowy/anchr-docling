import asyncio
import threading
import unittest

from pydantic import SecretStr

from anchr_docling.config import Settings
from anchr_docling.errors import DoclingParseError
from anchr_docling.jobs import (
    ActiveJobError,
    JobManager,
    JobNotFoundError,
    QueueFullError,
    RequestConflictError,
)
from anchr_docling.schemas import JobStatus, ParseRequest, ParseResponse

TOKEN = "t" * 64


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ANCHR_DOCLING_API_TOKEN": SecretStr(TOKEN),
        "ANCHR_DOCLING_ALLOWED_DOWNLOAD_HOSTS": "anchr.oss-cn-shanghai.aliyuncs.com",
        "ANCHR_DOCLING_PRELOAD_MODELS": False,
        "ANCHR_DOCLING_WORKERS": 1,
        "ANCHR_DOCLING_QUEUE_CAPACITY": 8,
        "ANCHR_DOCLING_RESULT_TTL_SECONDS": 600,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_request(request_id: str, file_name: str = "sample.pdf") -> ParseRequest:
    return ParseRequest(
        requestId=request_id,
        sourceUrl="https://anchr.oss-cn-shanghai.aliyuncs.com/anchr-dev/sample.pdf",
        fileName=file_name,
    )


def make_response(request: ParseRequest) -> ParseResponse:
    return ParseResponse(
        requestId=request.request_id,
        parser="docling",
        format="markdown",
        fileType="pdf",
        text="parsed",
    )


class ImmediateParser:
    def parse(self, request: ParseRequest) -> ParseResponse:
        return make_response(request)


class FailingParser:
    def parse(self, request: ParseRequest) -> ParseResponse:
        raise DoclingParseError("bad document")


class BlockingParser:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.max_active = 0
        self.order: list[str] = []
        self.lock = threading.Lock()

    def parse(self, request: ParseRequest) -> ParseResponse:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.order.append(request.request_id)
        self.started.set()
        self.release.wait(timeout=5)
        with self.lock:
            self.active -= 1
        return make_response(request)


async def wait_for_status(manager: JobManager, job_id: str, status: JobStatus) -> None:
    for _ in range(200):
        if manager.get(job_id).status == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {status}")


class JobManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_idempotency_conflict_and_delete(self) -> None:
        manager = JobManager(ImmediateParser(), make_settings())
        await manager.start()
        try:
            request = make_request("request-1")
            record, created = manager.submit(request)
            self.assertTrue(created)
            duplicate, duplicate_created = manager.submit(request)
            self.assertFalse(duplicate_created)
            self.assertEqual(record.job_id, duplicate.job_id)

            with self.assertRaises(RequestConflictError):
                manager.submit(make_request("request-1", "other.pdf"))

            await wait_for_status(manager, record.job_id, JobStatus.SUCCEEDED)
            self.assertEqual("parsed", manager.get(record.job_id).result.text)
            manager.delete(record.job_id)
            with self.assertRaises(JobNotFoundError):
                manager.get(record.job_id)
        finally:
            await manager.stop()

    async def test_parser_failure_is_recorded(self) -> None:
        manager = JobManager(FailingParser(), make_settings())
        await manager.start()
        try:
            record, _ = manager.submit(make_request("request-failure"))
            await wait_for_status(manager, record.job_id, JobStatus.FAILED)
            failed = manager.get(record.job_id)
            self.assertEqual("DOCLING_PARSE_ERROR", failed.error.code)
            self.assertEqual("bad document", failed.error.message)
        finally:
            await manager.stop()

    async def test_single_worker_and_bounded_waiting_queue(self) -> None:
        parser = BlockingParser()
        manager = JobManager(parser, make_settings())
        await manager.start()
        try:
            running, _ = manager.submit(make_request("request-running"))
            await asyncio.to_thread(parser.started.wait, 2)
            await wait_for_status(manager, running.job_id, JobStatus.RUNNING)

            queued = [manager.submit(make_request(f"request-{index}"))[0] for index in range(8)]
            with self.assertRaises(QueueFullError):
                manager.submit(make_request("request-overflow"))
            with self.assertRaises(ActiveJobError):
                manager.delete(queued[0].job_id)

            parser.release.set()
            for record in [running, *queued]:
                await wait_for_status(manager, record.job_id, JobStatus.SUCCEEDED)
            self.assertEqual(1, parser.max_active)
            self.assertEqual(
                ["request-running", *[f"request-{index}" for index in range(8)]],
                parser.order,
            )
        finally:
            parser.release.set()
            await manager.stop()

    async def test_queue_wait_timeout_and_result_ttl(self) -> None:
        parser = BlockingParser()
        manager = JobManager(
            parser,
            make_settings(
                ANCHR_DOCLING_QUEUE_WAIT_TIMEOUT_SECONDS=0.02,
                ANCHR_DOCLING_RESULT_TTL_SECONDS=0.02,
            ),
        )
        await manager.start()
        try:
            running, _ = manager.submit(make_request("request-running-timeout"))
            await asyncio.to_thread(parser.started.wait, 2)
            queued, _ = manager.submit(make_request("request-queued-timeout"))
            await asyncio.sleep(0.03)
            parser.release.set()

            await wait_for_status(manager, running.job_id, JobStatus.SUCCEEDED)
            await wait_for_status(manager, queued.job_id, JobStatus.FAILED)
            self.assertEqual("QUEUE_TIMEOUT", manager.get(queued.job_id).error.code)

            await asyncio.sleep(0.03)
            manager._prune_terminal_jobs()
            with self.assertRaises(JobNotFoundError):
                manager.get(running.job_id)
            with self.assertRaises(JobNotFoundError):
                manager.get(queued.job_id)
        finally:
            parser.release.set()
            await manager.stop()


if __name__ == "__main__":
    unittest.main()
