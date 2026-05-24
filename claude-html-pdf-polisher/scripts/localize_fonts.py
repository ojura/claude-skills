#!/usr/bin/env python3
"""Localize remote web fonts in an HTML file for deterministic Playwright PDF rendering.

Converts Google Fonts <link> / @import references into local @font-face CSS when
network access is available to this script. If network is not available, it still
acts as a preflight detector and reports the remote font dependencies to resolve
outside the render loop.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

GOOGLE_CSS_RE = re.compile(
    r'<link\b(?=[^>]*\brel=["\']stylesheet["\'])(?=[^>]*\bhref=["\'](?P<href>https://fonts\.googleapis\.com/[^"\']+)["\'])[^>]*>',
    re.I,
)
PRECONNECT_RE = re.compile(
    r'<link\b(?=[^>]*\brel=["\']preconnect["\'])(?=[^>]*\bhref=["\']https://fonts\.(?:googleapis|gstatic)\.com["\'])[^>]*>\s*',
    re.I,
)
CSS_IMPORT_RE = re.compile(
    r'@import\s+(?:url\()?(["\']?)(?P<href>https://fonts\.googleapis\.com/[^"\')]+)\1\)?\s*;',
    re.I,
)
URL_RE = re.compile(r'url\((?P<quote>["\']?)(?P<url>https://fonts\.gstatic\.com/[^"\')]+)(?P=quote)\)', re.I)
REMOTE_FONT_RE = re.compile(r'https://fonts\.(?:googleapis|gstatic)\.com/[^\s"\')<>]+', re.I)


def fetch_text(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - user-selected font URL only
        return response.read().decode("utf-8")


def fetch_bytes(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - user-selected font URL only
        return response.read()


def safe_font_name(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    base = Path(parsed.path).name or "font.woff2"
    suffix = Path(base).suffix or ".woff2"
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "-", Path(base).stem)[:70] or "font"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{stem}-{digest}{suffix}"


def replace_urls(css: str, font_dir: Path, css_dir: Path, timeout: int, dry_run: bool) -> str:
    def repl(match: re.Match[str]) -> str:
        url = match.group("url")
        filename = safe_font_name(url)
        target = font_dir / filename
        if not dry_run and not target.exists():
            target.write_bytes(fetch_bytes(url, timeout))
        rel = Path(target).resolve().relative_to(css_dir.resolve()) if target.resolve().is_relative_to(css_dir.resolve()) else target.resolve()
        rel_text = str(rel).replace("\\", "/")
        return f"url('{rel_text}')"

    return URL_RE.sub(repl, css)


def find_remote_fonts(html: str) -> list[str]:
    return sorted(set(REMOTE_FONT_RE.findall(html)))


def localize(html_path: Path, output_path: Path, font_dir: Path, timeout: int, dry_run: bool) -> tuple[str, list[str]]:
    html = html_path.read_text(encoding="utf-8")
    found: list[str] = []

    if dry_run:
        return html, find_remote_fonts(html)

    font_dir.mkdir(parents=True, exist_ok=True)
    css_chunks: list[str] = []

    # Remove preconnects; they are irrelevant for local rendering and can trigger needless network work.
    html = PRECONNECT_RE.sub("", html)

    def link_repl(match: re.Match[str]) -> str:
        href = match.group("href")
        found.append(href)
        css = fetch_text(href, timeout)
        css = replace_urls(css, font_dir, html_path.parent, timeout, dry_run=False)
        css_chunks.append(css)
        return ""

    html = GOOGLE_CSS_RE.sub(link_repl, html)

    def import_repl(match: re.Match[str]) -> str:
        href = match.group("href")
        found.append(href)
        css = fetch_text(href, timeout)
        css = replace_urls(css, font_dir, html_path.parent, timeout, dry_run=False)
        return css

    html = CSS_IMPORT_RE.sub(import_repl, html)

    if css_chunks:
        style = "\n<style id=\"localized-web-fonts\">\n" + "\n".join(css_chunks) + "\n</style>\n"
        m = re.search(r"</head\s*>", html, flags=re.I)
        if m:
            html = html[: m.start()] + style + html[m.start() :]
        else:
            html = style + html

    output_path.write_text(html, encoding="utf-8")
    return html, found


def main() -> int:
    parser = argparse.ArgumentParser(description="Localize Google Fonts references in HTML for deterministic PDF rendering.")
    parser.add_argument("html", type=Path, help="input HTML file")
    parser.add_argument("output", type=Path, help="output HTML file")
    parser.add_argument("--font-dir", type=Path, default=None, help="directory to store downloaded font binaries")
    parser.add_argument("--timeout", type=int, default=30, help="download timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="only report remote font URLs; do not download or rewrite")
    args = parser.parse_args()

    html_path = args.html.resolve()
    output_path = args.output.resolve()
    font_dir = (args.font_dir or output_path.parent / "fonts").resolve()

    if not html_path.exists():
        raise SystemExit(f"input HTML not found: {html_path}")

    try:
        rewritten, found = localize(html_path, output_path, font_dir, args.timeout, args.dry_run)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print("font localization failed before rendering.", file=sys.stderr)
        print("Do not continue hoping remote fonts will work inside the PDF render.", file=sys.stderr)
        print("Use a web/download bridge or ask the user to upload the required font files, then rewrite @font-face locally.", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    remaining = find_remote_fonts(rewritten)
    if args.dry_run:
        if found:
            print("remote font dependencies:")
            for url in found:
                print(url)
        else:
            print("no remote Google Font dependencies detected")
        return 0

    if found:
        print(f"localized {len(set(found))} Google Fonts stylesheet(s) into {output_path}")
        print(f"font files stored in {font_dir}")
    else:
        print("no Google Fonts stylesheet links/imports found; copied/reused HTML unchanged")
    if remaining:
        print("warning: remote font URLs remain after localization:", file=sys.stderr)
        for url in remaining:
            print(url, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
