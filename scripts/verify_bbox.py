"""Verify Docling bbox accuracy by drawing boxes on the source image.

Usage:
    python scripts/verify_bbox.py -i image.png \\
        -b "100,200,400,500" -b "50,60,300,350" \\
        -l "Figure 1" "Figure 2"

Each bbox is ``left,top,right,bottom`` in Docling coordinates (points).
Add labels with ``-l`` (one per bbox, optional).
"""

import argparse
import sys
from pathlib import Path


def parse_bbox(s: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must be 'l,t,r,b', got: {s}")
    return tuple(float(p) for p in parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Draw bbox overlays on an image")
    parser.add_argument("-i", "--image", required=True, help="Source image path")
    parser.add_argument(
        "-b", "--bbox", action="append", required=True,
        help="Bbox as l,t,r,b (repeat for multiple boxes)",
    )
    parser.add_argument(
        "-l", "--label", action="append", default=None,
        help="Label for each bbox. Repeat for multiple: -l 'A' -l 'B'",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Output image path (default: <input>_bbox.png)",
    )
    parser.add_argument(
        "--dpi", type=int, default=72,
        help="Image DPI for points-to-pixel conversion (default: 72)",
    )
    args = parser.parse_args()

    src = Path(args.image)
    if not src.exists():
        print(f"Error: image not found: {src}", file=sys.stderr)
        sys.exit(1)

    bboxes = [parse_bbox(b) for b in args.bbox]
    labels = args.label or []
    # Pad labels if fewer than bboxes.
    while len(labels) < len(bboxes):
        labels.append("")

    out_path = Path(args.output) if args.output else src.parent / f"{src.stem}_bbox{src.suffix}"

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Error: Pillow is required. Install with: pip install Pillow", file=sys.stderr)
        sys.exit(1)

    with Image.open(src) as img:
        w_px, h_px = img.size
        print(f"Image size: {w_px}x{h_px} pixels @ {args.dpi} DPI")

        draw = ImageDraw.Draw(img)

        colors = ["red", "lime", "blue", "yellow", "cyan", "magenta", "orange", "white"]
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
        except Exception:
            font = ImageFont.load_default()

        print(f"\nDrawing {len(bboxes)} bbox(es)...\n")
        for i, (l, t, r, b) in enumerate(bboxes):
            color = colors[i % len(colors)]
            label = labels[i] if i < len(labels) else ""

            # Docling bbox is in points (1/72 inch), PDF coords (origin bottom-left).
            # Flip Y-axis to image coords (origin top-left), then convert to pixels.
            h_pt = h_px / (args.dpi / 72.0)
            scale = args.dpi / 72.0
            x1 = l * scale
            x2 = r * scale
            y1 = (h_pt - t) * scale  # flip: PDF top -> image top
            y2 = (h_pt - b) * scale  # flip: PDF bottom -> image bottom

            # Ensure correct order.
            if x1 > x2:
                x1, x2 = x2, x1
            if y1 > y2:
                y1, y2 = y2, y1

            draw.rectangle([x1, y1, x2, y2], outline=color, width=3)

            # Print bbox info
            print(f"  [{i}] {label or 'unnamed'}")
            print(f"      bbox (points):  l={l:.0f} t={t:.0f} r={r:.0f} b={b:.0f}")
            print(f"      bbox (pixels):  x1={x1:.0f} y1={y1:.0f} x2={x2:.0f} y2={y2:.0f}")
            print(f"      size:            {abs(r-l):.0f}x{abs(t-b):.0f} pt  →  {x2-x1:.0f}x{y2-y1:.0f} px")

            if label:
                draw.text((x1 + 4, y1 - 16), label, fill=color, font=font)

        img.save(out_path)
        print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
