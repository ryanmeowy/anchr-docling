import logging
import os
import platform
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from anchr_docling.config import settings
from anchr_docling.schemas import (
    OutputFormat,
    ParsedPage,
    ParseOptions,
    ParseRequest,
    ParseResponse,
)


class SourceDownloadError(RuntimeError):
    pass


class DoclingParseError(RuntimeError):
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
        document: dict[str, Any] | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.text = text
        self.pages = pages
        self.quality_text = quality_text
        self.document = document
        self.blocks = blocks


class DoclingParser:
    def preload(self) -> None:
        preload_docling_models()

    def parse(self, request: ParseRequest) -> ParseResponse:
        suffix = resolve_suffix(request.file_name, str(request.source_url))
        with tempfile.TemporaryDirectory(prefix="anchr-docling-") as tmp_dir:
            source_path = Path(tmp_dir) / f"source{suffix}"
            download_source(str(request.source_url), source_path)
            parsed = convert_document(source_path, request.options)

        return ParseResponse(
            requestId=request.request_id,
            parser="docling",
            format=request.options.output_format,
            text=parsed.text,
            pages=parsed.pages,
            document=parsed.document,
            blocks=parsed.blocks,
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


def convert_document(source_path: Path, options: ParseOptions) -> ParsedDocument:
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
) -> ParsedDocument:
    converter = get_document_converter(
        options,
        components,
        ocr_options_by_engine,
        ocr=ocr,
        ocr_engine=ocr_engine,
    )
    result = converter.convert(source_path)
    return export_parsed_document(result.document, options.output_format)


def run_ocr_fallback_chain(
    source_path: Path,
    options: ParseOptions,
    components: dict[str, Any],
    ocr_options_by_engine: dict[str, type],
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


def export_parsed_document(document: Any, output_format: OutputFormat) -> ParsedDocument:
    json_document = document.export_to_dict() if output_format == "json" else None
    blocks = export_blocks(document) if output_format == "blocks" else None
    page_block_refs = collect_page_block_refs(document) if output_format in {"json", "blocks"} else {}
    text = export_document_content(document, output_format)
    pages = [
        ParsedPage(
            pageNo=page_no,
            text=export_page_content(
                document,
                output_format,
                page_no,
            ),
            blockRefs=page_block_refs.get(page_no),
        )
        for page_no in sorted(document.pages)
    ]
    return ParsedDocument(
        text=text,
        pages=pages,
        quality_text=document.export_to_text(),
        document=json_document,
        blocks=blocks,
    )


def export_document_content(
    document: Any,
    output_format: OutputFormat,
) -> str:
    if output_format == "markdown":
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
    if output_format == "markdown":
        return document.export_to_markdown(page_no=page_no).strip()
    if output_format == "html":
        return document.export_to_html(page_no=page_no).strip()
    if output_format in {"text", "json", "blocks"}:
        return document.export_to_text(page_no=page_no, traverse_pictures=True).strip()
    raise DoclingParseError(f"unsupported output format: {output_format}")


def export_blocks(document: Any) -> list[dict[str, Any]]:
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
            block["imageKey"] = None

        blocks.append(block)
    return blocks


def collect_page_block_refs(document: Any) -> dict[int, list[str]]:
    refs_by_page: dict[int, list[str]] = {}
    for item, _ in document.iterate_items(with_groups=True, traverse_pictures=True):
        ref = getattr(item, "self_ref", None)
        if not ref or ref == "#/body":
            continue

        page_no = resolve_page_no(document, item)
        if page_no is None:
            continue

        refs = refs_by_page.setdefault(page_no, [])
        if ref not in refs:
            refs.append(ref)
    return refs_by_page


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
    pipeline_options.generate_page_images = False
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
