import unittest
from unittest.mock import patch

from anchr_docling.docling_parser import export_parsed_document
from anchr_docling.images import attach_picture_image_metadata
from anchr_docling.schemas import ParseOptions


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


if __name__ == "__main__":
    unittest.main()
