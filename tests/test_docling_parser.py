import unittest
from unittest.mock import patch

from anchr_docling.docling_parser import (
    DoclingParser,
    ParsedDocument,
    _restore_md_images,
)
from anchr_docling.errors import DoclingParseError
from anchr_docling.schemas import ParseRequest


class _FakeImage:
    size = (32, 24)


class _FakeUploader:
    def __init__(self) -> None:
        self.block_ids: list[str] = []

    def upload_png(self, block_id: str, _image: object) -> str:
        self.block_ids.append(block_id)
        return f"embedded/{block_id}.png"

    def build_image_url(self, image_key: str) -> str:
        return f"https://bucket.example.test/{image_key}"


def request(file_name: str) -> ParseRequest:
    return ParseRequest(
        requestId="request-1",
        sourceUrl=f"https://anchr.oss-cn-shanghai.aliyuncs.com/{file_name}",
        fileName=file_name,
    )


def empty_document() -> ParsedDocument:
    return ParsedDocument(text="", pages=[], quality_text="", chunks=[])


class DoclingParserTest(unittest.TestCase):
    @patch("anchr_docling.docling_parser.encode_png", return_value=b"png")
    @patch("anchr_docling.docling_parser._download_md_image", return_value=_FakeImage())
    def test_markdown_image_index_advances_across_chunks(
        self,
        _download_md_image,
        _encode_png,
    ) -> None:
        uploader = _FakeUploader()
        cache = {}
        images = []
        references = [
            {"alt": "A", "url": "https://example.test/a.png"},
            {"alt": "B", "url": "https://example.test/b.png"},
        ]

        first, next_index = _restore_md_images(
            "<!-- image -->",
            references,
            uploader,
            [],
            cache,
            images,
        )
        second, next_index = _restore_md_images(
            "<!-- image -->",
            references,
            uploader,
            [],
            cache,
            images,
            next_index,
        )

        self.assertIn("embedded/md_image_0.png", first)
        self.assertIn("embedded/md_image_1.png", second)
        self.assertEqual(2, next_index)
        self.assertEqual(["md_image_0", "md_image_1"], uploader.block_ids)
        self.assertEqual(["md_image_0", "md_image_1"], [image["blockId"] for image in images])

    @patch("anchr_docling.docling_parser.encode_png", return_value=b"png")
    @patch("anchr_docling.docling_parser._download_md_image", return_value=_FakeImage())
    def test_repeated_markdown_url_keeps_upload_cache_and_document_position(
        self,
        _download_md_image,
        _encode_png,
    ) -> None:
        uploader = _FakeUploader()
        cache = {}
        images = []
        references = [
            {"alt": "first", "url": "https://example.test/shared.png"},
            {"alt": "second", "url": "https://example.test/shared.png"},
            {"alt": "third", "url": "https://example.test/third.png"},
        ]
        index = 0
        restored = []

        for _ in references:
            text, index = _restore_md_images(
                "<!-- image -->",
                references,
                uploader,
                [],
                cache,
                images,
                index,
            )
            restored.append(text)

        self.assertEqual(3, index)
        self.assertEqual(["md_image_0", "md_image_2"], uploader.block_ids)
        self.assertEqual(2, len(images))
        self.assertIn("embedded/md_image_0.png", restored[0])
        self.assertIn("embedded/md_image_0.png", restored[1])
        self.assertIn("embedded/md_image_2.png", restored[2])

    @patch("anchr_docling.docling_parser._validate_image_size")
    @patch("anchr_docling.docling_parser.download_source")
    @patch("anchr_docling.docling_parser.convert_document")
    def test_empty_image_is_a_valid_result(
        self,
        convert_document,
        _download_source,
        _validate_image_size,
    ) -> None:
        convert_document.return_value = empty_document()

        result = DoclingParser().parse(request("image.png"))

        self.assertEqual("image", result.file_type)
        self.assertEqual("", result.text)
        self.assertEqual([], result.chunks)

    @patch("anchr_docling.docling_parser.download_source")
    @patch("anchr_docling.docling_parser.convert_document")
    def test_empty_non_image_remains_a_parse_error(
        self,
        convert_document,
        _download_source,
    ) -> None:
        convert_document.return_value = empty_document()

        with self.assertRaisesRegex(
            DoclingParseError,
            "docling returned empty content",
        ):
            DoclingParser().parse(request("document.pdf"))


if __name__ == "__main__":
    unittest.main()
