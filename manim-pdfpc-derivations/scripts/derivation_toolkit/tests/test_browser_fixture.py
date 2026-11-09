"""The browser verifier must derive cases from independent document packages."""
from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from ..verify.browser_fixture import load_fixture, scene_at


class BrowserFixtures(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browser fixture ")
        self.addCleanup(self.temporary.cleanup)
        self.html = Path(self.temporary.name) / "only document.html"

    def write_bundle(self, editions):
        manifest = {"version": 1, "title": "Independent document", "themes": {}}
        assets = {}
        for theme, (pages, scenes) in editions.items():
            path = f"assets/{theme}.vector.json.gz"
            assets[path] = base64.b64encode(gzip.compress(json.dumps({
                "version": 1, "scenes": scenes}).encode(), mtime=0)).decode()
            manifest["themes"][theme] = {"pages": pages, "vectors": path}
        payload = json.dumps({"manifest": manifest, "assets": assets})
        self.html.write_text('<script type="application/json" id="derivation-bundle">' +
                             payload + "</script>")
        return payload

    def test_single_theme_portrait_article_uses_its_actual_scene_page(self):
        pages = [{"number": n, "logical": n, "width": 595.3, "height": 841.9,
                  "scene": {"id": "ratio-aggregation"} if n == 3 else None}
                 for n in range(1, 5)]
        source = {"id": "ratio-aggregation", "width": 1280, "height": 720,
                  "duration": 7, "checkpoints": [
                      {"time": 0}, {"time": 2.75}, {"time": 7}]}
        self.write_bundle({"light": (pages, [source])})
        fixture = load_fixture(self.html)
        self.assertEqual(list(fixture["manifest"]["themes"]), ["light"])
        self.assertEqual(len(fixture["manifest"]["themes"]["light"]["pages"]), 4)
        self.assertIsNone(scene_at(fixture, "light", 1))
        self.assertEqual(scene_at(fixture, "light", 3), source)
        self.assertEqual(fixture["manifest"]["themes"]["light"]["pages"][2]["height"], 841.9)

    def test_static_document_needs_no_authored_scene_or_second_theme(self):
        self.write_bundle({"light": ([{"number": 1, "logical": 1, "width": 800,
                                      "height": 450, "scene": None}], [])})
        fixture = load_fixture(self.html)
        self.assertEqual(fixture["timelines"], {"light": {}})
        self.assertIsNone(scene_at(fixture, "light", 1))

    def test_sole_animated_page_retains_each_themes_actual_checkpoints(self):
        page = {"number": 1, "logical": 1, "width": 800, "height": 450,
                "scene": {"id": "novel"}}
        editions = {theme: ([page], [{"id": "novel", "duration": duration,
                                    "checkpoints": [{"time": 0}, {"time": duration}]}])
                    for theme, duration in (("light", 4), ("dark", 6))}
        self.write_bundle(editions)
        fixture = load_fixture(self.html)
        self.assertEqual(scene_at(fixture, "light", 1)["duration"], 4)
        self.assertEqual(scene_at(fixture, "dark", 1)["duration"], 6)

    def test_missing_linked_scene_and_duplicate_bundle_are_rejected(self):
        page = {"number": 1, "scene": {"id": "absent"}}
        self.write_bundle({"light": ([page], [])})
        with self.assertRaisesRegex(ValueError, "missing linked scene absent"):
            load_fixture(self.html)
        self.write_bundle({"light": ([{"number": 1, "scene": None}], [])})
        self.html.write_text(self.html.read_text() * 2)
        with self.assertRaisesRegex(ValueError, "one embedded"):
            load_fixture(self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
