import unittest
from unittest.mock import patch

from anchr_docling.docling_parser import DoclingParser, ParsedDocument
from anchr_docling.errors import DoclingParseError
from anchr_docling.schemas import ParseRequest


def request(file_name: str) -> ParseRequest:
    return ParseRequest(
        requestId="request-1",
        sourceUrl=f"https://anchr.oss-cn-shanghai.aliyuncs.com/{file_name}",
        fileName=file_name,
    )


def empty_document() -> ParsedDocument:
    return ParsedDocument(text="", pages=[], quality_text="", chunks=[])


class DoclingParserTest(unittest.TestCase):
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
