"""Embed the maintained player and its staged assets in one local HTML file."""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path
import re


_IMPORT_MAP = re.compile(
    r'(<script type="importmap" id="derivation-imports">)(.*?)(</script>)', re.DOTALL,
)
_STYLE = '<link rel="stylesheet" href="./player.css">'
_PLAYER = '<script type="module" src="./player.mjs"></script>'


def _replace_once(source: str, marker: str, replacement: str) -> str:
    if source.count(marker) != 1:
        raise ValueError("The HTML template must contain exactly one " + marker)
    return source.replace(marker, replacement, 1)


def _json_script(value: object) -> str:
    # HTML parses script end tags before interpreting JSON string escapes.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).replace("<", r"\u003c")


def _encoded(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("An embedded resource must belong to the staged bundle: " + relative)
    return base64.b64encode(path.read_bytes()).decode("ascii")


def embed(root: Path) -> None:
    """Replace the staged index with an HTML containing every playback resource."""
    page = root / "index.html"
    source = page.read_text(encoding="utf-8")
    maps = list(_IMPORT_MAP.finditer(source))
    if len(maps) != 1:
        raise ValueError("The HTML template must contain one derivation-imports import map")
    match = maps[0]
    imports = json.loads(match.group(2))
    for name, path in imports["imports"].items():
        imports["imports"][name] = "data:text/javascript;base64," + _encoded(root, path)
    source = source[:match.start(2)] + _json_script(imports) + source[match.end(2):]
    source = _replace_once(source, _STYLE, "<style>\n" +
                           (root / "player.css").read_text(encoding="utf-8") + "\n</style>")
    player = "data:text/javascript;base64," + _encoded(root, "player.mjs")
    source = _replace_once(source, _PLAYER, '<script type="module" src="' + player + '"></script>')
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    source = _replace_once(source, "<title>Presentation</title>",
                           "<title>" + html.escape(manifest["title"]) + "</title>")
    source = _replace_once(source, '<html lang="en">',
                           '<html lang="' + html.escape(manifest.get("language", "en"), quote=True) + '">')
    assets = {"vendor/pdf.worker.mjs"}
    for theme in manifest["themes"].values():
        assets.update((theme["pdf"], theme["vectors"]))
    payload = {
        "manifest": manifest,
        "assets": {path: _encoded(root, path) for path in sorted(assets)},
        "pdfjsLicense": (root / "vendor/PDFJS-LICENSE.txt").read_text(encoding="utf-8"),
    }
    source = _replace_once(
        source, '<script type="importmap" id="derivation-imports">',
        '<script type="application/json" id="derivation-bundle">' + _json_script(payload) +
        '</script>\n  <script type="importmap" id="derivation-imports">',
    )
    page.write_text(source, encoding="utf-8")
