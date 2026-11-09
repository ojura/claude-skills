"""Real PDF inputs retain their theme, timeline and page map across CI delivery paths."""
from contextlib import redirect_stdout
import base64
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import fitz

from ..catalog import Catalog
from ..ci.authoring import compiled_inputs, main, mounted_inputs
from ..package_assets import collect_assets


class DeliveryInputs(unittest.TestCase):
    def test_downloaded_external_editions_feed_html_and_complete_native_inputs(self):
        with tempfile.TemporaryDirectory(prefix="delivery inputs ") as temporary, patch.dict(
            os.environ, {"DERIVATION_CACHE_ROOT": "", "DERIVATION_BUILD_DIR": ""}
        ):
            root = Path(temporary)
            project, assets = root / "project", root / "downloaded artifacts"
            project.mkdir()
            (assets / "different names").mkdir(parents=True)
            (project / "presentations.json").write_text(json.dumps({
                "version": 2, "default": "article", "decks": [
                    {"id": "article", "title": "Portrait article", "source": "article.pdf",
                     "build": {"kind": "external"}},
                    {"id": "static", "title": "Static slides", "source": "static.tex"},
                ]}))
            vendor = project / "build/tools/html5/node_modules/pdfjs-dist"
            (vendor / "build").mkdir(parents=True)
            (vendor / "package.json").write_text('{"version":"5.1.91"}')
            # The packaging boundary copies vendor bytes; browser execution is a
            # separate real-PDF.js acceptance gate.
            for name in ("pdf.min.mjs", "pdf.worker.min.mjs"):
                (vendor / "build" / name).write_text("export {};\n")
            (vendor / "LICENSE").write_text("Fixture vendor license\n")
            editions = {}
            for theme in ("light", "dark"):
                pdf = assets / "different names" / f"portrait-{theme}.pdf"
                with fitz.open() as document:
                    page = document.new_page(width=200, height=300)
                    page.insert_text((20, 30), "Actual article " + theme)
                    page.insert_link({"kind": fitz.LINK_URI, "uri": "pdfpc-vector:ratio",
                                      "from": fitz.Rect(20, 100, 180, 190)})
                    document.save(pdf)
                vectors = pdf.with_suffix(".motion.json")
                vectors.write_text(json.dumps({"version": 1, "scenes": [{
                    "id": "ratio", "width": 1280, "height": 720, "duration": 2,
                    "background": [1, 1, 1, 1], "objects": [],
                    "checkpoints": [{"time": 0, "label": "Start"}, {"time": 2, "label": "End"}],
                }]}))
                page_map = pdf.with_suffix(".map.json")
                page_map.write_text(json.dumps({"version": 1, "logicalSlides": 1,
                    "pages": [{"number": 1, "logical": 1, "title": "Authored appendix"}]}))
                editions[theme] = {key: "/assets/" + value.relative_to(assets).as_posix()
                                   for key, value in {"pdf": pdf, "vectors": vectors,
                                                      "page_map": page_map}.items()}
                target = assets / "static" / f"static-{theme}-live.pdf"
                target.parent.mkdir(exist_ok=True)
                with fitz.open() as document:
                    page = document.new_page(width=320, height=180)
                    page.insert_text((20, 30), "Actual static slide " + theme)
                    document.save(target)
                for mode in ("steps", "handout"):
                    target.with_name(f"static-{theme}-{mode}.pdf").write_bytes(target.read_bytes())
            catalog = Catalog(project)
            for themes in (("light",), ("light", "dark")):
                with self.subTest(themes=themes):
                    declared = {"article": {theme: editions[theme] for theme in themes}}
                    mapping = assets / "inputs.json"
                    mapping.write_text(json.dumps(declared))
                    resolved = mounted_inputs(mapping, assets)
                    complete = compiled_inputs(catalog, assets, resolved)
                    self.assertEqual(set(complete), {"article", "static"})
                    self.assertEqual(list(complete["article"]), list(themes))
                    self.assertEqual(set(complete["static"]), {"light", "dark"})
                    output = root / ("html-" + "-".join(themes))
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["html", "--project", str(project),
                            "--deck", "article", "--assets", str(assets),
                            "--inputs", str(mapping), "--output-dir", str(output)]), 0)
                    html = (output / "index.html").read_text()
                    payload = json.loads(re.search(r'id="derivation-bundle">(.*?)</script>',
                                                   html, re.DOTALL).group(1))
                    self.assertEqual(list(payload["manifest"]["themes"]), list(themes))
                    for theme, entry in payload["manifest"]["themes"].items():
                        page = entry["pages"][0]
                        self.assertEqual((page["width"], page["height"]), (200, 300))
                        self.assertEqual(page["title"], "Authored appendix")
                        self.assertEqual(page["scene"]["id"], "ratio")
                        self.assertEqual(base64.b64decode(payload["assets"][entry["pdf"]]),
                                         Path(complete["article"][theme]["pdf"]).read_bytes())
                    checks = root / ("checks-" + "-".join(themes))
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["artifacts", "--project", str(project),
                            "--assets", str(assets), "--inputs", str(mapping),
                            "--output-dir", str(checks)]), 0)
                    self.assertEqual(json.loads((checks / "resolved-inputs.json").read_text()), resolved)
                    fixtures = json.loads((checks / "runtime-fixtures.json").read_text())
                    article = next(item for item in fixtures["catalog"] if item["id"] == "article")
                    self.assertEqual(list(article["themes"]), list(themes))
                    self.assertTrue(article["animation"])
            for malformed in ([], ["article"], "article", 42):
                with self.subTest(malformed=malformed):
                    mapping.write_text(json.dumps(malformed))
                    with self.assertRaisesRegex(ValueError, "object keyed by deck ID"):
                        mounted_inputs(mapping, assets)
                    with self.assertRaisesRegex(ValueError, "object keyed by deck ID"):
                        collect_assets(catalog, assets, inputs=malformed)


if __name__ == "__main__":
    unittest.main()
