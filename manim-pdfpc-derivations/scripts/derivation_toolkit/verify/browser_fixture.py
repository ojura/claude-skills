"""Read browser acceptance cases from an exact standalone HTML package."""
from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
import re


def load_fixture(html: Path) -> dict:
    """Use the bundled document's themes, page geometry and authored scene IDs."""
    matches = re.findall(
        r'<script type="application/json" id="derivation-bundle">(.*?)</script>',
        html.read_text(encoding="utf-8"), re.DOTALL,
    )
    if len(matches) != 1:
        raise ValueError("Expected one embedded derivation-bundle payload")
    payload = json.loads(matches[0])
    manifest = payload["manifest"]
    if manifest.get("version") != 1 or not manifest.get("themes"):
        raise ValueError("Expected version 1 presentation metadata with available themes")
    timelines = {}
    for theme, entry in manifest["themes"].items():
        if not entry["pages"]:
            raise ValueError(f"{theme}: no PDF pages")
        raw = base64.b64decode(payload["assets"][entry["vectors"]], validate=True)
        timeline = json.loads(gzip.decompress(raw))
        if timeline.get("version") != 1:
            raise ValueError(f"{theme}: unsupported timeline version")
        scenes = {scene["id"]: scene for scene in timeline["scenes"]}
        if len(scenes) != len(timeline["scenes"]):
            raise ValueError(f"{theme}: duplicate timeline scene IDs")
        for page in entry["pages"]:
            if page.get("scene") and page["scene"]["id"] not in scenes:
                raise ValueError(f"{theme}: missing linked scene {page['scene']['id']}")
        timelines[theme] = scenes
    return {"manifest": manifest, "timelines": timelines}


def scene_at(fixture, theme, page):
    metadata = fixture["manifest"]["themes"][theme]["pages"][page - 1]
    return fixture["timelines"][theme][metadata["scene"]["id"]] if metadata.get("scene") else None
