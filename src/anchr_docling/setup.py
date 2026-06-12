import logging
import os
import platform
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from anchr_docling.config import settings
from anchr_docling.errors import DoclingParseError
from anchr_docling.schemas import ParseOptions

_log = logging.getLogger(__name__)
_converter_lock = threading.Lock()
_converters: dict[tuple[bool, str | None, bool], Any] = {}
_preloaded_artifacts_path: Path | None = None

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
    input_format: Any | None = None,
) -> Any:
    fmt = input_format if input_format is not None else components["InputFormat"].PDF
    cache_key = (ocr, ocr_engine if ocr else None, options.table_structure, fmt)
    with _converter_lock:
        converter = _converters.get(cache_key)
        if converter is None:
            converter = build_document_converter(
                options,
                components,
                ocr_options_by_engine,
                ocr=ocr,
                ocr_engine=ocr_engine,
                input_format=fmt,
            )
            converter.initialize_pipeline(fmt)
            _converters[cache_key] = converter
        return converter


def build_document_converter(
    options: ParseOptions,
    components: dict[str, Any],
    ocr_options_by_engine: dict[str, type],
    *,
    ocr: bool,
    ocr_engine: str | None,
    input_format: Any | None = None,
) -> Any:
    fmt = input_format if input_format is not None else components["InputFormat"].PDF
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

    # Only PDF / IMAGE need PdfPipelineOptions.  MD, DOCX, HTML etc.
    # work natively without pipeline configuration.
    pdf_formats = {components["InputFormat"].PDF, components["InputFormat"].IMAGE}

    return components["DocumentConverter"](
        format_options={
            fmt: components["PdfFormatOption"](
                pipeline_options=pipeline_options
            ),
        }
        if fmt in pdf_formats
        else {}
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


def resolve_input_format(suffix: str) -> Any:
    """Map a file suffix to a Docling InputFormat."""
    from docling.datamodel.base_models import InputFormat

    image_suffixes = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"}
    if suffix in image_suffixes:
        return InputFormat.IMAGE
    if suffix == ".pdf":
        return InputFormat.PDF
    if suffix == ".md":
        return InputFormat.MD
    if suffix == ".txt":
        return None  # handled manually; skip Docling conversion
    # Fallback: treat unknown formats as PDF (Docling will try its best).
    return InputFormat.PDF
