#!/usr/bin/env python3
"""Single-process render pipeline for Claude HTML PDF Polisher.

This is the default hot path used by render_job.sh and iterate_layout.sh. The
older stage scripts remain debuggable entry points, but this orchestrator avoids
paying Python startup once per stage in slow-Python environments.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Allow direct execution from scripts/ while importing sibling stage modules.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import preflight_assets  # noqa: E402
import render_playwright  # noqa: E402
import render_summary  # noqa: E402
import resolve_fonts  # noqa: E402
import resolve_fontsource_fonts  # noqa: E402
from make_contact_sheet import make_contact_sheet  # noqa: E402

BRIDGE_EXIT = 86


class Timing:
    """Wrapper-compatible timing logger.

    Keep this textual format stable; render_summary embeds its tail and tests
    grep for lines like `info: media=print`.
    """

    def __init__(self, path: Path, origin_ms: int | None = None):
        self.path = path
        self.origin_ms = origin_ms or self._now_ms()
        self.current: str | None = None
        self.current_start = self._now_ms()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def elapsed(self) -> float:
        return (self._now_ms() - self.origin_ms) / 1000.0

    def _write(self, line: str) -> None:
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def start(self, name: str) -> None:
        self.current = name
        self.current_start = self._now_ms()
        self._write(f"[{self.elapsed():.2f}s] start: {name}")

    def ok(self, name: str | None = None) -> None:
        label = name or self.current or "unknown"
        duration = (self._now_ms() - self.current_start) / 1000.0
        self._write(f"[{self.elapsed():.2f}s] ok: {label} ({duration:.2f}s)")
        self.current = None

    def info(self, message: str) -> None:
        self._write(f"[{self.elapsed():.2f}s] info: {message}")

    def fail(self, status: int | str, name: str | None = None) -> None:
        label = name or self.current or "unknown"
        duration = (self._now_ms() - self.current_start) / 1000.0
        self._write(f"[{self.elapsed():.2f}s] fail: {label} status={status} after {duration:.2f}s")
        self.current = None


def normalize_html(src: Path, dst: Path) -> None:
    text = src.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")


def run_asset_preflight(html: Path, allow_remote: bool = False) -> None:
    text = html.read_text(encoding="utf-8")
    urls = preflight_assets.collect_resource_urls(text)
    if not urls:
        print("asset preflight: local/embedded assets only")
        return

    fonts, other = preflight_assets.classify(urls)
    print("asset preflight: remote render-time assets detected", file=sys.stderr)
    if fonts:
        print("remote font resources:", file=sys.stderr)
        for url in fonts:
            print(f"  {url}", file=sys.stderr)
        print("action: run scripts/resolve_fonts.py first; it will localize or emit a deterministic download plan.", file=sys.stderr)
    if other:
        print("other remote resources:", file=sys.stderr)
        for url in other:
            print(f"  {url}", file=sys.stderr)
        print("action: stage these resources locally or embed them before rendering.", file=sys.stderr)
    if not allow_remote:
        print("blocked before Playwright render to avoid remote-fetch/admin/font fallback loops.", file=sys.stderr)
        raise SystemExit(1)


def resolve_fonts_stage(source_input: Path, source: Path, font_dir: Path, font_bridge: Path, timing: Timing) -> None:
    status, message = resolve_fonts.resolve(source_input, source, font_dir, font_bridge, timeout=20, embed=True)
    if status == 0:
        print(message)
        print(f"rewritten HTML: {source}")
        print(f"font dir: {font_dir}")
        timing.ok("font_resolution")
        return
    if status == BRIDGE_EXIT:
        print(message, file=sys.stderr)
        timing.info("Google font bridge required; trying deterministic Fontsource/npm embedding")
        status2, message2 = resolve_fontsource_fonts.resolve(
            source_input,
            source,
            font_dir,
            font_bridge / "fontsource-cache",
            font_bridge,
            embed=True,
        )
        print(message2, file=sys.stderr if status2 else sys.stdout)
        if status2 == 0:
            print(f"rewritten HTML: {source}")
            print(f"fontsource cache: {font_bridge / 'fontsource-cache'}")
            print(f"report: {font_bridge / 'fontsource-resolution.json'}")
            timing.ok("font_resolution_fontsource")
            return
        timing.fail(status2)
        raise SystemExit(status2)
    print(message, file=sys.stderr)
    timing.fail(status)
    raise SystemExit(status)


def render_pdf_and_raster(source: Path, pdf: Path, renders: Path, dpi: int, timings: Path, origin_ms: int) -> None:
    # Reuse the existing render module's internals rather than subprocess. This
    # preserves renderer semantics and log lines while avoiding a new Python start.
    rt = render_playwright.Timing(timings, origin_ms)
    rt.log("start", "render_playwright")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    renders.mkdir(parents=True, exist_ok=True)
    for old in renders.glob("page-*.png"):
        old.unlink()

    try:
        asyncio.run(render_playwright.render_pdf(source.resolve(), pdf.resolve(), 30000, rt))
    except Exception:
        rt.log("fail", "chromium_pdf")
        raise

    rt.log("start", f"raster_pdf dpi={dpi}")
    try:
        page_count = render_playwright.raster_with_pymupdf(pdf.resolve(), renders.resolve(), dpi)
        rt.log("ok", f"raster_pdf pages={page_count} engine=pymupdf")
    except Exception:
        rt.log("info", "pymupdf raster failed; trying pdftoppm")
        page_count = render_playwright.raster_with_pdftoppm(pdf.resolve(), renders.resolve(), dpi)
        rt.log("ok", f"raster_pdf pages={page_count} engine=pdftoppm")

    rt.log("ok", "render_playwright")
    print(f"rendered {pdf}")
    print(f"rastered {page_count} pages to {renders}")


def run_pipeline(args: argparse.Namespace) -> int:
    input_html = args.input.resolve()
    if not input_html.exists():
        print(f"input HTML not found: {input_html}", file=sys.stderr)
        return 2

    timings = args.timings.resolve()
    timing = Timing(timings, args.timing_origin_ms)

    try:
        timing.start("normalize_html")
        normalize_html(input_html, args.source_input.resolve())
        timing.ok("normalize_html")

        timing.start("font_resolution")
        resolve_fonts_stage(args.source_input.resolve(), args.source.resolve(), args.font_dir.resolve(), args.font_bridge.resolve(), timing)

        timing.start("asset_preflight")
        run_asset_preflight(args.source.resolve())
        timing.ok("asset_preflight")

        timing.start("playwright_pdf_and_raster")
        render_pdf_and_raster(args.source.resolve(), args.pdf.resolve(), args.renders.resolve(), args.dpi, timings, timing.origin_ms)
        timing.ok("playwright_pdf_and_raster")

        timing.start("contact_sheet")
        count = make_contact_sheet(args.renders.resolve(), args.contact.resolve(), thumb_width=420, cols=3, quality=92)
        print(f"contact sheet {args.contact} ({count} pages, thumb_width=420, cols=3)")
        timing.ok("contact_sheet")

        large_contact = args.large_contact.resolve() if args.large_contact else None
        if large_contact:
            timing.start("large_contact_sheet")
            count = make_contact_sheet(args.renders.resolve(), large_contact, thumb_width=700, cols=2, quality=92)
            print(f"contact sheet {large_contact} ({count} pages, thumb_width=700, cols=2)")
            timing.ok("large_contact_sheet")

        # Same summary logic, same timing labels, no subprocess.
        render_summary.write_render_summary(
            pdf=args.pdf.resolve(),
            renders=args.renders.resolve(),
            contact=args.contact.resolve(),
            summary=args.summary.resolve(),
            large_contact=large_contact,
            timings=timings,
            timing_origin_ms=timing.origin_ms,
        )

        timing.info(f"pipeline=single-process done_label={args.done_label}")
        timing._write(f"[{timing.elapsed():.2f}s] done: {args.done_label}")
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        timing.fail("exception")
        print(f"render pipeline failed: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the complete HTML-to-PDF render pipeline in one Python process.")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--source-input", required=True, type=Path)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--font-dir", required=True, type=Path)
    p.add_argument("--font-bridge", required=True, type=Path)
    p.add_argument("--pdf", required=True, type=Path)
    p.add_argument("--renders", required=True, type=Path)
    p.add_argument("--contact", required=True, type=Path)
    p.add_argument("--large-contact", type=Path, default=None)
    p.add_argument("--summary", required=True, type=Path)
    p.add_argument("--timings", required=True, type=Path)
    p.add_argument("--timing-origin-ms", type=int, default=None)
    p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--done-label", default="render_pipeline")
    return p


def main() -> int:
    return run_pipeline(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
