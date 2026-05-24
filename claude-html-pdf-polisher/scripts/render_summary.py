#!/usr/bin/env python3
"""Inspect PDF/page renders and write a concise render summary."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from PIL import Image


class Timing:
    def __init__(self, path: Path | None = None, origin_ms: int | None = None):
        self.t0 = time.perf_counter()
        self.origin_ms = origin_ms
        self.path = path
        self.current: str | None = None
        self.current_start = self._now_ms()

    def _elapsed(self) -> float:
        if self.origin_ms is not None:
            return (self._now_ms() - self.origin_ms) / 1000.0
        return time.perf_counter() - self.t0

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _write(self, line: str) -> None:
        print(line, flush=True)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def start(self, name: str) -> None:
        self.current = name
        self.current_start = self._now_ms()
        self._write(f"[{self._elapsed():07.2f}s] start: {name}")

    def ok(self, name: str | None = None) -> None:
        label = name or self.current or "unknown"
        duration = (self._now_ms() - self.current_start) / 1000.0
        self._write(f"[{self._elapsed():07.2f}s] ok: {label} ({duration:.2f}s)")
        self.current = None


def page_size_label(width_pt: float, height_pt: float) -> str:
    return f"{width_pt / 72:.2f} x {height_pt / 72:.2f} in ({round(width_pt)} x {round(height_pt)} pt)"


def image_ink_percent(path: Path) -> float:
    im = Image.open(path).convert("L")
    hist = im.histogram()
    total = sum(hist) or 1
    ink = sum(hist[:245])
    return 100.0 * ink / total


def inspect_with_fitz(pdf_path: Path) -> tuple[int, list[tuple[float, float]], list[int]]:
    import fitz  # type: ignore
    doc = fitz.open(str(pdf_path))
    page_count = len(doc)
    sizes = [(float(p.rect.width), float(p.rect.height)) for p in doc]
    text_lengths = [len(doc[idx].get_text("text").strip()) for idx in range(page_count)]
    return page_count, sizes, text_lengths


def inspect_with_pdfium(pdf_path: Path) -> tuple[int, list[tuple[float, float]], list[int]]:
    import pypdfium2 as pdfium  # type: ignore
    pdf = pdfium.PdfDocument(str(pdf_path))
    page_count = len(pdf)
    sizes: list[tuple[float, float]] = []
    text_lengths: list[int] = []
    for idx in range(page_count):
        page = pdf[idx]
        w, h = page.get_size()
        sizes.append((float(w), float(h)))
        textpage = page.get_textpage()
        text = textpage.get_text_range() or ""
        text_lengths.append(len(text.strip()))
    return page_count, sizes, text_lengths



def write_render_summary(
    pdf: Path,
    renders: Path,
    contact: Path,
    summary: Path,
    large_contact: Path | None = None,
    timings: Path | None = None,
    timing_origin_ms: int | None = None,
) -> str:
    """Generate a render summary without spawning a subprocess.

    The timing labels intentionally match the CLI path.
    """
    timing = Timing(timings, timing_origin_ms)

    timing.start("pdf_inspect")
    pdf = pdf.resolve()
    render_dir = renders.resolve()
    pages_png = sorted(render_dir.glob("page-*.png"))

    engine = "pymupdf"
    try:
        page_count, sizes, text_lengths = inspect_with_fitz(pdf)
    except ImportError:
        timing._write(f"[{timing._elapsed():07.2f}s] info: pymupdf not available; trying pypdfium2")
        try:
            page_count, sizes, text_lengths = inspect_with_pdfium(pdf)
            engine = "pypdfium2"
        except ImportError as exc:
            raise SystemExit("pdf_inspect requires PyMuPDF/fitz or pypdfium2") from exc

    timing._write(f"[{timing._elapsed():07.2f}s] info: pdf_inspect engine={engine}")
    first_size = sizes[0] if sizes else (0.0, 0.0)
    oversized: list[str] = []
    for idx, (w, h) in enumerate(sizes, start=1):
        if abs(w - first_size[0]) > 1.0 or abs(h - first_size[1]) > 1.0:
            oversized.append(f"page {idx}: {page_size_label(w, h)}")
    timing.ok("pdf_inspect")

    timing.start("analyze_page_images")
    sparse: list[str] = []
    for idx in range(page_count):
        img_path = render_dir / f"page-{idx + 1:02d}.png"
        ink = image_ink_percent(img_path) if img_path.exists() else 0.0
        if text_lengths[idx] < 20 and ink < 0.25:
            sparse.append(f"page {idx + 1}: text_chars={text_lengths[idx]}, ink={ink:.2f}%")
    timing.ok("analyze_page_images")

    timing.start("write_summary")
    lines = [
        "Render summary",
        f"- PDF: {pdf}",
        f"- Pages: {page_count}",
        f"- Page size: {page_size_label(*first_size) if page_count else 'unknown'}",
        f"- Rasterized pages: {len(pages_png)}",
        f"- Blank/sparse pages: {', '.join(sparse) if sparse else 'none detected'}",
        f"- Oversized pages: {'; '.join(oversized) if oversized else 'none detected'}",
        f"- Contact sheet: {contact.resolve()}",
    ]
    if timings:
        lines.append(f"- Timings: {timings.resolve()}")
    if large_contact:
        lines.append(f"- Large contact sheet: {large_contact.resolve()}")

    timing.ok("write_summary")

    timing_lines: list[str] = []
    if timings:
        timing_path = timings.resolve()
        if timing_path.exists():
            raw = timing_path.read_text(encoding="utf-8", errors="replace").splitlines()
            timing_lines = raw[-120:]

    summary_text = "\n".join(lines) + "\n"
    if timing_lines:
        summary_text += "\nTiming log\n" + "\n".join(timing_lines) + "\n"

    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(summary_text, encoding="utf-8")
    print(summary_text.rstrip())
    return summary_text

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a render summary with granular stage timings.")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--renders", required=True, type=Path)
    parser.add_argument("--contact", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--large-contact", type=Path, default=None)
    parser.add_argument("--timings", type=Path, default=None, help="append timings log for this render")
    parser.add_argument("--timing-origin-ms", type=int, default=None, help="epoch-millisecond origin for shared timing logs")
    args = parser.parse_args()

    timing = Timing(args.timings, args.timing_origin_ms)

    timing.start("pdf_inspect")
    pdf = args.pdf.resolve()
    render_dir = args.renders.resolve()
    pages_png = sorted(render_dir.glob("page-*.png"))

    engine = "pymupdf"
    try:
        page_count, sizes, text_lengths = inspect_with_fitz(pdf)
    except ImportError:
        timing._write(f"[{timing._elapsed():07.2f}s] info: pymupdf not available; trying pypdfium2")
        try:
            page_count, sizes, text_lengths = inspect_with_pdfium(pdf)
            engine = "pypdfium2"
        except ImportError as exc:
            raise SystemExit("pdf_inspect requires PyMuPDF/fitz or pypdfium2") from exc

    timing._write(f"[{timing._elapsed():07.2f}s] info: pdf_inspect engine={engine}")
    first_size = sizes[0] if sizes else (0.0, 0.0)
    oversized: list[str] = []
    for idx, (w, h) in enumerate(sizes, start=1):
        if abs(w - first_size[0]) > 1.0 or abs(h - first_size[1]) > 1.0:
            oversized.append(f"page {idx}: {page_size_label(w, h)}")
    timing.ok("pdf_inspect")

    timing.start("analyze_page_images")
    sparse: list[str] = []
    for idx in range(page_count):
        img_path = render_dir / f"page-{idx + 1:02d}.png"
        ink = image_ink_percent(img_path) if img_path.exists() else 0.0
        if text_lengths[idx] < 20 and ink < 0.25:
            sparse.append(f"page {idx + 1}: text_chars={text_lengths[idx]}, ink={ink:.2f}%")
    timing.ok("analyze_page_images")

    timing.start("write_summary")
    lines = [
        "Render summary",
        f"- PDF: {pdf}",
        f"- Pages: {page_count}",
        f"- Page size: {page_size_label(*first_size) if page_count else 'unknown'}",
        f"- Rasterized pages: {len(pages_png)}",
        f"- Blank/sparse pages: {', '.join(sparse) if sparse else 'none detected'}",
        f"- Oversized pages: {'; '.join(oversized) if oversized else 'none detected'}",
        f"- Contact sheet: {args.contact.resolve()}",
    ]
    if args.timings:
        lines.append(f"- Timings: {args.timings.resolve()}")
    if args.large_contact:
        lines.append(f"- Large contact sheet: {args.large_contact.resolve()}")

    # Mark the tiny summary stage complete before embedding the timing tail so
    # render-summary.txt contains the write_summary ok line too.
    timing.ok("write_summary")

    timing_lines: list[str] = []
    if args.timings:
        timing_path = args.timings.resolve()
        if timing_path.exists():
            raw = timing_path.read_text(encoding="utf-8", errors="replace").splitlines()
            timing_lines = raw[-120:]

    summary_text = "\n".join(lines) + "\n"
    if timing_lines:
        summary_text += "\nTiming log\n" + "\n".join(timing_lines) + "\n"

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(summary_text, encoding="utf-8")
    print(summary_text.rstrip())
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
