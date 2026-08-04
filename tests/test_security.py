import os
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr
from pydantic_settings import SettingsError

from anchr_docling.config import Settings, UploadTargetRule
from anchr_docling.errors import ImageUploadError, SourceDownloadError
from anchr_docling.security import (
    open_validated_response,
    validate_download_url,
    validate_upload_target,
)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ANCHR_DOCLING_API_TOKEN": SecretStr("s" * 64),
        "ANCHR_DOCLING_ALLOWED_DOWNLOAD_HOSTS": "anchr.oss-cn-shanghai.aliyuncs.com",
        "ANCHR_DOCLING_PRELOAD_MODELS": False,
    }
    values.update(overrides)
    return Settings(
        _env_file=None,
        **values,
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


class UploadSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings(
            ANCHR_DOCLING_ALLOWED_UPLOAD_TARGETS=[
                {
                    "endpoint": "oss.example.test",
                    "bucket": "bucket-a",
                    "prefix": "embedded/",
                },
                {
                    "endpoint": "oss.other.test",
                    "bucket": "bucket-b",
                    "prefix": "other/",
                },
            ]
        )

    def test_allows_exact_atomic_target_and_prefix_descendants(self) -> None:
        validate_upload_target(
            "oss.example.test",
            "bucket-a",
            "embedded/ingestion/task-1/images/",
            self.settings,
        )

    def test_rejects_mismatch_cross_rule_and_adjacent_prefix(self) -> None:
        invalid = [
            ("oss.other.test", "bucket-a", "embedded/ingestion/"),
            ("oss.example.test", "bucket-b", "embedded/ingestion/"),
            ("oss.example.test", "bucket-a", "embedded-evil/ingestion/"),
            ("oss.example.test", "bucket-a", "embedded/../other/"),
            ("oss.example.test", "bucket-a", "embedded\\other"),
            ("oss.example.test", "bucket-a", "embedded/\u0085other"),
        ]
        for endpoint, bucket, prefix in invalid:
            with self.subTest(
                endpoint=endpoint,
                bucket=bucket,
                prefix=prefix,
            ), self.assertRaises(ImageUploadError):
                validate_upload_target(endpoint, bucket, prefix, self.settings)

    def test_empty_allowlist_starts_but_rejects_uploads(self) -> None:
        settings = make_settings()
        settings.validate_runtime()

        with self.assertRaises(ImageUploadError):
            validate_upload_target(
                "oss.example.test",
                "bucket-a",
                "embedded/ingestion/",
                settings,
            )

    def test_upload_target_rule_rejects_wildcards_and_empty_prefix(self) -> None:
        for rule in (
            {"endpoint": "*.example.test", "bucket": "bucket-a", "prefix": "embedded/"},
            {"endpoint": "oss.example.test", "bucket": "bucket-a", "prefix": "/"},
        ):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                UploadTargetRule.model_validate(rule)

    def test_upload_targets_load_from_json_environment_value(self) -> None:
        serialized = (
            '[{"endpoint":"oss.example.test","bucket":"bucket-a","prefix":"embedded/"}]'
        )
        with patch.dict(
            os.environ,
            {"ANCHR_DOCLING_ALLOWED_UPLOAD_TARGETS": serialized},
            clear=False,
        ):
            settings = make_settings()

        self.assertEqual(
            ("oss.example.test", "bucket-a", "embedded"),
            (
                settings.allowed_upload_targets[0].endpoint,
                settings.allowed_upload_targets[0].bucket,
                settings.allowed_upload_targets[0].prefix,
            ),
        )

    def test_malformed_upload_target_json_fails_settings_creation(self) -> None:
        with patch.dict(
            os.environ,
            {"ANCHR_DOCLING_ALLOWED_UPLOAD_TARGETS": "not-json"},
            clear=False,
        ), self.assertRaises(SettingsError):
            make_settings()


if __name__ == "__main__":
    unittest.main()
