import base64
import json
import logging
import os
import platform
import tempfile
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from anchr_docling.config import settings
from anchr_docling.schemas import (
    EncryptedCredentials,
    OssUploadOptions,
    OutputFormat,
    ParsedPage,
    ParseOptions,
    ParseRequest,
    ParseResponse,
    ParseWarning,
)


class SourceDownloadError(RuntimeError):
    pass


class DoclingParseError(RuntimeError):
    pass


class ImageUploadError(RuntimeError):
    pass


_log = logging.getLogger(__name__)
_converter_lock = threading.Lock()
_converters: dict[tuple[bool, str | None, bool], Any] = {}
_preloaded_artifacts_path: Path | None = None


class ParsedDocument:
    def __init__(
        self,
        text: str,
        pages: list[ParsedPage],
        quality_text: str,
        ocr_used: bool,
        document: dict[str, Any] | None = None,
        blocks: list[dict[str, Any]] | None = None,
        chunks: list[dict[str, Any]] | None = None,
        images: list[dict[str, Any]] | None = None,
        warnings: list[ParseWarning] | None = None,
    ) -> None:
        self.text = text
        self.pages = pages
        self.quality_text = quality_text
        self.ocr_used = ocr_used
        self.document = document
        self.blocks = blocks
        self.chunks = chunks
        self.images = images
        self.warnings = warnings or []


@dataclass(frozen=True)
class OssCredentials:
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: str | None = None


@dataclass
class ImageUploadContext:
    uploader: "OssImageUploader | None"
    unavailable_code: str | None = None
    unavailable_message: str | None = None


class DoclingParser:
    def preload(self) -> None:
        preload_docling_models()

    def parse(self, request: ParseRequest) -> ParseResponse:
        suffix = resolve_suffix(request.file_name, str(request.source_url))
        with tempfile.TemporaryDirectory(prefix="anchr-docling-") as tmp_dir:
            source_path = Path(tmp_dir) / f"source{suffix}"
            download_source(str(request.source_url), source_path)
            parsed = convert_document(source_path, request.options, request.oss, request.request_id)

        return ParseResponse(
            requestId=request.request_id,
            parser="docling",
            format=request.options.output_format,
            text=parsed.text,
            pages=parsed.pages,
            document=parsed.document,
            blocks=parsed.blocks,
            chunks=parsed.chunks,
            images=parsed.images,
            warnings=parsed.warnings or None,
        )


def download_source(source_url: str, target_path: Path) -> None:
    timeout = httpx.Timeout(
        connect=settings.connect_timeout_seconds,
        read=settings.read_timeout_seconds,
        write=settings.connect_timeout_seconds,
        pool=settings.connect_timeout_seconds,
    )
    max_bytes = settings.max_download_mb * 1024 * 1024
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AnchrDocling/1.0)",
        "Accept": "application/pdf,application/octet-stream,text/html,text/plain,*/*",
    }

    total = 0
    try:
        with httpx.stream(
            "GET",
            source_url,
            headers=headers,
            follow_redirects=True,
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            with target_path.open("wb") as output:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceDownloadError(
                            f"source file exceeds {settings.max_download_mb} MB limit"
                        )
                    output.write(chunk)
    except httpx.HTTPStatusError as exc:
        raise SourceDownloadError(
            f"source URL responded HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise SourceDownloadError(f"failed to download source URL: {exc}") from exc


def convert_document(
    source_path: Path,
    options: ParseOptions,
    oss_options: OssUploadOptions | None,
    request_id: str | None = None,
) -> ParsedDocument:
    try:
        components = load_docling_components()
        configure_torch_device()
        from docling.datamodel.pipeline_options import (
            EasyOcrOptions,
            OcrMacOptions,
            RapidOcrOptions,
            TesseractOcrOptions,
        )
    except ImportError as exc:
        raise DoclingParseError("docling is not installed") from exc

    try:
        ocr_options_by_engine = {
            "easyocr": EasyOcrOptions,
            "ocrmac": OcrMacOptions,
            "rapidocr": RapidOcrOptions,
            "tesseract": TesseractOcrOptions,
        }
        parsed = run_docling_convert(
            source_path,
            options,
            components,
            ocr_options_by_engine,
            ocr=options.ocr,
            ocr_engine=first_configured_ocr_engine(),
            oss_options=oss_options,
            request_id=request_id,
        )
        if (
            options.validate_text_quality
            and not options.ocr
            and looks_garbled(parsed.quality_text)
            and options.ocr_fallback
        ):
            parsed = run_ocr_fallback_chain(
                source_path,
                options,
                components,
                ocr_options_by_engine,
                oss_options,
                request_id=request_id,
            )
    except Exception as exc:
        raise DoclingParseError(f"docling parse failed: {exc}") from exc

    if not parsed.quality_text or not parsed.quality_text.strip():
        raise DoclingParseError("docling returned empty content")
    if options.validate_text_quality and looks_garbled(parsed.quality_text):
        raise DoclingParseError(
            "docling returned garbled text. The PDF may use custom font encoding; retry with "
            "`ocrFallback: true` or `ocr: true`."
        )
    if isinstance(parsed.text, str):
        parsed.text = parsed.text.strip()
    return parsed


def run_docling_convert(
    source_path: Path,
    options: ParseOptions,
    components: dict[str, Any],
    ocr_options_by_engine: dict[str, type],
    *,
    ocr: bool,
    ocr_engine: str | None,
    oss_options: OssUploadOptions | None,
    request_id: str | None = None,
) -> ParsedDocument:
    converter = get_document_converter(
        options,
        components,
        ocr_options_by_engine,
        ocr=ocr,
        ocr_engine=ocr_engine,
    )
    result = converter.convert(source_path)
    return export_parsed_document(
        result.document,
        options,
        oss_options,
        ocr_used=ocr,
        request_id=request_id,
    )


def run_ocr_fallback_chain(
    source_path: Path,
    options: ParseOptions,
    components: dict[str, Any],
    ocr_options_by_engine: dict[str, type],
    oss_options: OssUploadOptions | None,
    request_id: str | None = None,
) -> ParsedDocument:
    errors: list[str] = []
    for engine in configured_ocr_engines():
        try:
            parsed = run_docling_convert(
                source_path,
                options,
                components,
                ocr_options_by_engine,
                ocr=True,
                ocr_engine=engine,
                oss_options=oss_options,
                request_id=request_id,
            )
            if (
                parsed.quality_text
                and parsed.quality_text.strip()
                and not looks_garbled(parsed.quality_text)
            ):
                return parsed
            errors.append(f"{engine}: empty or garbled OCR result")
        except Exception as exc:
            errors.append(f"{engine}: {exc}")
    raise DoclingParseError("all OCR engines failed; " + " | ".join(errors))


def export_parsed_document(
    document: Any,
    options: ParseOptions,
    oss_options: OssUploadOptions | None = None,
    *,
    ocr_used: bool = False,
    request_id: str | None = None,
) -> ParsedDocument:
    output_format = options.output_format
    json_document = document.export_to_dict() if output_format == "json" else None
    warnings: list[ParseWarning] = []
    upload_context = (
        build_image_upload_context(oss_options, warnings, request_id=request_id)
        if output_format in {"blocks", "markdown", "chunks"}
        else None
    )
    blocks = (
        export_blocks(document, upload_context, warnings)
        if output_format == "blocks"
        else None
    )
    pages = [
        ParsedPage(
            pageNo=page_no,
            text=export_page_content(
                document,
                output_format,
                page_no,
            ),
        )
        for page_no in sorted(document.pages)
    ]
    text = export_document_content(document, output_format)

    # Enrich markdown output with OSS image URLs.
    images: list[dict[str, Any]] | None = None
    if output_format == "markdown" and upload_context is not None:
        images = []
        image_cache: dict[str, str | None] = {}
        text = enrich_markdown_text(
            document, text, upload_context, warnings, image_cache, images=images,
        )
        for page in pages:
            if page.page_no is not None:
                page.text = enrich_markdown_text(
                    document, page.text, upload_context, warnings,
                    image_cache, page_no=page.page_no, images=images,
                )

    chunks = (
        export_markdown_chunks(text, pages, options, warnings)
        if output_format == "chunks"
        else None
    )

    # Enrich text + page text + chunk text with OSS image URLs.
    if output_format == "chunks" and upload_context is not None:
        if images is None:
            images = []
        image_cache: dict[str, str | None] = {}
        text = enrich_markdown_text(
            document, text, upload_context, warnings, image_cache, images=images,
        )
        for page in pages:
            if page.page_no is not None:
                page.text = enrich_markdown_text(
                    document, page.text, upload_context, warnings,
                    image_cache, page_no=page.page_no, images=images,
                )
        if chunks:
            for chunk in chunks:
                chunk["text"] = enrich_markdown_text(
                    document, chunk["text"], upload_context, warnings,
                    image_cache, images=images,
                )

    return ParsedDocument(
        text=text,
        pages=pages,
        quality_text=document.export_to_text(),
        ocr_used=ocr_used,
        document=json_document,
        blocks=blocks,
        chunks=chunks,
        images=images,
        warnings=warnings,
    )


def export_document_content(
    document: Any,
    output_format: OutputFormat,
) -> str:
    if output_format == "markdown" or output_format == "chunks":
        return document.export_to_markdown()
    if output_format == "html":
        return document.export_to_html()
    if output_format in {"text", "json", "blocks"}:
        return document.export_to_text()
    raise DoclingParseError(f"unsupported output format: {output_format}")


def export_page_content(
    document: Any,
    output_format: OutputFormat,
    page_no: int,
) -> str:
    if output_format == "markdown" or output_format == "chunks":
        return document.export_to_markdown(page_no=page_no).strip()
    if output_format == "html":
        return document.export_to_html(page_no=page_no).strip()
    if output_format in {"text", "json", "blocks"}:
        return document.export_to_text(page_no=page_no, traverse_pictures=True).strip()
    raise DoclingParseError(f"unsupported output format: {output_format}")


def export_blocks(
    document: Any,
    upload_context: ImageUploadContext | None,
    warnings: list[ParseWarning],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item, _ in document.iterate_items(with_groups=True, traverse_pictures=True):
        ref = getattr(item, "self_ref", None)
        if not ref or ref == "#/body":
            continue

        block = {
            "blockId": ref_to_block_id(ref),
            "type": resolve_block_type(item),
        }
        add_if_present(block, "text", getattr(item, "text", None))
        if block["type"] == "group":
            add_if_present(block, "label", enum_value(getattr(item, "label", None)))
        add_if_present(block, "pageNo", resolve_page_no(document, item))
        add_if_present(block, "parentRef", resolve_parent_ref(item))
        add_if_present(block, "bbox", resolve_bbox(item))

        child_refs = resolve_child_refs(item)
        if child_refs:
            block["children"] = child_refs

        if resolve_block_type(item) == "picture":
            block["childrenText"] = collect_child_text(document, item)
            attach_picture_image_metadata(document, item, block, upload_context, warnings)

        blocks.append(block)
    return blocks


def export_markdown_chunks(
    full_text: str,
    pages: list[ParsedPage],
    options: ParseOptions,
    warnings: list[ParseWarning],
) -> list[dict[str, Any]]:
    """Chunk markdown text with structure-aware splitting.

    Headings (##/###) act as strong boundaries.  Tables and images are kept
    intact.  Each chunk carries both the raw markdown (``text``, for LLM
    consumption) and a plain-text variant (``textPlain``, for embedding).
    """
    target_chars = max(1, options.chunk_target_chars)
    max_chars = max(target_chars, options.chunk_max_chars)

    canonical = full_text.strip()
    if not canonical:
        return []

    blocks = parse_markdown_blocks(canonical)
    if not blocks:
        return []

    spans = _build_markdown_page_spans(canonical, pages)

    chunks: list[dict[str, Any]] = []
    current_blocks: list[dict[str, Any]] = []

    def _cur_len() -> int:
        return len(join_blocks_text(current_blocks))

    for block in blocks:
        cur_len = _cur_len()

        # Table that exceeds max_chars on its own: emit as single chunk.
        if block["type"] == "table" and len(block["text"]) > max_chars:
            if current_blocks:
                chunks.append(_build_md_chunk(len(chunks), current_blocks, spans))
                current_blocks = []
            chunks.append(_build_md_chunk(len(chunks), [block], spans))
            continue

        # Heading forces flush when we already have enough content.
        if block["type"] == "heading" and cur_len >= target_chars:
            chunks.append(_build_md_chunk(len(chunks), current_blocks, spans))
            current_blocks = []

        # Would overflow: flush current batch.
        if current_blocks and cur_len + len(block["text"]) > max_chars and cur_len >= target_chars:
            chunks.append(_build_md_chunk(len(chunks), current_blocks, spans))
            current_blocks = []

        current_blocks.append(block)

    if current_blocks:
        chunks.append(_build_md_chunk(len(chunks), current_blocks, spans))

    for chunk in chunks:
        chunk["source"] = "native"
    return chunks


def parse_markdown_blocks(md_text: str) -> list[dict[str, Any]]:
    """Parse markdown text into typed logical blocks.

    Returns blocks like:
      {"type": "heading", "text": "## Overview", "level": 2, "offset": 42}
      {"type": "paragraph", "text": "Some text.", "offset": 78}
      …
    ``offset`` is the byte position of the block's text within *md_text*.
    """
    parsed: list[dict[str, Any]] = []
    pos = 0
    n = len(md_text)

    while pos < n:
        # Skip leading whitespace between blocks.
        while pos < n and md_text[pos] in ("\n", " ", "\t", "\r"):
            pos += 1
        if pos >= n:
            break

        # Find the next \n\n boundary.
        end = md_text.find("\n\n", pos)
        if end == -1:
            end = n

        block_text = md_text[pos:end].strip()
        if not block_text:
            pos = end + 2 if end < n else n
            continue

        line = block_text.split("\n", 1)[0]

        if line.startswith("```"):
            parsed.append({"type": "code", "text": block_text, "offset": pos})
        elif line.startswith("<!-- image -->") or line.startswith("!["):
            parsed.append({"type": "image", "text": block_text, "offset": pos})
        elif line.startswith("|"):
            parsed.append({"type": "table", "text": block_text, "offset": pos})
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            parsed.append({"type": "heading", "text": block_text, "level": level, "offset": pos})
        elif line in ("---", "***", "___") or set(line) <= {"-", "*", "_", " "}:
            pass  # horizontal rule – skip
        elif line.startswith(("- ", "* ")) or (line[0].isdigit() and ". " in line[:4]):
            parsed.append({"type": "list_item", "text": block_text, "offset": pos})
        else:
            parsed.append({"type": "paragraph", "text": block_text, "offset": pos})

        pos = end + 2 if end < n else n

    return parsed


def join_blocks_text(blocks: list[dict[str, Any]]) -> str:
    return "\n\n".join(b["text"] for b in blocks if b.get("text"))


def _build_md_chunk(
    index: int,
    blocks: list[dict[str, Any]],
    page_spans: list[dict[str, Any]],
) -> dict[str, Any]:
    md_text = join_blocks_text(blocks)
    plain = strip_markdown(md_text)

    chunk: dict[str, Any] = {
        "chunkId": f"chunks/{index}",
        "type": "section",
        "text": md_text,
        "textPlain": plain,
        "charCount": len(md_text),
    }

    page_range = _resolve_chunk_page_range(blocks, page_spans)
    if page_range is not None:
        chunk["pageRange"] = page_range

    return chunk


def _build_markdown_page_spans(
    full_text: str,
    pages: list[ParsedPage],
) -> list[dict[str, Any]]:
    """Map page markdown texts to character offsets in the full markdown."""
    spans: list[dict[str, Any]] = []
    cursor = 0
    for page in pages:
        page_text = page.text.strip()
        if not page_text:
            continue
        found = full_text.find(page_text, cursor)
        if found >= 0:
            start = found
            end = found + len(page_text)
        else:
            start = min(cursor, len(full_text))
            end = min(len(full_text), start + len(page_text))
        spans.append(
            {"start": start, "end": end, "pageNo": page.page_no}
        )
        cursor = end
    return spans


def _resolve_page_no(
    offset: int,
    page_spans: list[dict[str, Any]],
) -> int | None:
    """Return the page number for a character offset."""
    for span in page_spans:
        if span["start"] <= offset < span["end"]:
            return span.get("pageNo")
    if page_spans:
        nearest = min(page_spans, key=lambda s: abs(s["start"] - offset))
        return nearest.get("pageNo")
    return None


def _resolve_chunk_page_range(
    blocks: list[dict[str, Any]],
    page_spans: list[dict[str, Any]],
) -> list[int] | None:
    """Determine page range for a chunk from its blocks' offsets."""
    page_nums: set[int] = set()
    for block in blocks:
        offset: int = block.get("offset", 0)
        pn = _resolve_page_no(offset, page_spans)
        if isinstance(pn, int):
            page_nums.add(pn)
    if not page_nums:
        return None
    return [min(page_nums), max(page_nums)]


def strip_markdown(text: str) -> str:
    """Strip markdown formatting to produce clean text for embedding."""
    import re as _re

    t = text
    # Remove code fences with content
    t = _re.sub(r"```[^`]*```", "", t)
    # Remove images: ![alt](url) and <!-- image -->
    t = _re.sub(r"!\[.*?\]\(.*?\)", "", t)
    t = t.replace("<!-- image -->", "")
    # Remove links, keep text: [text](url)
    t = _re.sub(r"\[([^\]]*)\]\(.*?\)", r"\1", t)
    # Strip heading markers
    t = _re.sub(r"^#{1,6}\s+", "", t, flags=_re.MULTILINE)
    # Bold / italic
    t = _re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = _re.sub(r"__(.+?)__", r"\1", t)
    t = _re.sub(r"\*(.+?)\*", r"\1", t)
    t = _re.sub(r"_(.+?)_", r"\1", t)
    # Inline code
    t = _re.sub(r"`([^`]+)`", r"\1", t)
    # Table formatting: strip pipes and alignment row
    t = _re.sub(r"\|", " ", t)
    t = _re.sub(r"\s{2,}", " ", t)
    # Clean up blank lines
    t = _re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def build_image_upload_context(
    oss_options: OssUploadOptions | None,
    warnings: list[ParseWarning],
    request_id: str | None = None,
) -> ImageUploadContext:
    if oss_options is None:
        return ImageUploadContext(
            uploader=None,
            unavailable_code="image_upload_skipped_no_credentials",
            unavailable_message="OSS credentials were not provided; image upload was skipped.",
        )

    try:
        credentials = decrypt_oss_credentials(oss_options.encrypted_credentials)
        return ImageUploadContext(
            uploader=OssImageUploader(
                endpoint=oss_options.endpoint,
                bucket_name=oss_options.bucket,
                base_path=oss_options.base_path,
                credentials=credentials,
                request_id=request_id,
            )
        )
    except Exception as exc:
        message = sanitize_error_message(exc)
        add_warning(
            warnings,
            code="image_upload_failed",
            message=f"OSS credentials could not be decrypted: {message}",
        )
        return ImageUploadContext(
            uploader=None,
            unavailable_code="image_upload_failed",
            unavailable_message="OSS credentials could not be decrypted.",
        )


def attach_picture_image_metadata(
    document: Any,
    item: Any,
    block: dict[str, Any],
    upload_context: ImageUploadContext | None,
    warnings: list[ParseWarning],
) -> None:
    block_id = block["blockId"]
    block["imageKey"] = None
    block["imageUploadStatus"] = "no_image"
    block["imageUploadError"] = None

    image = get_picture_image(document, item)
    if image is None:
        return

    block["imageMimeType"] = "image/png"
    block["imageWidth"], block["imageHeight"] = image.size

    if upload_context is None or upload_context.uploader is None:
        code = (
            upload_context.unavailable_code
            if upload_context is not None and upload_context.unavailable_code is not None
            else "image_upload_skipped_no_credentials"
        )
        message = (
            upload_context.unavailable_message
            if upload_context is not None and upload_context.unavailable_message is not None
            else "OSS credentials were not provided; image upload was skipped."
        )
        block["imageUploadStatus"] = (
            "skipped_no_credentials"
            if code == "image_upload_skipped_no_credentials"
            else "failed"
        )
        if block["imageUploadStatus"] == "failed":
            block["imageUploadError"] = message
        add_warning(warnings, code=code, message=message, block_id=block_id)
        return

    try:
        image_key = upload_context.uploader.upload_png(block_id, image)
        block["imageKey"] = image_key
        block["imageUploadStatus"] = "uploaded"
    except Exception:
        message = "Failed to upload image to OSS."
        block["imageUploadStatus"] = "failed"
        block["imageUploadError"] = message
        add_warning(
            warnings,
            code="image_upload_failed",
            message=message,
            block_id=block_id,
        )


def get_picture_image(document: Any, item: Any) -> Any | None:
    get_image = getattr(item, "get_image", None)
    if get_image is None:
        return None
    try:
        return get_image(doc=document)
    except Exception:
        return None


def _get_picture_alt_text(document: Any, item: Any) -> str:
    """Get alt text for a picture item.

    Prefers the document caption via ``FloatingItem.caption_text``.
    Falls back to collecting child/caption text (same logic as blocks mode).
    """
    caption_text_fn = getattr(item, "caption_text", None)
    if caption_text_fn is not None:
        try:
            return caption_text_fn(document)
        except Exception:
            pass
    texts = collect_child_text(document, item)
    return texts[0] if texts else ""


def enrich_markdown_text(
    document: Any,
    markdown_text: str,
    upload_context: ImageUploadContext | None,
    warnings: list[ParseWarning],
    image_cache: dict[str, str | None],
    page_no: int | None = None,
    images: list[dict[str, Any]] | None = None,
) -> str:
    """Replace ``<!-- image -->`` placeholders with OSS-hosted markdown images.

    Iterates pictures in document order, uploads them via the uploader,
    and substitutes each placeholder sequentially.  A per-self-ref cache
    avoids uploading the same picture more than once (shared between
    document-level and per-page texts).

    When *images* is provided, each picture is appended as a structured
    dict (``url``, ``pageNo``, ``blockId``, ``alt``) for the top-level
    ``images`` response field.
    """
    placeholder = "<!-- image -->"
    if placeholder not in markdown_text:
        return markdown_text

    # Collect picture items in document traversal order,
    # optionally filtered by page.
    picture_items: list[tuple[Any, str]] = []
    for item, _ in document.iterate_items(
        with_groups=True, traverse_pictures=True
    ):
        ref = getattr(item, "self_ref", None)
        if not ref or resolve_block_type(item) != "picture":
            continue
        if page_no is not None:
            item_page_no = resolve_page_no(document, item)
            if item_page_no != page_no:
                continue
        picture_items.append((item, ref_to_block_id(ref)))

    if not picture_items:
        return markdown_text

    # Build ordered lists of alt texts and image URLs.
    alt_texts: list[str] = []
    image_urls: list[str | None] = []

    for item, block_id in picture_items:
        self_ref: str = getattr(item, "self_ref", "")

        # Cache hit: reuse previously resolved URL (or None).
        if self_ref in image_cache:
            image_urls.append(image_cache[self_ref])
            alt_texts.append(
                image_cache.get(f"{self_ref}_alt", "") or ""
            )
            continue

        alt_text = _get_picture_alt_text(document, item)
        alt_texts.append(alt_text)
        image_cache[f"{self_ref}_alt"] = alt_text

        url: str | None = None
        if upload_context is not None and upload_context.uploader is not None:
            image = get_picture_image(document, item)
            if image is not None:
                try:
                    image_key = upload_context.uploader.upload_png(
                        block_id, image
                    )
                    url = upload_context.uploader.build_image_url(image_key)
                except Exception:
                    add_warning(
                        warnings,
                        code="image_upload_failed",
                        message="Failed to upload image to OSS.",
                        block_id=block_id,
                    )

        image_urls.append(url)
        image_cache[self_ref] = url

        if images is not None:
            item_page_no = resolve_page_no(document, item)
            images.append({
                "url": url,
                "pageNo": item_page_no,
                "blockId": block_id,
                "alt": alt_text,
            })

    # Replace placeholders one at a time, in order.
    result = markdown_text
    for url, alt in zip(image_urls, alt_texts):
        if url is not None:
            result = result.replace(
                placeholder, f"![{alt}]({url})", 1
            )

    return result


class OssImageUploader:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket_name: str,
        base_path: str,
        credentials: OssCredentials,
        request_id: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.bucket_name = bucket_name
        self.base_path = base_path.strip("/")
        self.credentials = credentials
        self.request_id = request_id

    def upload_png(self, block_id: str, image: Any) -> str:
        try:
            import oss2
        except ImportError as exc:
            raise ImageUploadError("oss2 is not installed") from exc

        image_key = self.build_image_key(block_id)
        content = encode_png(image)
        auth = oss2.StsAuth(
            self.credentials.access_key_id,
            self.credentials.access_key_secret,
            self.credentials.security_token,
        )
        bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        bucket.put_object(image_key, content, headers={"Content-Type": "image/png"})
        return image_key

    def build_image_key(self, block_id: str) -> str:
        suffix = self.request_id or str(int(time.time() * 1000))
        safe_suffix = suffix.replace("/", "_").replace(":", "_")
        stem = block_id.replace("/", "_")
        filename = f"{stem}_{safe_suffix}.png"
        if not self.base_path:
            return filename
        return f"{self.base_path}/{filename}"

    def build_image_url(self, image_key: str) -> str:
        """Build the full OSS URL for an image key.

        OSS URL format (virtual-hosted style): https://<bucket>.<endpoint>/<key>
        """
        return f"https://{self.bucket_name}.{self.endpoint}/{image_key}"


def encode_png(image: Any) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def decrypt_oss_credentials(encrypted: EncryptedCredentials) -> OssCredentials:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
    except ImportError as exc:
        raise ImageUploadError("cryptography is not installed") from exc

    key = load_oss_encrypt_key()
    iv = decode_base64_field(encrypted.iv, "iv")
    ciphertext = decode_base64_field(encrypted.ciphertext, "ciphertext")
    if len(iv) != 16:
        raise ImageUploadError("encrypted credentials iv must be 16 bytes")

    decryptor = Cipher(
        algorithms.AES256(key), modes.CBC(iv)
    ).decryptor()

    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()

    payload = json.loads(plaintext.decode("utf-8"))
    return OssCredentials(
        access_key_id=require_string(payload, "accessKeyId"),
        access_key_secret=require_string(payload, "accessKeySecret"),
        security_token=require_string(payload, "securityToken"),
        expiration=payload.get("expiration"),
    )


def load_oss_encrypt_key() -> bytes:
    configured = settings.oss_encrypt_key.strip()
    if not configured:
        raise ImageUploadError("OSS encryption key is not configured")

    try:
        decoded = base64.b64decode(configured, validate=True)
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass

    raw = configured.encode("utf-8")
    if len(raw) != 32:
        raise ImageUploadError(
            "OSS encryption key must be 32 bytes or base64-encoded 32 bytes"
        )
    return raw


def decode_base64_field(value: str, field_name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ImageUploadError(
            f"encrypted credentials {field_name} is not valid base64"
        ) from exc


def require_string(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ImageUploadError(f"encrypted credentials missing {field_name}")
    return value


def add_warning(
    warnings: list[ParseWarning],
    *,
    code: str,
    message: str,
    block_id: str | None = None,
) -> None:
    warning = ParseWarning(code=code, message=message, block_id=block_id)
    if warning not in warnings:
        warnings.append(warning)


def sanitize_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message[:200]


def resolve_block_type(item: Any) -> str:
    ref = getattr(item, "self_ref", "")
    if ref.startswith("#/groups/"):
        return "group"
    return enum_value(getattr(item, "label", None)) or ref.split("/")[1].rstrip("s")


def resolve_page_no(document: Any, item: Any) -> int | None:
    prov = getattr(item, "prov", None)
    if prov:
        return getattr(prov[0], "page_no", None)

    for child_ref in getattr(item, "children", []) or []:
        child = resolve_ref(document, child_ref)
        if child is None:
            continue
        page_no = resolve_page_no(document, child)
        if page_no is not None:
            return page_no
    return None


def resolve_bbox(item: Any) -> dict[str, Any] | None:
    prov = getattr(item, "prov", None)
    if not prov:
        return None
    bbox = getattr(prov[0], "bbox", None)
    if bbox is None:
        return None
    if hasattr(bbox, "model_dump"):
        return bbox.model_dump(mode="json")
    if isinstance(bbox, dict):
        return bbox
    return None


def resolve_parent_ref(item: Any) -> str | None:
    parent = getattr(item, "parent", None)
    return getattr(parent, "cref", None)


def resolve_child_refs(item: Any) -> list[str]:
    return [
        ref
        for child in getattr(item, "children", []) or []
        if (ref := getattr(child, "cref", None))
    ]


def collect_child_text(document: Any, item: Any) -> list[str]:
    texts: list[str] = []
    seen_refs: set[str] = set()
    for ref_item in [
        *(getattr(item, "captions", []) or []),
        *(getattr(item, "children", []) or []),
    ]:
        collect_ref_text(document, ref_item, texts, seen_refs)
    return texts


def collect_ref_text(
    document: Any,
    ref_item: Any,
    texts: list[str],
    seen_refs: set[str],
) -> None:
    ref = getattr(ref_item, "cref", None)
    if not ref or ref in seen_refs:
        return
    seen_refs.add(ref)

    item = resolve_ref(document, ref_item)
    if item is None:
        return

    text = getattr(item, "text", None)
    if text:
        texts.append(text)
    for child in getattr(item, "children", []) or []:
        collect_ref_text(document, child, texts, seen_refs)


def resolve_ref(document: Any, ref_item: Any) -> Any | None:
    try:
        return ref_item.resolve(document)
    except Exception:
        return None


def ref_to_block_id(ref: str) -> str:
    return ref.removeprefix("#/")


def enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", str(value))


def add_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def build_ocr_options(
    ocr_options_by_engine: dict[str, type],
    engine: str,
) -> object:
    options_class = ocr_options_by_engine.get(engine)
    if options_class is None:
        raise DoclingParseError(f"unsupported OCR engine: {engine}")

    lang = resolve_ocr_languages(engine, explicit=True)
    options = options_class(lang=lang)
    options.force_full_page_ocr = settings.force_full_page_ocr
    if hasattr(options, "use_gpu"):
        options.use_gpu = settings.device.strip().lower() not in {"cpu", ""}
    return options


def preload_docling_models() -> None:
    global _preloaded_artifacts_path

    try:
        configure_torch_device()
        components = load_docling_components()
        ocr_options_by_engine = load_ocr_option_classes()
        default_options = ParseOptions()
        _preloaded_artifacts_path = prefetch_docling_model_artifacts(default_options)
        get_document_converter(
            default_options,
            components,
            ocr_options_by_engine,
            ocr=False,
            ocr_engine=first_configured_ocr_engine(),
        )
        if settings.preload_ocr_models:
            for engine in configured_ocr_engines():
                get_document_converter(
                    default_options,
                    components,
                    ocr_options_by_engine,
                    ocr=True,
                    ocr_engine=engine,
                )
        _log.info("Docling model preload completed")
    except ImportError as exc:
        raise DoclingParseError("docling is not installed") from exc


def prefetch_docling_model_artifacts(default_options: ParseOptions) -> Path:
    from docling.datamodel.settings import settings as docling_settings
    from docling.utils.model_downloader import download_models

    engines = set(configured_ocr_engines()) if settings.preload_ocr_models else set()
    return download_models(
        output_dir=docling_settings.artifacts_path,
        progress=False,
        with_layout=True,
        with_tableformer=default_options.table_structure,
        with_tableformer_v2=False,
        with_code_formula=False,
        with_picture_classifier=False,
        with_smolvlm=False,
        with_granitedocling=False,
        with_granitedocling_mlx=False,
        with_granitedocling_2stage=False,
        with_smoldocling=False,
        with_smoldocling_mlx=False,
        with_granite_vision=False,
        with_granite_chart_extraction=False,
        with_granite_chart_extraction_v4=False,
        with_rapidocr="rapidocr" in engines,
        with_easyocr="easyocr" in engines,
    )


def load_docling_components() -> dict[str, Any]:
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return {
        "AcceleratorDevice": AcceleratorDevice,
        "AcceleratorOptions": AcceleratorOptions,
        "DocumentConverter": DocumentConverter,
        "InputFormat": InputFormat,
        "PdfFormatOption": PdfFormatOption,
        "PdfPipelineOptions": PdfPipelineOptions,
    }


def load_ocr_option_classes() -> dict[str, type]:
    from docling.datamodel.pipeline_options import (
        EasyOcrOptions,
        OcrMacOptions,
        RapidOcrOptions,
        TesseractOcrOptions,
    )

    return {
        "easyocr": EasyOcrOptions,
        "ocrmac": OcrMacOptions,
        "rapidocr": RapidOcrOptions,
        "tesseract": TesseractOcrOptions,
    }


def get_document_converter(
    options: ParseOptions,
    components: dict[str, Any],
    ocr_options_by_engine: dict[str, type],
    *,
    ocr: bool,
    ocr_engine: str | None,
) -> Any:
    cache_key = (ocr, ocr_engine if ocr else None, options.table_structure)
    with _converter_lock:
        converter = _converters.get(cache_key)
        if converter is None:
            converter = build_document_converter(
                options,
                components,
                ocr_options_by_engine,
                ocr=ocr,
                ocr_engine=ocr_engine,
            )
            converter.initialize_pipeline(components["InputFormat"].PDF)
            _converters[cache_key] = converter
        return converter


def build_document_converter(
    options: ParseOptions,
    components: dict[str, Any],
    ocr_options_by_engine: dict[str, type],
    *,
    ocr: bool,
    ocr_engine: str | None,
) -> Any:
    pipeline_options = components["PdfPipelineOptions"]()
    if _preloaded_artifacts_path is not None:
        pipeline_options.artifacts_path = _preloaded_artifacts_path
    pipeline_options.accelerator_options = components["AcceleratorOptions"](
        num_threads=4,
        device=resolve_accelerator_device(components["AcceleratorDevice"]),
    )
    pipeline_options.do_ocr = ocr
    pipeline_options.force_backend_text = not ocr
    if ocr:
        if ocr_engine is None:
            raise DoclingParseError("OCR engine is required when OCR is enabled")
        pipeline_options.ocr_options = build_ocr_options(
            ocr_options_by_engine,
            ocr_engine,
        )
    pipeline_options.do_table_structure = options.table_structure
    pipeline_options.do_picture_classification = False
    pipeline_options.do_picture_description = False
    pipeline_options.do_code_enrichment = False
    pipeline_options.do_formula_enrichment = False
    pipeline_options.generate_page_images = True
    pipeline_options.generate_picture_images = False
    pipeline_options.generate_table_images = False

    return components["DocumentConverter"](
        format_options={
            components["InputFormat"].PDF: components["PdfFormatOption"](
                pipeline_options=pipeline_options
            ),
        }
    )


def configured_ocr_engines() -> list[str]:
    engines = [
        item.strip().lower()
        for item in settings.ocr_engines.split(",")
        if item.strip()
    ]
    if engines:
        return engines
    if platform.system() == "Darwin":
        return ["ocrmac", "rapidocr"]
    return ["rapidocr"]


def first_configured_ocr_engine() -> str | None:
    engines = configured_ocr_engines()
    return engines[0] if engines else None


def resolve_ocr_languages(engine: str, *, explicit: bool) -> list[str]:
    configured = [item.strip() for item in settings.ocr_lang.split(",") if item.strip()]
    if configured and (explicit or settings.ocr_lang != "chinese"):
        if engine == "ocrmac" and configured == ["chinese"]:
            return ["zh-Hans", "en-US"]
        if engine == "easyocr" and configured == ["chinese"]:
            return ["ch_sim", "en"]
        if engine == "tesseract" and configured == ["chinese"]:
            return ["chi_sim", "eng"]
        return configured
    if engine == "ocrmac":
        return ["zh-Hans", "en-US"]
    if engine == "tesseract":
        return ["chi_sim", "eng"]
    if engine == "easyocr":
        return ["ch_sim", "en"]
    return ["chinese"]


def configure_torch_device() -> None:
    if settings.device.strip().lower() == "cpu":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


def resolve_accelerator_device(accelerator_device: type) -> object:
    device = settings.device.strip().lower()
    if device == "mps":
        return accelerator_device.MPS
    if device == "cuda":
        return accelerator_device.CUDA
    if device == "xpu":
        return accelerator_device.XPU
    if device == "auto":
        return accelerator_device.AUTO
    return accelerator_device.CPU


def looks_garbled(text: str) -> bool:
    cleaned = text.replace("<!-- image -->", "").strip()
    if len(cleaned) < 80:
        return False

    sample = cleaned[:4000]
    visible_chars = [ch for ch in sample if not ch.isspace()]
    if not visible_chars:
        return False

    suspicious = sum(1 for ch in visible_chars if is_suspicious_char(ch))
    cjk = sum(1 for ch in visible_chars if "\u4e00" <= ch <= "\u9fff")
    ascii_letters = sum(1 for ch in visible_chars if ch.isascii() and ch.isalpha())
    digits = sum(1 for ch in visible_chars if ch.isdigit())
    normal = cjk + ascii_letters + digits

    suspicious_ratio = suspicious / len(visible_chars)
    normal_ratio = normal / len(visible_chars)
    return suspicious_ratio > 0.2 and normal_ratio < 0.35


def is_suspicious_char(ch: str) -> bool:
    code = ord(ch)
    if code < 32 or code == 127:
        return True
    if 0x80 <= code <= 0x9F:
        return True
    if 0x0100 <= code <= 0x024F:
        return True
    if 0x0370 <= code <= 0x052F:
        return True
    return False


def resolve_suffix(file_name: str | None, source_url: str) -> str:
    candidate = file_name or unquote(Path(urlparse(source_url).path).name)
    suffix = Path(candidate).suffix.lower()
    if suffix and len(suffix) <= 12:
        return suffix
    return ".bin"
