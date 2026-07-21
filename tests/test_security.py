import unittest

import httpx
from pydantic import SecretStr

from anchr_docling.config import Settings
from anchr_docling.errors import SourceDownloadError
from anchr_docling.security import open_validated_response, validate_download_url


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        ANCHR_DOCLING_API_TOKEN=SecretStr("s" * 64),
        ANCHR_DOCLING_ALLOWED_DOWNLOAD_HOSTS="anchr.oss-cn-shanghai.aliyuncs.com",
        ANCHR_DOCLING_PRELOAD_MODELS=False,
    )


class DownloadSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()

    def test_rejects_http_private_and_unlisted_hosts(self) -> None:
        invalid = [
            "http://anchr.oss-cn-shanghai.aliyuncs.com/file.pdf",
            "https://127.0.0.1/file.pdf",
            "https://100.64.0.1/file.pdf",
            "https://example.com/file.pdf",
            "https://anchr.oss-cn-shanghai.aliyuncs.com:8443/file.pdf",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(SourceDownloadError):
                validate_download_url(url, self.settings)

    def test_allows_exact_https_oss_host(self) -> None:
        validate_download_url(
            "https://anchr.oss-cn-shanghai.aliyuncs.com/anchr-dev/file.pdf?signature=secret",
            self.settings,
        )

    def test_revalidates_redirect_target(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/latest/meta-data"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(SourceDownloadError):
                open_validated_response(
                    client,
                    "https://anchr.oss-cn-shanghai.aliyuncs.com/file.pdf",
                    self.settings,
                )


if __name__ == "__main__":
    unittest.main()
