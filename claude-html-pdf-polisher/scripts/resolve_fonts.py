#!/usr/bin/env python3
"""Deterministically resolve Google Fonts references before PDF rendering.

This script is intentionally a font-resolution stage, not a renderer. It turns
remote Google Fonts stylesheet links/imports into local @font-face CSS using a
fixed cache/bridge protocol:

1. Try to fetch Google CSS and font binaries directly from this process.
2. If direct fetch fails, write a machine-readable download plan and exit 86.
3. After the planned files are staged (by a tool bridge or uploaded files), rerun
   the same command; it will rewrite the HTML without remote font URLs.

No PDF render should start until this script has produced HTML with zero remote
fonts and preflight_assets.py agrees.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
BRIDGE_EXIT = 86

GOOGLE_LINK_RE = re.compile(
    r'<link\b(?=[^>]*\brel=["\']stylesheet["\'])(?=[^>]*\bhref=["\'](?P<href>https://fonts\.googleapis\.com/[^"\']+)["\'])[^>]*>\s*',
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
GOOGLE_CSS_URL_RE = re.compile(r'https://fonts\.googleapis\.com/[^\s"\')<>]+', re.I)
GSTATIC_URL_RE = re.compile(r'https://fonts\.gstatic\.com/[^\s"\')<>]+', re.I)
REMOTE_FONT_RE = re.compile(r'https://fonts\.(?:googleapis|gstatic)\.com/[^\s"\')<>]+', re.I)
URL_RE = re.compile(r'url\((?P<quote>["\']?)(?P<url>https://fonts\.gstatic\.com/[^"\')]+)(?P=quote)\)', re.I)


@dataclass(frozen=True)
class DownloadItem:
    kind: str
    url: str
    path: Path
    reason: str


def sha_name(url: str, suffix: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{digest}{suffix}"


def safe_font_name(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    base = Path(parsed.path).name or "font.woff2"
    suffix = Path(base).suffix or ".woff2"
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "-", Path(base).stem)[:80] or "font"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{stem}-{digest}{suffix}"


def cache_path_for_css(url: str, bridge_dir: Path) -> Path:
    return bridge_dir / "css" / sha_name(url, ".css")


def cache_path_for_font(url: str, font_dir: Path) -> Path:
    return font_dir / safe_font_name(url)


def fetch_bytes(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - limited to declared font URLs
        return response.read()


def try_fetch_to(url: str, target: Path, timeout: int) -> tuple[bool, str | None]:
    if target.exists() and target.stat().st_size > 0:
        return True, None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = fetch_bytes(url, timeout)
        target.write_bytes(data)
        return True, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if target.exists() and target.stat().st_size == 0:
            try:
                target.unlink()
            except OSError:
                pass
        return False, str(exc)


def collect_google_css_urls(html: str) -> list[str]:
    urls: set[str] = set()
    for match in GOOGLE_LINK_RE.finditer(html):
        urls.add(match.group("href"))
    for match in CSS_IMPORT_RE.finditer(html):
        urls.add(match.group("href"))
    return sorted(urls)


def strip_google_links_and_preconnects(html: str) -> str:
    html = PRECONNECT_RE.sub("", html)
    html = GOOGLE_LINK_RE.sub("", html)
    # @imports are handled in CSS/HTML text. Remove remote imports from inline styles.
    html = CSS_IMPORT_RE.sub("", html)
    return html


def rel_from_output(target: Path, output_path: Path) -> str:
    try:
        rel = target.resolve().relative_to(output_path.parent.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return target.resolve().as_uri()


def font_data_url(font_path: Path) -> str:
    mime = mimetypes.guess_type(str(font_path))[0] or "font/woff2"
    data = base64.b64encode(font_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def rewrite_css_urls(css: str, font_dir: Path, output_path: Path, embed: bool) -> str:
    def repl(match: re.Match[str]) -> str:
        url = match.group("url")
        font_path = cache_path_for_font(url, font_dir)
        if embed:
            replacement = font_data_url(font_path)
        else:
            replacement = rel_from_output(font_path, output_path)
        return f"url('{replacement}')"

    return URL_RE.sub(repl, css)


def inject_font_css(html: str, css_chunks: list[str]) -> str:
    if not css_chunks:
        return html
    style = "\n<style id=\"resolved-web-fonts\">\n" + "\n".join(css_chunks) + "\n</style>\n"
    match = re.search(r"</head\s*>", html, flags=re.I)
    if match:
        return html[: match.start()] + style + html[match.start() :]
    return style + html


def write_plan(plan_path: Path, downloads: list[DownloadItem], html_path: Path, output_path: Path) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    actions_path = plan_path.parent / "font-bridge-actions.jsonl"
    container_actions_path = plan_path.parent / "container-download-actions.jsonl"
    open_urls_path = plan_path.parent / "open-before-download-urls.txt"
    stop_path = plan_path.parent / "FONT_BRIDGE_REQUIRED.md"

    bridge_steps = []
    for item in downloads:
        bridge_steps.append({
            "action": "view_url_first",
            "url": item.url,
            "why": "surface this exact URL through the web layer before download; do not search or substitute fonts",
        })
        bridge_steps.append({
            "action": "download_after_view",
            "url": item.url,
            "filepath": str(item.path),
            "container_download_payload": {"url": item.url, "filepath": str(item.path)},
        })

    payload = {
        "status": "bridge_required",
        "created_at_unix": int(time.time()),
        "input_html": str(html_path),
        "output_html": str(output_path),
        "hard_stop": "DO NOT SUBSTITUTE FONTS. DO NOT REMOVE GOOGLE FONT LINKS AND REPLACE FAMILIES. A substitute-font PDF is a failed job.",
        "bridge_rule": "For each URL, view/open the exact URL first, then download that same URL to the exact filepath. If either step fails, stop and report the blocker; do not render with substitutes.",
        "instructions": [
            "Stage each URL at the exact path listed, then rerun the same resolve_fonts.py/render_job.sh command.",
            "In ChatGPT, first open/view each exact URL so the web layer surfaces it; only then call container.download with the exact payload.",
            "Do not use web search, package-name research, Fontsource research, or installed/system fonts during routine bridge handling.",
            "If open/view or container.download fails for any exact URL, stop and report this plan instead of rendering.",
            "Do not substitute EB Garamond, Noto Serif, DejaVu, system fonts, or any other fallback for the requested fonts.",
            "After rerun succeeds, run preflight_assets.py on the rewritten HTML before rendering.",
        ],
        "downloads": [
            {
                "kind": item.kind,
                "url": item.url,
                "path": str(item.path),
                "reason": item.reason,
                "view_before_download": True,
                "view_url": item.url,
                "container_download_payload": {"url": item.url, "filepath": str(item.path)},
            }
            for item in downloads
        ],
        "bridge_steps": bridge_steps,
        "action_files": {
            "font_bridge_actions_jsonl": str(actions_path),
            "container_download_actions_jsonl": str(container_actions_path),
            "open_before_download_urls": str(open_urls_path),
            "hard_stop_note": str(stop_path),
        },
    }
    plan_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    actions_path.write_text(
        "".join(json.dumps(step, ensure_ascii=False) + "\n" for step in bridge_steps),
        encoding="utf-8",
    )
    container_actions_path.write_text(
        "".join(
            json.dumps({
                "requires_view_before_download": True,
                "view_url": item.url,
                "url": item.url,
                "filepath": str(item.path),
            }, ensure_ascii=False) + "\n"
            for item in downloads
        ),
        encoding="utf-8",
    )
    open_urls_path.write_text("".join(item.url + "\n" for item in downloads), encoding="utf-8")
    stop_path.write_text(
        "# Font bridge required\n\n"
        "HARD STOP: do not render with substituted or fallback fonts. Do not remove "
        "Google Fonts links and replace font-family declarations. A PDF produced with "
        "substituted fonts is a failed job, not a workaround.\n\n"
        "Exact protocol, in order:\n\n"
        "1. Open/view each exact URL in `open-before-download-urls.txt` or each "
        "`view_url_first` item in `font-bridge-actions.jsonl`.\n"
        "2. Only after that, download the same URL to the exact filepath from "
        "`container-download-actions.jsonl`.\n"
        "3. Rerun the same `render_job.sh` command. If the rerun emits another "
        "bridge plan for font binaries, repeat this view-then-download loop.\n"
        "4. If any exact URL cannot be viewed or downloaded, report the blocker and "
        "the plan path. Do not search, do not substitute, do not render.\n\n"
        f"Plan: `{plan_path}`\n"
        f"Actions: `{actions_path}`\n"
        f"Open URLs: `{open_urls_path}`\n"
        f"Download payloads: `{container_actions_path}`\n",
        encoding="utf-8",
    )


def print_plan(plan_path: Path, downloads: list[DownloadItem]) -> None:
    print("font bridge required; direct font localization could not complete in this sandbox", file=sys.stderr)
    print("HARD STOP: do not substitute fonts or render with fallback/system fonts", file=sys.stderr)
    print(f"download plan: {plan_path}", file=sys.stderr)
    print(f"open URLs first: {plan_path.parent / 'open-before-download-urls.txt'}", file=sys.stderr)
    print(f"bridge actions: {plan_path.parent / 'font-bridge-actions.jsonl'}", file=sys.stderr)
    print(f"download actions: {plan_path.parent / 'container-download-actions.jsonl'}", file=sys.stderr)
    print(f"stop note: {plan_path.parent / 'FONT_BRIDGE_REQUIRED.md'}", file=sys.stderr)
    print("downloads needed:", file=sys.stderr)
    for item in downloads:
        print(f"  - {item.kind}: {item.url}", file=sys.stderr)
        print(f"    -> {item.path}", file=sys.stderr)
    print("after staging these files, rerun the same render_job.sh/resolve_fonts.py command", file=sys.stderr)
    print("if the download bridge is unavailable, report the blocker instead of rendering", file=sys.stderr)


def resolve(html_path: Path, output_path: Path, font_dir: Path, bridge_dir: Path, timeout: int, embed: bool) -> tuple[int, str]:
    html = html_path.read_text(encoding="utf-8")
    css_urls = collect_google_css_urls(html)
    if not css_urls:
        if html_path.resolve() != output_path.resolve():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(html, encoding="utf-8")
        return 0, "no Google Fonts stylesheet links/imports detected"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    font_dir.mkdir(parents=True, exist_ok=True)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    plan_path = bridge_dir / "font-download-plan.json"

    downloads: list[DownloadItem] = []
    css_by_url: dict[str, str] = {}

    for url in css_urls:
        css_path = cache_path_for_css(url, bridge_dir)
        ok, error = try_fetch_to(url, css_path, timeout)
        if ok:
            css_by_url[url] = css_path.read_text(encoding="utf-8")
        else:
            downloads.append(DownloadItem("google_css", url, css_path, error or "direct fetch failed"))

    if downloads:
        write_plan(plan_path, downloads, html_path, output_path)
        print_plan(plan_path, downloads)
        return BRIDGE_EXIT, f"bridge required for {len(downloads)} Google CSS file(s)"

    bad_css: list[str] = []
    for url, css in css_by_url.items():
        if "@font-face" not in css or not GSTATIC_URL_RE.search(css):
            bad_css.append(url)
    if bad_css:
        print("staged Google Fonts CSS did not contain usable @font-face/gstatic URLs; do not render", file=sys.stderr)
        for url in bad_css:
            print(f"  invalid css for: {url}", file=sys.stderr)
        print("HARD STOP: do not substitute fonts. Re-view/re-download the exact CSS URL or report the blocker.", file=sys.stderr)
        return 2, "invalid staged Google Fonts CSS"

    font_urls: set[str] = set()
    for css in css_by_url.values():
        font_urls.update(GSTATIC_URL_RE.findall(css))

    for url in sorted(font_urls):
        font_path = cache_path_for_font(url, font_dir)
        ok, error = try_fetch_to(url, font_path, timeout)
        if not ok:
            downloads.append(DownloadItem("font_binary", url, font_path, error or "direct fetch failed"))

    if downloads:
        write_plan(plan_path, downloads, html_path, output_path)
        print_plan(plan_path, downloads)
        return BRIDGE_EXIT, f"bridge required for {len(downloads)} font binary file(s)"

    css_chunks = []
    for url in css_urls:
        css = css_by_url[url]
        css_chunks.append("/* localized Google Fonts stylesheet */\n" + rewrite_css_urls(css, font_dir, output_path, embed))

    rewritten = strip_google_links_and_preconnects(html)
    rewritten = inject_font_css(rewritten, css_chunks)
    remaining = REMOTE_FONT_RE.findall(rewritten)
    if remaining:
        output_path.write_text(rewritten, encoding="utf-8")
        print("remote Google font URLs remain after resolution:", file=sys.stderr)
        for url in sorted(set(remaining)):
            print(f"  {url}", file=sys.stderr)
        return 2, "remote font URLs remain"

    output_path.write_text(rewritten, encoding="utf-8")
    return 0, f"resolved {len(css_urls)} Google Fonts stylesheet(s) and {len(font_urls)} font binary URL(s)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve Google Fonts references to local font files before rendering.")
    parser.add_argument("html", type=Path, help="input HTML")
    parser.add_argument("output", type=Path, help="rewritten output HTML")
    parser.add_argument("--font-dir", type=Path, default=None, help="font binary cache/output directory")
    parser.add_argument("--bridge-dir", type=Path, default=None, help="CSS/download-plan cache directory")
    parser.add_argument("--timeout", type=int, default=20, help="direct fetch timeout in seconds")
    parser.add_argument("--embed", action="store_true", help="embed fonts as data URLs in output HTML for internal rendering")
    parser.add_argument("--dry-run", action="store_true", help="detect Google Fonts URLs and report what would be resolved")
    args = parser.parse_args()

    html_path = args.html.resolve()
    output_path = args.output.resolve()
    if not html_path.exists():
        print(f"input HTML not found: {html_path}", file=sys.stderr)
        return 2

    html = html_path.read_text(encoding="utf-8")
    css_urls = collect_google_css_urls(html)
    if args.dry_run:
        if css_urls:
            print("Google Fonts stylesheets/imports detected:")
            for url in css_urls:
                print(url)
        else:
            print("no Google Fonts stylesheet links/imports detected")
        return 0

    font_dir = (args.font_dir or output_path.parent / "fonts").resolve()
    bridge_dir = (args.bridge_dir or output_path.parent / "font-bridge").resolve()
    status, message = resolve(html_path, output_path, font_dir, bridge_dir, args.timeout, args.embed)
    if status == 0:
        print(message)
        print(f"rewritten HTML: {output_path}")
        print(f"font dir: {font_dir}")
    else:
        print(message, file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
