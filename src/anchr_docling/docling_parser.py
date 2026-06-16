import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from anchr_docling.config import settings
from anchr_docling.blocks import export_blocks
from anchr_docling.chunking import (
    export_markdown_chunks,
    export_native_chunks,
)
from anchr_docling.errors import (
    DoclingParseError,
    ImageUploadError,
    SourceDownloadError,
    add_warning,
)
from anchr_docling.images import (
    ImageUploadContext,
    OssCredentials,
    _validate_image_size,
    build_image_upload_context,
    enrich_markdown_text,
)
from anchr_docling.schemas import (
    OssUploadOptions,
    OutputFormat,
    ParsedPage,
    ParseOptions,
    ParseRequest,
    ParseResponse,
    ParseWarning,
)
from anchr_docling.setup import (
    build_document_converter,
    configured_ocr_engines,
    configure_torch_device,
    first_configured_ocr_engine,
    get_document_converter,
    load_docling_components,
    load_ocr_option_classes,
    looks_garbled,
    preload_docling_models,
    resolve_input_format,
    resolve_suffix,
)


class ParsedDocument:
    def __init__(
        self,
        text: str,
        pages: list[ParsedPage],
        quality_text: str,
        document: dict[str, Any] | None = None,
        blocks: list[dict[str, Any]] | None = None,
        chunks: list[dict[str, Any]] | None = None,
        images: list[dict[str, Any]] | None = None,
        warnings: list[ParseWarning] | None = None,
    ) -> None:
        self.text = text
        self.pages = pages
        self.quality_text = quality_text
        self.document = document
        self.blocks = blocks
        self.chunks = chunks
        self.images = images
        self.warnings = warnings or []


class DoclingParser:
    def preload(self) -> None:
        preload_docling_models()

    def parse(self, request: ParseRequest) -> ParseResponse:
        suffix = resolve_suffix(request.file_name, str(request.source_url))
        with tempfile.TemporaryDirectory(prefix="anchr-docling-") as tmp_dir:
            source_path = Path(tmp_dir) / f"source{suffix}"
            download_source(str(request.source_url), source_path)
            _validate_image_size(source_path)
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

    input_format = resolve_input_format(source_path.suffix.lower())

    # Plain text / Markdown: build a minimal DoclingDocument manually.
    # Only chunks output is supported for these formats.
    is_md = source_path.suffix.lower() == ".md"
    if input_format is None or is_md:
        if options.output_format != "chunks":
            suffix = source_path.suffix.lower()
            raise DoclingParseError(
                f"outputFormat must be 'chunks' for {suffix} input, "
                f"got '{options.output_format}'"
            )

        text_content = source_path.read_text(encoding="utf-8")

        # Extract image references from markdown before Docling parses it.
        md_images = _extract_md_images(text_content) if is_md else []

        try:
            from docling_core.types.doc import DoclingDocument
            from docling_core.types.doc.labels import DocItemLabel
        except ImportError as exc:
            raise DoclingParseError("docling is not installed") from exc

        if is_md:
            # Use Docling's MD converter for proper structure (headings, etc.).
            components = load_docling_components()
            converter = get_document_converter(
                options, components, {}, ocr=False, ocr_engine=None,
                input_format=resolve_input_format(".md"),
            )
            result = converter.convert(source_path)
            doc = result.document
        else:
            doc = DoclingDocument(name=source_path.name)
            for paragraph in text_content.split("\n\n"):
                stripped = paragraph.strip()
                if stripped:
                    doc.add_text(text=stripped, label=DocItemLabel.TEXT)

        return export_parsed_document(
            doc, options, oss_options, request_id=request_id,
            md_images=md_images,
        )

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
            input_format=input_format,
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
                input_format=input_format,
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


_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _extract_md_images(text: str) -> list[dict[str, str]]:
    """Extract ``![alt](url)`` references from markdown text."""
    return [
        {"alt": m.group(1), "url": m.group(2)}
        for m in _MD_IMAGE_RE.finditer(text)
    ]


def _restore_md_images(
    text: str,
    md_images: list[dict[str, str]],
    uploader: "OssImageUploader | None",
    warnings: list[ParseWarning],
    uploaded_cache: dict[str, str],
) -> str:
    """Replace ``<!-- image -->`` placeholders with markdown image syntax.

    Tries OSS upload first; falls back to the original URL.
    ``uploaded_cache`` maps original URL → OSS URL to avoid re-uploading.
    """
    placeholder = "<!-- image -->"
    if placeholder not in text:
        return text

    index = 0
    while placeholder in text and index < len(md_images):
        info = md_images[index]
        alt = info["alt"]
        original_url = info["url"]

        # Try OSS upload.
        final_url: str | None = None
        if uploader is not None and original_url in uploaded_cache:
            final_url = uploaded_cache[original_url]
        elif uploader is not None:
            try:
                image = _download_md_image(original_url)
                if image is not None:
                    image_key = uploader.upload_png(
                        f"md_image_{index}", image
                    )
                    final_url = uploader.build_image_url(image_key)
                    uploaded_cache[original_url] = final_url
                else:
                    uploaded_cache[original_url] = ""
            except Exception:
                uploaded_cache[original_url] = ""

        if final_url is None:
            final_url = original_url

        text = text.replace(placeholder, f"![{alt}]({final_url})", 1)
        index += 1

    return text


def _download_md_image(url: str) -> Any | None:
    """Download an image from a URL.  Returns a PIL Image or ``None``."""
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return None

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content))
    except Exception:
        return None


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
    input_format: Any | None = None,
) -> ParsedDocument:
    converter = get_document_converter(
        options,
        components,
        ocr_options_by_engine,
        ocr=ocr,
        ocr_engine=ocr_engine,
        input_format=input_format,
    )
    result = converter.convert(source_path)
    return export_parsed_document(
        result.document,
        options,
        oss_options,
        request_id=request_id,
    )


def run_ocr_fallback_chain(
    source_path: Path,
    options: ParseOptions,
    components: dict[str, Any],
    ocr_options_by_engine: dict[str, type],
    oss_options: OssUploadOptions | None,
    request_id: str | None = None,
    input_format: Any | None = None,
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
                input_format=input_format,
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
    request_id: str | None = None,
    md_images: list[dict[str, str]] | None = None,
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

    if output_format == "chunks":
        if options.use_native_chunker:
            chunks = export_native_chunks(document, options, warnings)
        else:
            chunks = export_markdown_chunks(text, pages, options, warnings)
    else:
        chunks = None

    # Enrich chunk text with OSS image URLs (from Docling pipeline, e.g. PDF).
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

    # Restore markdown-source image references (md/txt input).
    # Tries OSS upload first, falls back to original URL.
    if md_images and chunks:
        uploader = upload_context.uploader if upload_context is not None else None
        uploaded_cache: dict[str, str] = {}
        for chunk in chunks:
            chunk["text"] = _restore_md_images(
                chunk["text"], md_images, uploader, warnings, uploaded_cache,
            )

    return ParsedDocument(
        text=text,
        pages=pages,
        quality_text=document.export_to_text(),
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
