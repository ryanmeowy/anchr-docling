"""Render PDF pages with Docling bbox overlays.

Examples:
    python scripts/verify_pdf_bbox.py --pdf input.pdf --json chunk.json

    python scripts/verify_pdf_bbox.py --pdf input.pdf --json response.json \\
        --out-dir tmp/bbox_verify --dpi 144

    python scripts/verify_pdf_bbox.py --image page-1.png --json chunk.json \\
        --page-no 1 --dpi 144

The JSON input can be a single chunk, a list of chunks, or a response object
containing nested chunk-like objects. The script looks for entries shaped like:

    {"pageNo": 1, "bbox": {"l": 44, "t": 227, "r": 259, "b": 210}}

Docling PDF bboxes usually use point units with a bottom-left origin:

    x1 = l * dpi / 72
    y1 = (page_height_pt - t) * dpi / 72
    x2 = r * dpi / 72
    y2 = (page_height_pt - b) * dpi / 72
"""

from __future__ import annotations

import argparse
import io
import json
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_COLORS = (
    "red",
    "lime",
    "deepskyblue",
    "yellow",
    "magenta",
    "orange",
    "cyan",
    "white",
)


@dataclass(frozen=True)
class BBoxRecord:
    page_no: int
    bbox: dict[str, Any]
    label: str


@dataclass(frozen=True)
class PageImage:
    image: Any
    width_pt: float
    height_pt: float


def die(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"JSON file not found: {path}")
    except json.JSONDecodeError as exc:
        die(f"Invalid JSON in {path}: {exc}")


def parse_bbox_text(value: str) -> dict[str, float | str]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        die(f"bbox must be 'l,t,r,b', got: {value}")
    try:
        l, t, r, b = (float(part) for part in parts)
    except ValueError:
        die(f"bbox contains a non-number value: {value}")
    return {"l": l, "t": t, "r": r, "b": b, "coord_origin": "BOTTOMLEFT"}


def bbox_has_edges(value: Any) -> bool:
    return isinstance(value, dict) and all(key in value for key in ("l", "t", "r", "b"))


def short_label(node: dict[str, Any], fallback: str) -> str:
    chunk_id = node.get("chunkId") or node.get("blockId") or node.get("id")
    node_type = node.get("type")
    if chunk_id and node_type:
        return f"{chunk_id} ({node_type})"
    if chunk_id:
        return str(chunk_id)
    if node_type:
        return str(node_type)
    return fallback


def collect_bbox_records(data: Any) -> list[BBoxRecord]:
    records: list[BBoxRecord] = []

    def walk(node: Any, inherited_label: str | None = None) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, inherited_label)
            return

        if not isinstance(node, dict):
            return

        label = short_label(node, inherited_label or f"bbox-{len(records)}")

        bboxes = node.get("bboxes")
        if isinstance(bboxes, list):
            for index, item in enumerate(bboxes):
                if not isinstance(item, dict):
                    continue
                bbox = item.get("bbox")
                page_no = item.get("pageNo") or item.get("page_no")
                if bbox_has_edges(bbox) and page_no is not None:
                    records.append(BBoxRecord(int(page_no), bbox, f"{label} #{index + 1}"))

        bbox = node.get("bbox")
        page_no = node.get("pageNo") or node.get("page_no")
        if bbox_has_edges(bbox) and page_no is not None:
            records.append(BBoxRecord(int(page_no), bbox, label))

        for key, value in node.items():
            if key in {"bbox", "bboxes"}:
                continue
            if isinstance(value, (dict, list)):
                walk(value, label)

    walk(data)
    return records


def records_from_manual_bboxes(values: list[str] | None, page_no: int) -> list[BBoxRecord]:
    if not values:
        return []
    return [
        BBoxRecord(page_no=page_no, bbox=parse_bbox_text(value), label=f"manual #{index + 1}")
        for index, value in enumerate(values)
    ]


def records_by_page(records: list[BBoxRecord]) -> dict[int, list[BBoxRecord]]:
    grouped: dict[int, list[BBoxRecord]] = {}
    for record in records:
        grouped.setdefault(record.page_no, []).append(record)
    return grouped


def require_pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        die("Pillow is required. Install with: python -m pip install Pillow")
    return Image, ImageDraw, ImageFont


def load_font(image_font: Any) -> Any:
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return image_font.truetype(path, 14)
        except Exception:
            pass
    return image_font.load_default()


def render_pdf_page_with_fitz(pdf_path: Path, page_no: int, dpi: int) -> PageImage | None:
    try:
        import fitz
    except ImportError:
        return None

    doc = fitz.open(pdf_path)
    if page_no < 1 or page_no > len(doc):
        die(f"pageNo {page_no} is outside PDF page range 1..{len(doc)}")

    page = doc[page_no - 1]
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)

    image, _, _ = require_pillow()
    rendered = image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    return PageImage(rendered, float(page.rect.width), float(page.rect.height))


def render_pdf_page_with_pdfium(pdf_path: Path, page_no: int, dpi: int) -> PageImage | None:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None

    pdf = pdfium.PdfDocument(str(pdf_path))
    if page_no < 1 or page_no > len(pdf):
        die(f"pageNo {page_no} is outside PDF page range 1..{len(pdf)}")

    page = pdf[page_no - 1]
    width_pt, height_pt = page.get_size()
    scale = dpi / 72.0
    bitmap = page.render(scale=scale)
    rendered = bitmap.to_pil().convert("RGB")
    return PageImage(rendered, float(width_pt), float(height_pt))


def page_size_with_pypdf(pdf_path: Path, page_no: int) -> tuple[float, float]:
    try:
        from pypdf import PdfReader
    except ImportError:
        die("PyMuPDF is unavailable and pypdf is required for the pdftoppm fallback.")

    reader = PdfReader(str(pdf_path))
    if page_no < 1 or page_no > len(reader.pages):
        die(f"pageNo {page_no} is outside PDF page range 1..{len(reader.pages)}")
    mediabox = reader.pages[page_no - 1].mediabox
    return float(mediabox.width), float(mediabox.height)


def render_pdf_page_with_pdftoppm(pdf_path: Path, page_no: int, dpi: int) -> PageImage:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        die("Neither PyMuPDF nor pdftoppm is available. Install PyMuPDF or Poppler.")

    width_pt, height_pt = page_size_with_pypdf(pdf_path, page_no)
    image, _, _ = require_pillow()

    with tempfile.TemporaryDirectory(prefix="bbox-render-") as tmp_dir:
        prefix = Path(tmp_dir) / "page"
        command = [
            pdftoppm,
            "-f",
            str(page_no),
            "-l",
            str(page_no),
            "-r",
            str(dpi),
            "-png",
            str(pdf_path),
            str(prefix),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip()
            die(f"pdftoppm failed for page {page_no}: {details}")

        rendered_files = sorted(Path(tmp_dir).glob("page-*.png"))
        if not rendered_files:
            die(f"pdftoppm did not produce an image for page {page_no}")
        rendered = image.open(rendered_files[0]).convert("RGB")
        return PageImage(rendered.copy(), width_pt, height_pt)


def render_pdf_page(pdf_path: Path, page_no: int, dpi: int) -> PageImage:
    rendered = render_pdf_page_with_fitz(pdf_path, page_no, dpi)
    if rendered is not None:
        return rendered
    rendered = render_pdf_page_with_pdfium(pdf_path, page_no, dpi)
    if rendered is not None:
        return rendered
    return render_pdf_page_with_pdftoppm(pdf_path, page_no, dpi)


def load_image_page(image_path: Path, dpi: int, page_height: float | None) -> PageImage:
    image, _, _ = require_pillow()
    if not image_path.exists():
        die(f"image not found: {image_path}")

    rendered = image.open(image_path).convert("RGB")
    scale = dpi / 72.0
    width_pt = rendered.width / scale
    height_pt = page_height if page_height is not None else rendered.height / scale
    return PageImage(rendered, width_pt, height_pt)


def as_float(bbox: dict[str, Any], key: str) -> float:
    try:
        value = float(bbox[key])
    except KeyError:
        die(f"bbox is missing '{key}': {bbox}")
    except (TypeError, ValueError):
        die(f"bbox '{key}' is not numeric: {bbox}")
    if not math.isfinite(value):
        die(f"bbox '{key}' is not finite: {bbox}")
    return value


def bbox_origin(bbox: dict[str, Any]) -> str:
    raw = str(bbox.get("coord_origin") or bbox.get("coordOrigin") or "BOTTOMLEFT")
    return raw.replace("_", "").replace("-", "").upper()


def bbox_to_pixels(
    bbox: dict[str, Any],
    page_width_pt: float,
    page_height_pt: float,
    dpi: int,
) -> tuple[list[float], list[str]]:
    l = as_float(bbox, "l")
    t = as_float(bbox, "t")
    r = as_float(bbox, "r")
    b = as_float(bbox, "b")
    origin = bbox_origin(bbox)
    warnings: list[str] = []

    if l >= r:
        warnings.append(f"l >= r ({l:.2f} >= {r:.2f})")

    if origin == "BOTTOMLEFT":
        if b >= t:
            warnings.append(f"BOTTOMLEFT expects b < t ({b:.2f} < {t:.2f})")
        x1 = l
        x2 = r
        y1 = page_height_pt - t
        y2 = page_height_pt - b
        if min(l, r) < 0 or max(l, r) > page_width_pt:
            warnings.append("x is outside page width")
        if min(b, t) < 0 or max(b, t) > page_height_pt:
            warnings.append("y is outside page height")
    elif origin == "TOPLEFT":
        if t >= b:
            warnings.append(f"TOPLEFT expects t < b ({t:.2f} < {b:.2f})")
        x1 = l
        x2 = r
        y1 = t
        y2 = b
        if min(l, r) < 0 or max(l, r) > page_width_pt:
            warnings.append("x is outside page width")
        if min(t, b) < 0 or max(t, b) > page_height_pt:
            warnings.append("y is outside page height")
    else:
        warnings.append(f"unknown coord_origin '{origin}', treated as BOTTOMLEFT")
        x1 = l
        x2 = r
        y1 = page_height_pt - t
        y2 = page_height_pt - b

    scale = dpi / 72.0
    rect = [x1 * scale, y1 * scale, x2 * scale, y2 * scale]
    left, top, right, bottom = rect
    if left > right:
        left, right = right, left
    if top > bottom:
        top, bottom = bottom, top
    return [left, top, right, bottom], warnings


def draw_label(draw: Any, font: Any, xy: tuple[float, float], text: str, color: str) -> None:
    x, y = xy
    y = max(0, y - 17)
    draw.text((x + 4, y), text, fill=color, font=font)


def draw_records_on_page(
    page: PageImage,
    records: list[BBoxRecord],
    dpi: int,
    show_labels: bool,
) -> tuple[Any, int]:
    _, image_draw, image_font = require_pillow()
    image = page.image.copy()
    draw = image_draw.Draw(image)
    font = load_font(image_font)
    warning_count = 0

    for index, record in enumerate(records):
        color = DEFAULT_COLORS[index % len(DEFAULT_COLORS)]
        rect, warnings = bbox_to_pixels(record.bbox, page.width_pt, page.height_pt, dpi)
        warning_count += len(warnings)

        draw.rectangle(rect, outline=color, width=3)
        if show_labels:
            draw_label(draw, font, (rect[0], rect[1]), str(index + 1), color)

        l = as_float(record.bbox, "l")
        t = as_float(record.bbox, "t")
        r = as_float(record.bbox, "r")
        b = as_float(record.bbox, "b")
        print(f"  [{index + 1}] {record.label}")
        print(
            "      bbox pt: "
            f"l={l:.2f} t={t:.2f} r={r:.2f} b={b:.2f} "
            f"origin={bbox_origin(record.bbox)}"
        )
        print(
            "      rect px: "
            f"x1={rect[0]:.0f} y1={rect[1]:.0f} "
            f"x2={rect[2]:.0f} y2={rect[3]:.0f}"
        )
        for warning in warnings:
            print(f"      warning: {warning}")

    return image, warning_count


def output_path_for_page(out_dir: Path, source: Path, page_no: int) -> Path:
    return out_dir / f"{source.stem}_page-{page_no}_bbox.png"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render PDF/image pages with Docling bbox overlays."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", type=Path, help="Source PDF path.")
    source.add_argument("--image", type=Path, help="Already-rendered page image path.")

    parser.add_argument("--json", type=Path, help="Docling/chunk JSON containing bboxes.")
    parser.add_argument(
        "-b",
        "--bbox",
        action="append",
        help="Manual bbox as l,t,r,b. Can be repeated.",
    )
    parser.add_argument(
        "--page-no",
        type=int,
        default=1,
        help="Page number for --image mode or manual --bbox values. Default: 1.",
    )
    parser.add_argument(
        "--page-height",
        type=float,
        default=None,
        help="PDF page height in points for --image mode. Defaults to image height / scale.",
    )
    parser.add_argument("--dpi", type=int, default=144, help="Render DPI. Default: 144.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("tmp/bbox_verify"),
        help="Output directory. Default: tmp/bbox_verify.",
    )
    parser.add_argument(
        "--no-labels",
        action="store_true",
        help="Do not draw numeric labels beside boxes.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.dpi <= 0:
        die("--dpi must be positive")
    if not args.json and not args.bbox:
        die("provide --json, --bbox, or both")

    records: list[BBoxRecord] = []
    if args.json:
        records.extend(collect_bbox_records(load_json(args.json)))
    records.extend(records_from_manual_bboxes(args.bbox, args.page_no))

    if not records:
        die("no bbox records found")

    if args.pdf and not args.pdf.exists():
        die(f"PDF not found: {args.pdf}")

    grouped = records_by_page(records)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    total_warnings = 0
    for page_no in sorted(grouped):
        page_records = grouped[page_no]
        print(f"\nPage {page_no}: {len(page_records)} bbox(es)")

        if args.pdf:
            page = render_pdf_page(args.pdf, page_no, args.dpi)
            source = args.pdf
        else:
            if page_no != args.page_no:
                print(
                    f"  skipping page {page_no}; --image mode is fixed to page {args.page_no}",
                    file=sys.stderr,
                )
                continue
            page = load_image_page(args.image, args.dpi, args.page_height)
            source = args.image

        print(
            f"  page size: {page.width_pt:.2f}x{page.height_pt:.2f} pt, "
            f"image size: {page.image.width}x{page.image.height} px"
        )
        image, warning_count = draw_records_on_page(
            page=page,
            records=page_records,
            dpi=args.dpi,
            show_labels=not args.no_labels,
        )
        total_warnings += warning_count

        out_path = output_path_for_page(args.out_dir, source, page_no)
        image.save(out_path)
        print(f"  saved: {out_path}")

    if total_warnings:
        print(f"\nDone with {total_warnings} warning(s).")
    else:
        print("\nDone with no bbox coordinate warnings.")


if __name__ == "__main__":
    main()
