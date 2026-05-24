#!/usr/bin/env python3
"""Create a contact sheet from page PNG renders for layout proofing."""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw


def make_contact_sheet(render_dir: Path, contact_path: Path, thumb_width: int, cols: int, quality: int) -> int:
    pages = sorted(render_dir.glob("page-*.png"))
    if not pages:
        raise SystemExit(f"no page PNGs found in {render_dir}")

    thumbs: list[tuple[str, Image.Image]] = []
    for page_path in pages:
        im = Image.open(page_path).convert("RGB")
        scale = thumb_width / im.width
        thumb_height = max(1, int(im.height * scale))
        im = im.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
        thumbs.append((page_path.name, im))

    gap = max(16, thumb_width // 18)
    label_h = max(28, thumb_width // 14)
    cell_w = thumb_width
    cell_h = max(im.height for _, im in thumbs) + label_h
    rows = math.ceil(len(thumbs) / cols)
    sheet_w = cols * cell_w + (cols + 1) * gap
    sheet_h = rows * cell_h + (rows + 1) * gap
    sheet = Image.new("RGB", (sheet_w, sheet_h), "white")
    draw = ImageDraw.Draw(sheet)

    for idx, (name, im) in enumerate(thumbs):
        row, col = divmod(idx, cols)
        x = gap + col * (cell_w + gap)
        y = gap + row * (cell_h + gap)
        draw.text((x, y), name, fill=(0, 0, 0))
        sheet.paste(im, (x, y + label_h))

    contact_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(contact_path, quality=quality)
    return len(thumbs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Make a PDF render contact sheet from page PNGs.")
    parser.add_argument("render_dir", type=Path, help="directory containing page-XX.png files")
    parser.add_argument("contact", type=Path, help="output contact sheet path")
    parser.add_argument("--thumb-width", type=int, default=420, help="thumbnail width in pixels")
    parser.add_argument("--cols", type=int, default=3, help="number of columns")
    parser.add_argument("--quality", type=int, default=92, help="JPEG quality")
    args = parser.parse_args()

    if args.thumb_width < 120:
        raise SystemExit("--thumb-width must be at least 120")
    if args.cols < 1:
        raise SystemExit("--cols must be at least 1")
    count = make_contact_sheet(args.render_dir, args.contact, args.thumb_width, args.cols, args.quality)
    print(f"contact sheet {args.contact} ({count} pages, thumb_width={args.thumb_width}, cols={args.cols})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
