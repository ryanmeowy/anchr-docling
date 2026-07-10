import threading
import time
import unittest

from fastapi.testclient import TestClient
from pydantic import SecretStr

from anchr_docling.config import Settings
from anchr_docling.main import create_app
from anchr_docling.schemas import ParseRequest, ParseResponse

TOKEN = "a" * 64


class StubParser:
    def preload(self) -> None:
        pass

    def parse(self, request: ParseRequest) -> ParseResponse:
        return ParseResponse(
            requestId=request.request_id,
            parser="docling",
            format="markdown",
            fileType="pdf",
            text="ok",
        )


class BlockingStubParser(StubParser):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def parse(self, request: ParseRequest) -> ParseResponse:
        self.started.set()
        self.release.wait(timeout=5)
        return super().parse(request)


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        ANCHR_DOCLING_API_TOKEN=SecretStr(TOKEN),
        ANCHR_DOCLING_ALLOWED_DOWNLOAD_HOSTS="anchr.oss-cn-shanghai.aliyuncs.com",
        ANCHR_DOCLING_PRELOAD_MODELS=False,
        ANCHR_DOCLING_ENABLE_DOCS=False,
    )


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        application = create_app(parser=StubParser(), app_settings=make_settings())
        self.client_context = TestClient(application)
        self.client = self.client_context.__enter__()
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def payload(self) -> dict[str, object]:
        return {
            "requestId": "api-request-1",
            "sourceUrl": "https://anchr.oss-cn-shanghai.aliyuncs.com/anchr-dev/sample.pdf",
            "fileName": "sample.pdf",
        }

    def test_health_is_public_and_docs_are_disabled(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(200, response.status_code)
        self.assertEqual(8, response.json()["queueCapacity"])
        self.assertEqual(404, self.client.get("/docs").status_code)

    def test_jobs_require_valid_bearer_token(self) -> None:
        self.assertEqual(401, self.client.post("/v1/jobs", json=self.payload()).status_code)
        self.assertEqual(
            401,
            self.client.post(
                "/v1/jobs",
                json=self.payload(),
                headers={"Authorization": "Bearer wrong"},
            ).status_code,
        )

    def test_submit_poll_ack_and_legacy_route(self) -> None:
        submitted = self.client.post("/v1/jobs", json=self.payload(), headers=self.auth)
        self.assertEqual(202, submitted.status_code)
        job_id = submitted.json()["jobId"]

        duplicate = self.client.post("/v1/jobs", json=self.payload(), headers=self.auth)
        self.assertEqual(200, duplicate.status_code)
        self.assertEqual(job_id, duplicate.json()["jobId"])

        status_response = None
        for _ in range(100):
            status_response = self.client.get(f"/v1/jobs/{job_id}", headers=self.auth)
            if status_response.json()["status"] == "succeeded":
                break
            time.sleep(0.01)
        self.assertEqual("ok", status_response.json()["result"]["text"])
        delete_response = self.client.delete(f"/v1/jobs/{job_id}", headers=self.auth)
        self.assertEqual(204, delete_response.status_code)
        self.assertEqual(404, self.client.get(f"/v1/jobs/{job_id}", headers=self.auth).status_code)
        self.assertEqual(404, self.client.post("/v1/parse", json=self.payload()).status_code)

    def test_queue_full_returns_retry_after(self) -> None:
        parser = BlockingStubParser()
        settings = make_settings()
        settings.queue_capacity = 1
        with TestClient(create_app(parser=parser, app_settings=settings)) as client:
            first = self.payload()
            first["requestId"] = "queue-running"
            first_response = client.post("/v1/jobs", json=first, headers=self.auth)
            self.assertEqual(202, first_response.status_code)
            self.assertTrue(parser.started.wait(timeout=2))

            second = self.payload()
            second["requestId"] = "queue-waiting"
            second_response = client.post("/v1/jobs", json=second, headers=self.auth)
            self.assertEqual(202, second_response.status_code)

            third = self.payload()
            third["requestId"] = "queue-rejected"
            rejected = client.post("/v1/jobs", json=third, headers=self.auth)
            self.assertEqual(429, rejected.status_code)
            self.assertEqual("5", rejected.headers["Retry-After"])
            parser.release.set()


if __name__ == "__main__":
    unittest.main()
