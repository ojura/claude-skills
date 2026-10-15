#!/usr/bin/env python3
"""Write or compare the showcase of the skill: docs/sample-report.pdf and the previews.

    sample.py write   --pdf REPORT.pdf --docs docs
    sample.py compare --pdf REPORT.pdf --docs docs

The sample is a copy of the fixture's PDF. The previews are three of its pages
rendered at PREVIEW_DPI, chosen by content so that they follow the fixture when
its pagination changes:

    01-title-toc.png        page 1, the title with the contents
    02-code-wrapping.png    the first page with a code listing that wraps a line
                            (the continuation arrow); without one, the first
                            page with a listing of ten lines or more
    03-chart-and-table.png  the first page that holds a figure and a table

`compare` checks the committed files against a fresh build: the page count,
the text of every page, the pages rendered at CHECK_DPI, and the previews. It
exits 1 with the message STALE when they differ. `make sample` runs `write`,
`make check` on the fixture runs `compare`, and hooks/pre-commit runs it when
a staged path touches the pipeline.
"""
import argparse
import os
import re
import shutil
import sys

import numpy as np
import pymupdf

PREVIEWS = ["01-title-toc.png", "02-code-wrapping.png", "03-chart-and-table.png"]
PREVIEW_DPI = 220
CHECK_DPI = 60
STALE = "sample report is stale: run make sample"
# A page differs when more than DIFF_FRACTION of its pixels differ by more
# than DIFF_LEVELS in some channel; small enough to see a changed word, large
# enough to ignore anti-aliasing noise.
DIFF_LEVELS = 32
DIFF_FRACTION = 0.0005


def page_lines(page):
    """The text lines of a page as (text, mono, bbox); mono when every span is in a monospace font."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in l["spans"])
            mono = all("Mono" in s["font"] or (s["flags"] & 8) for s in spans)
            out.append((text, mono, l["bbox"]))
    return out


def listing_lines(lines):
    """Mono lines with no other text beside them: the lines of a code block, not the code cells of a table."""
    def beside(a, b):
        top, bottom = max(a[1], b[1]), min(a[3], b[3])
        return bottom - top > 0.5 * (a[3] - a[1])
    prose = [bb for _, m, bb in lines if not m]
    return sum(1 for _, m, bb in lines if m and not any(beside(bb, o) for o in prose))


def select_pages(doc):
    """Page index (0-based) of each preview, chosen by content."""
    code = wrapped = chart = None
    for i, page in enumerate(doc):
        lines = page_lines(page)
        listing = listing_lines(lines)
        if code is None and listing >= 10:
            code = i
        if wrapped is None and listing >= 10 and any("\u21aa" in t for t, _, _ in lines):
            wrapped = i
        has_figure = any(re.match(r"Figure \d", t) for t, _, _ in lines)
        has_table = any(re.match(r"Table \d", t) for t, _, _ in lines)
        if chart is None and has_figure and has_table:
            chart = i
        if wrapped is not None and chart is not None:
            break
    if wrapped is not None:
        code = wrapped
    missing = [n for n, v in (("a code listing", code), ("a figure and a table", chart)) if v is None]
    if missing:
        sys.exit("no page with %s in the PDF; the preview rules in tools/sample.py need the fixture" % " or ".join(missing))
    return dict(zip(PREVIEWS, [0, code, chart]))


def render(page, dpi):
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]


def load_png(path):
    pix = pymupdf.Pixmap(path)
    if pix.n not in (3, 4):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
    return arr, (pix.xres, pix.yres)


def differ(a, b):
    """True when two images differ in size or in more than DIFF_FRACTION of their pixels."""
    if a.shape != b.shape:
        return True
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    return (d > DIFF_LEVELS).mean() > DIFF_FRACTION


def write(pdf, docs):
    doc = pymupdf.open(pdf)
    pages = select_pages(doc)
    os.makedirs(os.path.join(docs, "preview"), exist_ok=True)
    target = os.path.join(docs, "sample-report.pdf")
    shutil.copyfile(pdf, target)
    print("wrote %s: %d pages, %d bytes" % (target, len(doc), os.path.getsize(target)))
    for name, i in pages.items():
        pix = doc[i].get_pixmap(dpi=PREVIEW_DPI, colorspace=pymupdf.csRGB, alpha=False)
        pix.set_dpi(PREVIEW_DPI, PREVIEW_DPI)
        out = os.path.join(docs, "preview", name)
        pix.save(out)
        print("wrote %s: page %d, %dx%d px at %d dpi, %d bytes" % (out, i + 1, pix.w, pix.h, PREVIEW_DPI, os.path.getsize(out)))


def compare(pdf, docs):
    fresh = pymupdf.open(pdf)
    problems = []
    sample_path = os.path.join(docs, "sample-report.pdf")
    if not os.path.exists(sample_path):
        problems.append("%s is missing" % sample_path)
    else:
        sample = pymupdf.open(sample_path)
        if len(sample) != len(fresh):
            problems.append("page count: the sample has %d pages, the fresh build %d" % (len(sample), len(fresh)))
        text_pages, render_pages = [], []
        for i in range(min(len(sample), len(fresh))):
            if sample[i].get_text("text") != fresh[i].get_text("text"):
                text_pages.append(i + 1)
            if differ(render(sample[i], CHECK_DPI), render(fresh[i], CHECK_DPI)):
                render_pages.append(i + 1)
        if text_pages:
            problems.append("text differs on %d page(s): %s" % (len(text_pages), ", ".join(map(str, text_pages[:20])) + (" ..." if len(text_pages) > 20 else "")))
        if render_pages:
            problems.append("the pages rendered at %d dpi differ on %d page(s): %s" % (CHECK_DPI, len(render_pages), ", ".join(map(str, render_pages[:20])) + (" ..." if len(render_pages) > 20 else "")))
    pages = select_pages(fresh)
    for name, i in pages.items():
        p = os.path.join(docs, "preview", name)
        if not os.path.exists(p):
            problems.append("%s is missing" % p)
            continue
        committed, dpi = load_png(p)
        if dpi != (PREVIEW_DPI, PREVIEW_DPI):
            problems.append("%s is at %s dpi, not %d" % (p, dpi, PREVIEW_DPI))
        if differ(committed, render(fresh[i], PREVIEW_DPI)):
            problems.append("%s differs from page %d of the fresh build" % (p, i + 1))
    if problems:
        print(STALE)
        for p in problems:
            print("  - " + p)
        sys.exit(1)
    print("the sample report and the previews match the fresh build (%d pages; previews from pages %s)"
          % (len(fresh), ", ".join(str(i + 1) for i in pages.values())))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["write", "compare"])
    ap.add_argument("--pdf", required=True, help="the fresh build of the fixture")
    ap.add_argument("--docs", default="docs", help="the showcase directory (default docs)")
    a = ap.parse_args()
    (write if a.mode == "write" else compare)(a.pdf, a.docs)


if __name__ == "__main__":
    main()
