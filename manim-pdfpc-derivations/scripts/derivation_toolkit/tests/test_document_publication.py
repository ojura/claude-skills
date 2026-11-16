"""Keep compiled PDFs, their navigation and timelines consistent after updates."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fitz

from .. import build, launch
from ..catalog import Animation, Catalog
from ..paths import get_paths
from ..web.build import pdf_manifest


class DocumentFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="compiled document ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        (self.project / "presentations.json").write_text(json.dumps({
            "version": 2, "default": "example", "tex": {"packages": ["beamer.cls"]},
            "decks": [{"id": "example", "source": "example.tex", "title": "Example"}],
        }))
        self.deck = Catalog(self.project).get()
        self.paths = get_paths(self.deck)
        self.target = self.paths.pdf("light", "live")
        self.map = self.target.with_suffix(".presentation.json")
        self.vector = self.target.with_suffix(".vector.json")

    def output_bytes(self):
        return {p.name: p.read_bytes() for p in (self.target, self.map, self.vector) if p.exists()}

    def assert_output_preserved(self, previous):
        self.assertEqual(self.output_bytes(), previous)
        self.assertEqual(list(self.target.parent.glob(".derivation-publish-*")), [])

    def write_pdf(self, filename, labels):
        filename.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open() as document:
            for label in labels:
                page = document.new_page(width=320, height=180)
                page.insert_text((20, 40), label)
            document.save(filename)

    def write_map(self, filename, logical):
        data = {"version": 1, "logicalSlides": len(set(logical)), "title": "Authored title",
                "annotation": "Preserve this project field", "pages": [
                    {"number": number, "logical": slide, "title": f"Authored page {number}"}
                    for number, slide in enumerate(logical, 1)]}
        filename.write_text(json.dumps(data, indent=2) + "\n")
        return data


@unittest.skipUnless(shutil.which("pdflatex") and shutil.which("kpsewhich"), "Installed TeX is required")
class CompiledDocumentPublication(DocumentFixture):
    def setUp(self):
        super().setUp()
        self.environment = build.tex_environment(self.paths)
        if build.missing_tex_packages(self.environment, ("beamer.cls",)):
            self.skipTest("Installed Beamer is required")
        self.write_tex(["Accepted page"])

    def write_tex(self, labels, preamble=""):
        self.deck.source.write_text(
            r"\documentclass{beamer}" + preamble + r"\begin{document}"
            + "".join(r"\begin{frame}{" + text + r"}Body\end{frame}" for text in labels)
            + r"\end{document}" + "\n")

    def compile(self, deck=None):
        return build.build_one(deck or self.deck, "light", "live", self.environment, self.paths,
                               (deck.animation.assets if deck and deck.animation else None))

    def test_warm_cache_nofiles_cannot_reuse_previous_navigation(self):
        self.compile()
        previous = self.output_bytes()
        self.write_tex(["New first page", "New second page"], r"\nofiles")
        with self.assertRaisesRegex(RuntimeError, "navigation"):
            self.compile()
        self.assert_output_preserved(previous)
        with fitz.open(self.paths.latex_dir / "example-light-live/example-light-live.pdf") as document:
            self.assertEqual(len(document), 2)
            self.assertIn("New second page", document[1].get_text())

    def test_bad_navigation_is_rejected_before_replacing_the_pdf(self):
        self.compile()
        previous = self.output_bytes()
        self.write_tex(["Replacement first page", "Replacement second page"])
        real_run = subprocess.run
        cases = {
            "overlap": ([(1, 2), (2, 2)], 2),
            "gap": ([(2, 2)], 2),
            "count": ([(1, 1)], 1),
            "reverse": ([(2, 2), (1, 1)], 2),
            "empty_range": ([(2, 1), (1, 2)], 2),
        }
        for name, (ranges, count) in cases.items():
            calls = 0
            for filename, content in previous.items():
                (self.target.parent / filename).write_bytes(content)
            (self.paths.latex_dir / "example-light-live/example-light-live.nav").unlink(missing_ok=True)

            def run(command, *args, **kwargs):
                nonlocal calls
                result = real_run(command, *args, **kwargs)
                if Path(command[0]).name == "pdflatex" and result.returncode == 0:
                    calls += 1
                    if calls == 2:
                        output = Path(next(value.split("=", 1)[1] for value in command
                                           if value.startswith("-output-directory=")))
                        stem = next(value.split("=", 1)[1] for value in command if value.startswith("-jobname="))
                        navigation = "".join(rf"\headcommand{{\beamer@framepages{{{a}}}{{{b}}}}}" + "\n"
                                             for a, b in ranges)
                        navigation += rf"\headcommand{{\beamer@documentpages{{{count}}}}}" + "\n"
                        (output / (stem + ".nav")).write_text(navigation)
                return result

            with self.subTest(case=name), patch.object(build.subprocess, "run", side_effect=run):
                with self.assertRaises(RuntimeError):
                    self.compile()
                self.assertEqual(calls, 2)
                self.assert_output_preserved(previous)

    def test_failed_timeline_preparation_keeps_the_previous_complete_output(self):
        assets = self.project / "assets"
        (assets / "light").mkdir(parents=True)
        timeline = assets / "light/timeline.vector.json"
        timeline.write_text('{"version":1,"scenes":[],"generation":"old"}\n')
        deck = replace(self.deck, animation=Animation(self.project / "provider.py", assets, timeline.name))
        self.compile(deck)
        previous = self.output_bytes()
        self.write_tex(["Changed first page", "Changed second page"])
        timeline.write_text('{"version":1,"scenes":[],"generation":"new"}\n')
        copy = shutil.copyfile

        def copyfile(source, destination, *args, **kwargs):
            if Path(source) == timeline:
                raise OSError("timeline copy failed")
            return copy(source, destination, *args, **kwargs)

        with patch.object(shutil, "copyfile", side_effect=copyfile):
            with self.assertRaisesRegex(OSError, "timeline copy failed"):
                self.compile(deck)
        self.assert_output_preserved(previous)

    def test_failed_map_publication_restores_the_previous_pdf_and_map(self):
        self.compile()
        previous = self.output_bytes()
        self.write_tex(["Changed first page", "Changed second page"])
        rename = os.replace
        failed = False

        def replace_file(source, destination, *args, **kwargs):
            nonlocal failed
            if Path(destination) == self.map and not failed:
                failed = True
                raise OSError("page map publication failed")
            return rename(source, destination, *args, **kwargs)

        with patch.object(os, "replace", side_effect=replace_file):
            with self.assertRaisesRegex(OSError, "page map publication failed"):
                self.compile()
        self.assertTrue(failed)
        self.assert_output_preserved(previous)

    def test_successful_static_update_removes_a_stale_timeline(self):
        self.compile()
        self.vector.write_text('{"version":1,"scenes":[]}\n')
        unrelated = self.target.parent / "author-notes.txt"
        unrelated.write_text("Keep this file")
        self.write_tex(["First page", "Second page"])
        self.compile()
        with fitz.open(self.target) as document:
            self.assertEqual(len(document), 2)
        self.assertEqual([page["logical"] for page in json.loads(self.map.read_text())["pages"]], [1, 2])
        self.assertFalse(self.vector.exists())
        self.assertEqual(unrelated.read_text(), "Keep this file")


class ExplicitDocumentPublication(DocumentFixture):
    def setUp(self):
        super().setUp()
        self.write_pdf(self.target, ["Accepted page"])
        self.write_map(self.map, [1])
        self.vector.write_text('{"version":1,"scenes":[]}\n')
        self.source = self.project / "external/report.pdf"
        self.write_pdf(self.source, ["New first page", "New second page"])

    def test_explicit_pdf_preserves_the_authored_map_and_removes_obsolete_timeline(self):
        source_map = self.source.with_suffix(".presentation.json")
        self.write_map(source_map, [1, 1])
        launch.prepare_document(self.deck, "light", self.paths, pdf=self.source)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.map.read_bytes(), source_map.read_bytes())
        self.assertFalse(self.vector.exists())
        manifest = pdf_manifest(self.target, None, "light")
        self.assertEqual(manifest["logicalSlides"], 1)
        self.assertEqual([page["logical"] for page in manifest["pages"]], [1, 1])

    def test_missing_source_map_gets_fresh_physical_page_mapping(self):
        launch.prepare_document(self.deck, "light", self.paths, pdf=self.source)
        manifest = pdf_manifest(self.target, None, "light")
        self.assertEqual(manifest["logicalSlides"], 2)
        self.assertEqual([page["logical"] for page in manifest["pages"]], [1, 2])

    def test_invalid_source_map_preserves_the_previous_set(self):
        previous = self.output_bytes()
        self.write_map(self.source.with_suffix(".presentation.json"), [1])
        with self.assertRaisesRegex(ValueError, "each compiled PDF page"):
            launch.prepare_document(self.deck, "light", self.paths, pdf=self.source)
        self.assert_output_preserved(previous)

    def test_map_copy_failure_preserves_the_previous_set(self):
        previous = self.output_bytes()
        source_map = self.source.with_suffix(".presentation.json")
        self.write_map(source_map, [1, 1])
        copy = shutil.copyfile

        def copyfile(source, destination, *args, **kwargs):
            if Path(source) == source_map:
                raise OSError("source map copy failed")
            return copy(source, destination, *args, **kwargs)

        with patch.object(shutil, "copyfile", side_effect=copyfile):
            with self.assertRaisesRegex(OSError, "source map copy failed"):
                launch.prepare_document(self.deck, "light", self.paths, pdf=self.source)
        self.assert_output_preserved(previous)

    def test_preparing_the_existing_output_preserves_its_sidecars(self):
        previous = self.output_bytes()
        launch.prepare_document(self.deck, "light", self.paths, pdf=self.target)
        self.assert_output_preserved(previous)


class CompiledPdfReader(DocumentFixture):
    def test_each_available_reader_counts_actual_pdf_pages_and_rejects_invalid_input(self):
        self.write_pdf(self.target, ["First page", "Second page"])
        invalid = self.root / "invalid.pdf"
        invalid.write_text("This is not a PDF.")
        which = shutil.which
        checked = 0
        for backend in ("pdfinfo", "texlua"):
            if not which(backend):
                continue
            checked += 1

            def select(name, *args, **kwargs):
                return which(name, *args, **kwargs) if name == backend else None

            with self.subTest(backend=backend), patch.object(build.shutil, "which", side_effect=select):
                self.assertEqual(build.pdf_page_count(self.target, os.environ.copy()), 2)
                with self.assertRaisesRegex(RuntimeError, "Could not read compiled PDF pages"):
                    build.pdf_page_count(invalid, os.environ.copy())
        if not checked:
            self.skipTest("An installed PDF reader is required")

    def test_missing_reader_reports_the_required_installed_tools(self):
        self.write_pdf(self.target, ["One page"])
        with patch.object(build.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "pdfinfo or TeX's texlua"):
                build.pdf_page_count(self.target, os.environ.copy())


if __name__ == "__main__":
    unittest.main(verbosity=2)
