#!/usr/bin/python3
"""Probe actual PDF search, area extraction and horizontal drag selection.

Run with the system Python, separately from the pinned Manim environment.
The authoring tests require the system packages python3-gi and
gir1.2-poppler-0.18; missing readers are an error, never a skipped check.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, nargs="?")
    parser.add_argument("phrases", nargs="*")
    parser.add_argument("--page", type=int, default=1, help="One-based PDF page (default: 1)")
    parser.add_argument("--version", action="store_true", help="Check the required Poppler reader")
    args = parser.parse_args()
    if not args.version and (args.pdf is None or not args.phrases):
        parser.error("provide a PDF and at least one phrase, or --version")
    try:
        import gi
        gi.require_version("Poppler", "0.18")
        from gi.repository import Poppler
    except (ImportError, ValueError) as error:
        raise SystemExit(
            "Required Poppler selection reader is unavailable. Install python3-gi "
            "and gir1.2-poppler-0.18, then run this probe with /usr/bin/python3."
        ) from error

    reader = {"name": "Poppler", "version": Poppler.get_version()}
    if args.version:
        print(json.dumps({"reader": reader}))
        return

    document = Poppler.Document.new_from_file(args.pdf.resolve(strict=True).as_uri(), None)
    if not 1 <= args.page <= document.get_n_pages():
        parser.error("--page is outside the PDF")
    page = document.get_page(args.page - 1)
    width, height = page.get_size()

    def rectangle(values):
        result = Poppler.Rectangle()
        result.x1, result.y1, result.x2, result.y2 = values
        return result

    result = {"reader": reader, "page": args.page, "page_size": [width, height], "phrases": []}
    for phrase in args.phrases:
        matches = []
        for found in page.find_text(phrase):
            # find_text reports PDF coordinates. The area and selection APIs
            # use page coordinates with a top-left origin. Selection rectangles
            # contain drag endpoints, so test a same-line drag independently.
            area = [found.x1, height - found.y2, found.x2, height - found.y1]
            if (not all(math.isfinite(value) for value in area) or
                    area[2] <= area[0] or area[3] <= area[1]):
                raise ValueError(f"Poppler returned invalid search bounds for {phrase!r}: {area!r}")
            middle = (area[1] + area[3]) / 2
            drag = [area[0], middle, area[2], middle]
            matches.append({
                "find_rect_pdf": [found.x1, found.y1, found.x2, found.y2],
                "area_rect": area,
                "area_text": page.get_text_for_area(rectangle(area)),
                "drag_endpoints": drag,
                "drag_text": page.get_selected_text(Poppler.SelectionStyle.GLYPH, rectangle(drag)),
                "corner_drag_diagnostic": page.get_selected_text(
                    Poppler.SelectionStyle.GLYPH, rectangle(area)),
            })
        result["phrases"].append({"phrase": phrase, "matches": matches})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
