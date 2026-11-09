"""Distinct HTML outputs for one deck must retain their own downloadable archives."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import fitz

from ..catalog import Catalog
from ..verify.browser_fixture import load_fixture
from ..web.build import build_bundle


class HtmlArchives(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="HTML archives ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "presentations.json").write_text(json.dumps({
            "version": 2, "default": "article", "decks": [{
                "id": "article", "title": "Independent article",
                "source": "article.pdf", "build": {"kind": "external"},
            }],
        }))
        self.catalog = Catalog(self.project)
        self.vendor = self.root / "pdfjs"
        (self.vendor / "build").mkdir(parents=True)
        (self.vendor / "package.json").write_text('{"version":"5.1.91"}')
        # This test exercises real PDF packaging and archive identity. The
        # browser acceptance gate exercises the actual PDF.js implementation.
        for name in ("pdf.min.mjs", "pdf.worker.min.mjs"):
            (self.vendor / "build" / name).write_text("export {};\n")
        (self.vendor / "LICENSE").write_text("Fixture vendor license\n")
        self.complete = self.document("complete", 2)
        self.excerpt = self.document("excerpt", 1)

    def document(self, name, count):
        target = self.root / f"{name}.pdf"
        with fitz.open() as document:
            for number in range(count):
                page = document.new_page(width=200, height=300)
                page.insert_text((20, 30), f"{name} page {number + 1}")
            document.save(target)
        return target

    def build(self, pdf, output=None):
        with patch.dict("os.environ", {"DERIVATION_BUILD_DIR": ""}), \
                redirect_stdout(io.StringIO()):
            return build_bundle(
                self.catalog, self.catalog.get(), output=output, pdf=pdf,
                pdfjs_dist=self.vendor, themes=("light",),
                cache_root=self.project / "build",
            )

    def assert_archive(self, archive, name, html, page_count):
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(bundle.read(f"{name}/index.html"), html.read_bytes())
            self.assertTrue(all(member.startswith(name + "/")
                                for member in bundle.namelist()))
        pages = load_fixture(html)["manifest"]["themes"]["light"]["pages"]
        self.assertEqual(len(pages), page_count)

    def test_explicit_outputs_for_one_deck_keep_both_archives(self):
        delivery = self.root / "delivery"
        complete = self.build(self.complete, delivery / "article-html5")
        complete_zip = delivery / "article-html5.zip"
        accepted_archive = complete_zip.read_bytes()
        excerpt = self.build(self.excerpt, delivery / "one-page-html5")
        self.assertEqual(complete_zip.read_bytes(), accepted_archive,
                         "Building an excerpt replaced the complete presentation ZIP")
        self.assert_archive(complete_zip, "article-html5", complete, 2)
        self.assert_archive(delivery / "one-page-html5.zip", "one-page-html5", excerpt, 1)

    def test_default_output_keeps_the_deck_archive_name(self):
        html = self.build(self.complete)
        self.assertEqual(html.parent.name, "html5")
        self.assert_archive(html.parent.parent / "article-html5.zip", "article-html5", html, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
