import base64
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from anchr_docling.config import UploadTargetRule, settings
from anchr_docling.docling_parser import export_parsed_document
from anchr_docling.images import (
    OssCredentials,
    OssImageUploader,
    attach_picture_image_metadata,
    build_image_upload_context,
    credential_aad,
    decrypt_oss_credentials,
)
from anchr_docling.schemas import (
    EmbeddedImageArtifact,
    EncryptedCredentials,
    OssUploadOptions,
    ParseOptions,
)


class _FakeImage:
    size = (640, 480)


class _FakePicture:
    def get_image(self, doc: object) -> _FakeImage:
        return _FakeImage()


class _FakeDocument:
    pages: dict[int, object] = {}

    def export_to_markdown(self, page_no: int | None = None) -> str:
        return "document text"

    def export_to_text(
        self,
        page_no: int | None = None,
        traverse_pictures: bool = False,
    ) -> str:
        return "document text"


class EmbeddedImageUploadTest(unittest.TestCase):
    def test_parse_options_disable_embedded_images_by_default(self) -> None:
        options = ParseOptions()

        self.assertFalse(options.include_embedded_images)
        self.assertFalse(options.model_dump(by_alias=True)["includeEmbeddedImages"])

    def test_disabled_chunks_mode_does_not_build_upload_context(self) -> None:
        options = ParseOptions(
            outputFormat="chunks",
            useNativeChunker=True,
            includeEmbeddedImages=False,
        )
        chunks = [{"text": "document text", "textPlain": "document text"}]

        with (
            patch(
                "anchr_docling.docling_parser.build_image_upload_context",
                return_value=None,
            ) as build_upload_context,
            patch(
                "anchr_docling.docling_parser.export_native_chunks",
                return_value=chunks,
            ),
        ):
            parsed = export_parsed_document(
                _FakeDocument(),
                options,
                oss_options=object(),
                request_id="request-1",
            )

        build_upload_context.assert_not_called()
        self.assertIsNone(parsed.images)
        self.assertEqual([], parsed.warnings)
        self.assertEqual(chunks, parsed.chunks)

    def test_disabled_blocks_mode_does_not_emit_missing_credentials_warning(self) -> None:
        block = {"blockId": "pictures_0"}
        warnings = []

        attach_picture_image_metadata(
            object(),
            _FakePicture(),
            block,
            upload_context=None,
            warnings=warnings,
        )

        self.assertEqual("disabled", block["imageUploadStatus"])
        self.assertEqual([], warnings)
        self.assertEqual(640, block["imageWidth"])
        self.assertEqual(480, block["imageHeight"])

    def test_aead_credentials_are_bound_to_request_and_output_target(self) -> None:
        key = bytes(range(32))
        nonce = bytes(range(12))
        aad = credential_aad("request-1", "bucket-a", "embedded/", "oss.example.test")
        payload = json.dumps({
            "accessKeyId": "temporary-id",
            "accessKeySecret": "temporary-secret",
            "securityToken": "temporary-token",
            "expiration": "2099-01-01T00:00:00Z",
        }).encode()
        sealed = AESGCM(key).encrypt(nonce, payload, aad)
        envelope = EncryptedCredentials(
            version=1,
            keyId="app-security-v1",
            nonce=base64.b64encode(nonce).decode(),
            ciphertext=base64.b64encode(sealed[:-16]).decode(),
            tag=base64.b64encode(sealed[-16:]).decode(),
            expiration=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        )

        with patch.object(settings, "oss_encrypt_key", base64.b64encode(key).decode()):
            credentials = decrypt_oss_credentials(envelope, aad)
            self.assertEqual("temporary-id", credentials.access_key_id)
            with self.assertRaises(InvalidTag):
                decrypt_oss_credentials(
                    envelope,
                    credential_aad("request-2", "bucket-a", "embedded/", "oss.example.test"),
                )

    def test_disallowed_upload_target_is_rejected_before_decryption(self) -> None:
        options = OssUploadOptions(
            endpoint="oss.denied.test",
            bucket="bucket-a",
            basePath="embedded/ingestion/task-1/images/",
            objectKeyLayout="ATTEMPT_PREFIX_V1",
            encryptedCredentials=EncryptedCredentials(
                version=1,
                keyId="app-security-v1",
                nonce="nonce",
                ciphertext="ciphertext",
                tag="tag",
                expiration="2099-01-01T00:00:00Z",
            ),
        )
        warnings = []
        allowed = [
            UploadTargetRule(
                endpoint="oss.allowed.test",
                bucket="bucket-a",
                prefix="embedded/",
            )
        ]

        with (
            patch.object(settings, "allowed_upload_targets", allowed),
            patch("anchr_docling.images.decrypt_oss_credentials") as decrypt,
            patch("anchr_docling.images.OssImageUploader") as uploader,
        ):
            context = build_image_upload_context(
                options,
                warnings,
                request_id="request-1",
            )

        self.assertIsNone(context.uploader)
        self.assertEqual("image_upload_target_not_allowed", context.unavailable_code)
        self.assertEqual(
            ["image_upload_target_not_allowed"],
            [warning.code for warning in warnings],
        )
        decrypt.assert_not_called()
        uploader.assert_not_called()

    def test_artifact_preserves_all_picture_provenance_coordinates(self) -> None:
        artifact = EmbeddedImageArtifact.model_validate({
            "artifactVersion": 1,
            "blockId": "pictures/0",
            "imageObjectKey": "embedded/pictures_0.png",
            "uploadStatus": "UPLOADED",
            "bboxes": [
                {"pageNo": 2, "bbox": {
                    "l": 1, "t": 2, "r": 3, "b": 4, "coord_origin": "BOTTOMLEFT"
                }},
                {"pageNo": 3, "bbox": {
                    "l": 5, "t": 6, "r": 7, "b": 8, "coordOrigin": "TOPLEFT"
                }},
            ],
        })

        dumped = artifact.model_dump(by_alias=True)
        self.assertEqual([2, 3], [entry["pageNo"] for entry in dumped["bboxes"]])
        self.assertEqual("BOTTOMLEFT", dumped["bboxes"][0]["bbox"]["coordOrigin"])

    def test_attempt_prefix_layout_uses_stable_block_key(self) -> None:
        uploader = OssImageUploader(
            endpoint="oss.example.test",
            bucket_name="bucket-a",
            base_path="embedded/ingestion/task-1/item-1/parse/2/images/",
            credentials=OssCredentials("id", "secret", "token"),
            request_id="task-1:item-1:2",
            object_key_layout="ATTEMPT_PREFIX_V1",
        )

        self.assertEqual(
            "embedded/ingestion/task-1/item-1/parse/2/images/pictures_0.png",
            uploader.build_image_key("pictures/0"),
        )
        self.assertEqual(
            uploader.build_image_key("pictures/0"),
            uploader.build_image_key("pictures/0"),
        )

    def test_legacy_layout_keeps_request_suffix(self) -> None:
        uploader = OssImageUploader(
            endpoint="oss.example.test",
            bucket_name="bucket-a",
            base_path="embedded",
            credentials=OssCredentials("id", "secret", "token"),
            request_id="task-1:item-1:2",
        )

        self.assertEqual(
            "embedded/pictures_0_task-1_item-1_2.png",
            uploader.build_image_key("pictures/0"),
        )


if __name__ == "__main__":
    unittest.main()
