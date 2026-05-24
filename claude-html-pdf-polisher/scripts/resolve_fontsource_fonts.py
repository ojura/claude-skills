#!/usr/bin/env python3
"""Resolve known Google Fonts families through Fontsource packages and embed them.

This is the deterministic fallback when direct Google Fonts localization cannot
run inside the sandbox. It is not a substitution path: it preserves the original
CSS family names and supplies those families from Fontsource variable-font
packages. By default it embeds WOFF2 files as data URLs because Chromium loaded
via page.set_content(...) may block sibling file:// font assets.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

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
    r'@import\s+(?:url\()?(?:["\']?)(?P<href>https://fonts\.googleapis\.com/[^"\')]+)(?:["\']?)\)?\s*;',
    re.I,
)
REMOTE_FONT_RE = re.compile(r'https://fonts\.(?:googleapis|gstatic)\.com/[^\s"\')<>]+', re.I)
URL_RE = re.compile(r"url\((?P<quote>['\"]?)(?P<url>\.\/files\/[^'\")]+\.woff2)(?P=quote)\)", re.I)
HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.I)
HEAD_OPEN_RE = re.compile(r"<head[^>]*>", re.I)


@dataclass(frozen=True)
class FontsourceSpec:
    package: str
    old_family: str
    family: str
    css_files: tuple[str, ...]


# Known-good specs: deterministic CSS-file choices for families with multiple
# valid axis files. Auto-discovery handles everything else.
KNOWN_FAMILIES: dict[str, FontsourceSpec] = {
    "fraunces": FontsourceSpec(
        package="@fontsource-variable/fraunces",
        old_family="Fraunces Variable",
        family="Fraunces",
        css_files=("full.css", "full-italic.css"),
    ),
    "source serif 4": FontsourceSpec(
        package="@fontsource-variable/source-serif-4",
        old_family="Source Serif 4 Variable",
        family="Source Serif 4",
        css_files=("opsz.css", "opsz-italic.css"),
    ),
    "jetbrains mono": FontsourceSpec(
        package="@fontsource-variable/jetbrains-mono",
        old_family="JetBrains Mono Variable",
        family="JetBrains Mono",
        css_files=("wght.css", "wght-italic.css"),
    ),
}

# CSS-file pickers, in preference order: full coverage first, then optical-size,
# then weight axis, then non-variable index.css. Used when the package is
# auto-discovered and we have no curated css_files choice for it.
DISCOVERY_CSS_OPTIONS: tuple[tuple[str, ...], ...] = (
    ("full.css", "full-italic.css"),
    ("opsz.css", "opsz-italic.css"),
    ("wght.css", "wght-italic.css"),
    ("full.css",),
    ("opsz.css",),
    ("wght.css",),
    ("index.css",),
)


def kebab_case_family(name: str) -> str:
    """Convert a Google Fonts family display name to Fontsource's package suffix.

    Examples:
        "Source Serif 4" -> "source-serif-4"
        "Playfair Display SC" -> "playfair-display-sc"
        "Noto Sans" -> "noto-sans"
    """
    s = name.strip().lower()
    s = re.sub(r"[\s_+]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def pick_css_files(pdir: Path) -> tuple[str, ...]:
    """Return the first CSS-file option present in the installed package dir."""
    for option in DISCOVERY_CSS_OPTIONS:
        if all((pdir / name).exists() for name in option):
            return option
    return ()


def discover_fontsource_spec(family: str, cache_dir: Path) -> FontsourceSpec | None:
    """Try to find a Fontsource package for an arbitrary Google Fonts family.

    Tries the variable package first (@fontsource-variable/<kebab>) then the
    non-variable one (@fontsource/<kebab>). On successful npm install,
    inspects the package directory to pick a CSS file pattern. Returns None
    if neither package can be installed.
    """
    kebab = kebab_case_family(family)
    candidates = (
        (f"@fontsource-variable/{kebab}", f"{family} Variable"),
        (f"@fontsource/{kebab}", family),
    )
    for package, old_family in candidates:
        try:
            ensure_package(cache_dir, package)
        except Exception:
            continue
        pdir = package_dir(cache_dir, package)
        css_files = pick_css_files(pdir)
        if css_files:
            return FontsourceSpec(
                package=package,
                old_family=old_family,
                family=family,
                css_files=css_files,
            )
    return None


def norm_family(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("+", " ").strip()).lower()


def collect_google_css_urls(html: str) -> list[str]:
    urls: set[str] = set()
    for match in GOOGLE_LINK_RE.finditer(html):
        urls.add(match.group("href"))
    for match in CSS_IMPORT_RE.finditer(html):
        urls.add(match.group("href"))
    return sorted(urls)


def families_from_google_url(url: str) -> list[str]:
    parsed = urllib.parse.urlsplit(url)
    values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("family", [])
    families: list[str] = []
    for raw in values:
        decoded = urllib.parse.unquote_plus(raw)
        family = decoded.split(":", 1)[0].strip()
        if family:
            families.append(re.sub(r"\s+", " ", family))
    return families


def strip_google_font_references(html: str) -> str:
    html = PRECONNECT_RE.sub("", html)
    html = GOOGLE_LINK_RE.sub("", html)
    html = CSS_IMPORT_RE.sub("", html)
    return html


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def ensure_npm_project(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    pkg = cache_dir / "package.json"
    if not pkg.exists():
        pkg.write_text('{"private":true,"name":"polisher-fontsource-cache","version":"0.0.0"}\n', encoding="utf-8")


def package_dir(cache_dir: Path, package: str) -> Path:
    # package like @fontsource-variable/fraunces
    return cache_dir / "node_modules" / package


def ensure_package(cache_dir: Path, package: str) -> None:
    ensure_npm_project(cache_dir)
    pdir = package_dir(cache_dir, package)
    if pdir.exists() and (pdir / "package.json").exists():
        return
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found; cannot install Fontsource packages")
    proc = run([npm, "install", package, "--silent"], cwd=cache_dir, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"npm install failed for {package}:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    if not pdir.exists():
        raise RuntimeError(f"npm install completed but package directory is missing: {pdir}")


def font_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "font/woff2"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def css_with_embedded_fonts(spec: FontsourceSpec, cache_dir: Path, font_dir: Path, embed: bool, output_path: Path) -> tuple[str, list[Path]]:
    pdir = package_dir(cache_dir, spec.package)
    chunks: list[str] = []
    used_files: list[Path] = []
    for css_name in spec.css_files:
        css_path = pdir / css_name
        if not css_path.exists():
            raise RuntimeError(f"Fontsource CSS missing for {spec.family}: {css_path}")
        css = css_path.read_text(encoding="utf-8")
        css = css.replace(f"font-family: '{spec.old_family}';", f"font-family: '{spec.family}';")
        css = css.replace('format(\'woff2-variations\')', 'format(\'woff2\')')
        css = css.replace('format("woff2-variations")', 'format("woff2")')

        def repl(match: re.Match[str]) -> str:
            rel_url = match.group("url")
            source = (pdir / rel_url).resolve()
            if not source.exists():
                raise RuntimeError(f"Fontsource referenced missing font file: {source}")
            used_files.append(source)
            if embed:
                return "url('" + font_data_url(source) + "')"
            # Local-file mode is retained for completeness, but wrappers should use --embed.
            target = font_dir / spec.family.lower().replace(" ", "-") / "files" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            try:
                rel = target.resolve().relative_to(output_path.parent.resolve())
                href = str(rel).replace(os.sep, "/")
            except ValueError:
                href = target.resolve().as_uri()
            return f"url('{href}')"

        chunks.append("/* Fontsource-localized requested family: " + spec.family + " */\n" + URL_RE.sub(repl, css))
    return "\n\n".join(chunks), used_files


def inject_head(html: str, injection: str) -> str:
    m = HEAD_CLOSE_RE.search(html)
    if m:
        return html[: m.start()] + injection + "\n" + html[m.start() :]
    m = HEAD_OPEN_RE.search(html)
    if m:
        return html[: m.end()] + "\n" + injection + html[m.end() :]
    return injection + "\n" + html


def write_stop(bridge_dir: Path, message: str, details: dict) -> None:
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "FONTSOURCE_RESOLUTION_FAILED.md").write_text(
        "# Fontsource font resolution failed\n\n"
        "HARD STOP: do not substitute fonts or render with fallback/system fonts.\n\n"
        + message
        + "\n\nDetails JSON: `fontsource-resolution-failed.json`\n",
        encoding="utf-8",
    )
    (bridge_dir / "fontsource-resolution-failed.json").write_text(json.dumps(details, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def resolve(input_html: Path, output_html: Path, font_dir: Path, cache_dir: Path, bridge_dir: Path, embed: bool) -> tuple[int, str]:
    html = input_html.read_text(encoding="utf-8")
    google_urls = collect_google_css_urls(html)
    if not google_urls:
        if input_html.resolve() != output_html.resolve():
            output_html.parent.mkdir(parents=True, exist_ok=True)
            output_html.write_text(html, encoding="utf-8")
        return 0, "no Google Fonts stylesheet links/imports detected"

    requested: list[str] = []
    for url in google_urls:
        requested.extend(families_from_google_url(url))
    # Preserve order while de-duping.
    seen: set[str] = set()
    families: list[str] = []
    for family in requested:
        key = norm_family(family)
        if key not in seen:
            seen.add(key)
            families.append(family)

    output_html.parent.mkdir(parents=True, exist_ok=True)
    font_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    specs: list[FontsourceSpec] = []
    discovered: list[str] = []
    unresolved: list[str] = []
    for family in families:
        key = norm_family(family)
        if key in KNOWN_FAMILIES:
            specs.append(KNOWN_FAMILIES[key])
            continue
        spec = discover_fontsource_spec(family, cache_dir)
        if spec is not None:
            specs.append(spec)
            discovered.append(family)
        else:
            unresolved.append(family)

    if unresolved:
        write_stop(
            bridge_dir,
            "Could not locate Fontsource packages for: " + ", ".join(unresolved),
            {
                "unresolved_families": unresolved,
                "google_css_urls": google_urls,
                "tried_known": sorted(spec.family for spec in KNOWN_FAMILIES.values()),
                "tried_discovery": [kebab_case_family(f) for f in unresolved],
            },
        )
        return BRIDGE_EXIT, "Fontsource resolution failed for one or more requested families"
    used_files: list[Path] = []
    css_chunks: list[str] = []
    install_errors: list[str] = []
    for spec in specs:
        try:
            ensure_package(cache_dir, spec.package)
            css, files = css_with_embedded_fonts(spec, cache_dir, font_dir, embed, output_html)
            css_chunks.append(css)
            used_files.extend(files)
        except Exception as exc:
            install_errors.append(f"{spec.family} ({spec.package}): {exc}")

    if install_errors:
        write_stop(
            bridge_dir,
            "Deterministic Fontsource/npm resolution failed.\n\n" + "\n".join(f"- {e}" for e in install_errors),
            {"errors": install_errors, "families": families, "google_css_urls": google_urls},
        )
        return BRIDGE_EXIT, "Fontsource/npm resolution failed"

    style = "\n<style id=\"polisher-fontsource-fonts\">\n" + "\n\n".join(css_chunks) + "\n</style>"
    meta = "\n<meta name=\"polisher-required-fonts\" content=\"" + "|".join(spec.family for spec in specs) + "\">"
    rewritten = strip_google_font_references(html)
    rewritten = inject_head(rewritten, meta + style)

    remaining = REMOTE_FONT_RE.findall(rewritten)
    if remaining:
        write_stop(
            bridge_dir,
            "Remote Google font URLs remain after Fontsource rewrite.",
            {"remaining_remote_fonts": sorted(set(remaining)), "families": families},
        )
        output_html.write_text(rewritten, encoding="utf-8")
        return 2, "remote Google font URLs remain after Fontsource rewrite"

    output_html.write_text(rewritten, encoding="utf-8")
    report = {
        "status": "ok",
        "method": "fontsource_embedded" if embed else "fontsource_local_files",
        "input_html": str(input_html),
        "output_html": str(output_html),
        "families": [spec.family for spec in specs],
        "packages": [spec.package for spec in specs],
        "auto_discovered_families": discovered,
        "embedded": embed,
        "font_file_count": len(set(used_files)),
        "created_at_unix": int(time.time()),
        "note": "Requested family names are preserved; this is not a substitute-font render.",
    }
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "fontsource-resolution.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0, f"resolved Google Fonts via Fontsource for {', '.join(report['families'])}; embedded={embed}; font_files={report['font_file_count']}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve supported Google Fonts through Fontsource packages and embed them.")
    parser.add_argument("html", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font-dir", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None, help="npm/Fontsource cache directory")
    parser.add_argument("--bridge-dir", type=Path, default=None, help="directory for reports and failure notes")
    parser.add_argument("--embed", action="store_true", default=False, help="embed WOFF2 data URLs. Wrappers should pass this.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    html_path = args.html.resolve()
    output_path = args.output.resolve()
    if not html_path.exists():
        print(f"input HTML not found: {html_path}", file=sys.stderr)
        return 2
    html = html_path.read_text(encoding="utf-8")
    urls = collect_google_css_urls(html)
    families: list[str] = []
    for url in urls:
        families.extend(families_from_google_url(url))
    if args.dry_run:
        print("Google Fonts URLs:")
        for url in urls:
            print("-", url)
        print("Requested families:")
        for family in families:
            status = "known" if norm_family(family) in KNOWN_FAMILIES else "unsupported"
            print(f"- {family}: {status}")
        return 0

    font_dir = (args.font_dir or output_path.parent / "fonts").resolve()
    cache_dir = (args.cache_dir or output_path.parent / "fontsource-cache").resolve()
    bridge_dir = (args.bridge_dir or output_path.parent / "font-bridge").resolve()
    status, message = resolve(html_path, output_path, font_dir, cache_dir, bridge_dir, args.embed)
    print(message, file=sys.stderr if status else sys.stdout)
    if status == 0:
        print(f"rewritten HTML: {output_path}")
        print(f"fontsource cache: {cache_dir}")
        print(f"report: {bridge_dir / 'fontsource-resolution.json'}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
