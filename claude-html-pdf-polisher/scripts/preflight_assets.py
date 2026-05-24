#!/usr/bin/env python3
"""Fail-fast preflight for remote assets before Playwright PDF rendering.

The render loop is deterministic only when HTML, CSS, fonts, and images are local or
embedded. This script blocks common remote resource references before Chromium has a
chance to hang, fall back, or produce different typography.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REMOTE_RE = re.compile(r"https?://[^\s\"'()<>]+", re.I)
RESOURCE_CONTEXT_RE = re.compile(
    r"(?:src|href)\s*=\s*['\"](?P<url>https?://[^'\"]+)['\"]|"
    r"url\(\s*['\"]?(?P<cssurl>https?://[^'\")]+)['\"]?\s*\)|"
    r"@import\s+(?:url\()?\s*['\"]?(?P<importurl>https?://[^'\") ;]+)",
    re.I,
)

FONT_HOST_RE = re.compile(r"https?://fonts\.(?:googleapis|gstatic)\.com/", re.I)
REMOTE_STYLESHEET_RE = re.compile(r"<link\b(?=[^>]*\brel=['\"]stylesheet['\"])(?=[^>]*\bhref=['\"]https?://)", re.I)
REMOTE_IMAGE_RE = re.compile(r"<(?:img|source)\b(?=[^>]*\bsrc=['\"]https?://)", re.I)


def collect_resource_urls(text: str) -> list[str]:
    urls: set[str] = set()
    for match in RESOURCE_CONTEXT_RE.finditer(text):
        for key in ("url", "cssurl", "importurl"):
            value = match.groupdict().get(key)
            if value:
                urls.add(value)
    return sorted(urls)


def classify(urls: list[str]) -> tuple[list[str], list[str]]:
    fonts = [u for u in urls if FONT_HOST_RE.search(u)]
    other = [u for u in urls if u not in fonts]
    return fonts, other


def main() -> int:
    parser = argparse.ArgumentParser(description="Check HTML for remote assets before deterministic PDF rendering.")
    parser.add_argument("html", type=Path, help="HTML file to check")
    parser.add_argument("--allow-remote", action="store_true", help="warn but do not fail on remote assets")
    args = parser.parse_args()

    if not args.html.exists():
        print(f"input HTML not found: {args.html}", file=sys.stderr)
        return 2

    text = args.html.read_text(encoding="utf-8")
    urls = collect_resource_urls(text)
    if not urls:
        print("asset preflight: local/embedded assets only")
        return 0

    fonts, other = classify(urls)
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

    if args.allow_remote:
        return 0
    print("blocked before Playwright render to avoid remote-fetch/admin/font fallback loops.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
