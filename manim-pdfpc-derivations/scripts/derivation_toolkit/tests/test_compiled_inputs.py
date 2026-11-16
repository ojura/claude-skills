"""Reject malformed real PDF/timeline inputs before changing delivered files."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fitz

from ..catalog import Catalog
from .. import pdf_inputs
from ..package_assets import collect_assets, stage_assets
from ..timeline import load_timeline
from ..web.build import build_bundle, pdf_manifest


def timeline(identifier="example"):
    return {"version": 1, "scenes": [{
        "id": identifier, "width": 160, "height": 90, "duration": 1,
        "background": [1, 1, 1, 1],
        "checkpoints": [{"time": 0, "label": "Start"}, {"time": 1, "label": "End"}],
        "objects": [{"id": "shape", "commands": "ML", "keyframes": [{
            "time": 0, "coords": [0, 0, 100, 50], "fill": [0, 0, 0, 0],
            "stroke": [1, 0, 0, 1], "stroke_width": 2,
        }]}],
    }]}


def file_tree(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


class CompiledInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="compiled inputs ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "presentations.json").write_text(json.dumps({
            "version": 2, "default": "example", "decks": [{
                "id": "example", "title": "Independent input", "source": "example.pdf",
                "build": {"kind": "external"},
            }],
        }))
        self.catalog = Catalog(self.project)
        self.pdf = self.root / "input.pdf"
        self.vectors = self.root / "input.vector.json"
        self.static = self.root / "static.pdf"
        self.write_pdf(self.static)
        self.write_pdf(self.pdf, "example")
        self.write_timeline(timeline())
        self.vendor = self.root / "pdfjs"
        (self.vendor / "build").mkdir(parents=True)
        (self.vendor / "package.json").write_text('{"version":"5.1.91"}')
        for name in ("pdf.min.mjs", "pdf.worker.min.mjs"):
            (self.vendor / "build" / name).write_text("export {};\n")
        (self.vendor / "LICENSE").write_text("Fixture vendor license\n")

    def write_pdf(self, path, scene=None, *, compressed=False, rectangle=None):
        if compressed:
            uri = ("pdfpc-vector:" + scene).encode("utf-8").hex()
            source = path.with_suffix(".tex")
            source.write_text(
                r"\pdfoutput=1\pdfminorversion=5\pdfobjcompresslevel=3\pdfcompresslevel=9"
                r"\pdfpagewidth=320bp\pdfpageheight=180bp\hoffset=-1in\voffset=-1in"
                r"\hsize=320bp\vsize=180bp\nopagenumbers"
                r"\vbox{\kern40bp\hbox{\kern30bp"
                r"\pdfannot width160bp height90bp depth0bp{/Subtype/Link /A<< /S/URI /URI <"
                + uri + r"> >>}\vrule width0bp height90bp}\vss}\eject\end" + "\n")
            result = subprocess.run(["pdftex", "-interaction=nonstopmode", "-halt-on-error", source.name],
                                    cwd=path.parent, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn(uri.encode(), path.read_bytes(), "The annotation must be in an object stream")
            with fitz.open(path) as document:
                self.assertTrue(any(document.xref_get_key(index, "Type") == ("name", "/ObjStm")
                                    for index in range(1, document.xref_length())))
            return
        with fitz.open() as document:
            page = document.new_page(width=320, height=180)
            page.insert_text((15, 20), "Actual PDF input")
            if scene is not None:
                page.insert_link({"kind": fitz.LINK_URI, "from": rectangle or fitz.Rect(30, 40, 190, 130),
                                  "uri": "pdfpc-vector:" + scene})
            document.save(path, deflate=compressed)

    def write_timeline(self, data):
        self.vectors.write_text(json.dumps(data, ensure_ascii=False) + "\n")

    def inputs(self, pdf=None, vectors=None):
        item = {"pdf": str(pdf or self.pdf)}
        if vectors is not None:
            item["vectors"] = str(vectors)
        return {"example": {"light": item}}

    def collect(self, pdf=None, vectors=None):
        return collect_assets(self.catalog, self.root, selection="example",
                              inputs=self.inputs(pdf, vectors))

    def html(self, pdf=None, vectors=None):
        with redirect_stdout(io.StringIO()):
            return build_bundle(self.catalog, self.catalog.get(), pdf=pdf or self.pdf,
                                vectors=vectors, themes=("light",), pdfjs_dist=self.vendor,
                                output=self.root / "delivery/example-html5",
                                cache_root=self.project / "build")

    def test_static_and_valid_animation_remain_accepted_without_rewriting_inputs(self):
        before = {path: path.read_bytes() for path in (self.static, self.pdf, self.vectors)}
        rows, files = self.collect(self.static)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(files), 1)
        rows, files = self.collect(vectors=self.vectors)
        self.assertEqual(len(files), 2)
        self.assertEqual(len(pdf_manifest(self.pdf, self.vectors, "light")["pages"]), 1)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_native_collection_rejects_an_animation_link_without_a_timeline(self):
        with self.assertRaisesRegex(ValueError, "[Tt]imeline|[Ss]cene"):
            self.collect()

    def test_native_collection_rejects_invalid_pdf_and_json_bytes(self):
        bad = self.root / "bad.pdf"
        bad.write_bytes(b"This is a nonempty file, not a PDF.\n")
        with self.subTest(case="invalid PDF"):
            with self.assertRaisesRegex(ValueError, "PDF"):
                self.collect(bad)
        self.vectors.write_text("not JSON\n")
        with self.subTest(case="invalid JSON"):
            with self.assertRaises(ValueError):
                self.collect(vectors=self.vectors)

    def test_native_collection_rejects_unknown_scene_and_invalid_keyframes(self):
        unknown = timeline("a different scene")
        broken = timeline()
        del broken["scenes"][0]["objects"][0]["keyframes"][0]["coords"]
        for label, data in (("unknown scene", unknown), ("missing geometry", broken)):
            with self.subTest(case=label):
                self.write_timeline(data)
                with self.assertRaises(ValueError):
                    self.collect(vectors=self.vectors)

    def test_html_rejects_invalid_keyframes_without_replacing_accepted_html_or_zip(self):
        self.html(self.static)
        before = file_tree(self.root / "delivery")
        data = timeline()
        del data["scenes"][0]["objects"][0]["keyframes"][0]["coords"]
        self.write_timeline(data)
        with self.assertRaises(ValueError):
            self.html(vectors=self.vectors)
        self.assertEqual(file_tree(self.root / "delivery"), before)

    def test_native_staging_rejects_invalid_input_before_touching_previous_files(self):
        destination = self.root / "native"
        stage_assets(self.catalog, self.root, destination, selection="example",
                     inputs=self.inputs(self.static))
        before = file_tree(destination)
        self.vectors.write_text("not JSON\n")
        with self.assertRaises(ValueError):
            stage_assets(self.catalog, self.root, destination, selection="example",
                         inputs=self.inputs(vectors=self.vectors))
        self.assertEqual(file_tree(destination), before)

    def test_compressed_pdf_and_escaped_scene_id_use_actual_annotation_objects(self):
        identifier = 'ratio ("A") \\ source'
        self.pdf.unlink()
        self.write_pdf(self.pdf, identifier, compressed=True)
        self.write_timeline(timeline(identifier))
        before = (self.pdf.read_bytes(), self.vectors.read_bytes())
        self.collect(vectors=self.vectors)
        pages = pdf_manifest(self.pdf, self.vectors, "light")["pages"]
        self.assertEqual(pages[0]["scene"]["id"], identifier)
        self.assertEqual((self.pdf.read_bytes(), self.vectors.read_bytes()), before)

    def test_different_aspect_ratio_region_is_valid_and_keeps_its_authored_rectangle(self):
        self.pdf.unlink()
        self.write_pdf(self.pdf, "example", rectangle=fitz.Rect(30, 40, 190, 100))
        self.collect(vectors=self.vectors)
        pages = pdf_manifest(self.pdf, self.vectors, "light")["pages"]
        self.assertEqual(pages[0]["scene"]["rect"], [30, 40, 190, 100])

    def test_each_installed_reader_sees_the_first_compressed_annotation(self):
        identifier = 'ratio ("A") \\ source'
        self.pdf.unlink()
        self.write_pdf(self.pdf, identifier, compressed=True)
        self.write_timeline(timeline(identifier))
        for name, reader in (("Poppler", pdf_inputs.poppler_links), ("TeX", pdf_inputs.tex_links)):
            with self.subTest(reader=name):
                try:
                    pages = reader(self.pdf)
                except pdf_inputs.ReaderUnavailable as error:
                    self.skipTest(str(error))
                self.assertEqual(pages, [["pdfpc-vector:" + identifier]])
        with patch.object(pdf_inputs, "poppler_links", side_effect=pdf_inputs.ReaderUnavailable("forced TeX")):
            self.assertEqual(pdf_inputs.validate_compiled_input(self.pdf, self.vectors), 1)
            with self.assertRaisesRegex(ValueError, "matching timeline"):
                pdf_inputs.validate_compiled_input(self.pdf)

    def test_each_reader_keeps_page_order_and_all_uri_annotations(self):
        self.pdf.unlink()
        with fitz.open() as document:
            for page_number, identifiers in enumerate((('https://example.invalid/help', 'pdfpc-vector:example'),
                                                       ('pdfpc-vector:missing',))):
                page = document.new_page(width=320, height=180)
                page.insert_text((10, 20), str(page_number))
                for index, identifier in enumerate(identifiers):
                    page.insert_link({"kind": fitz.LINK_URI, "uri": identifier,
                                      "from": fitz.Rect(30, 40 + 50 * index, 100, 75 + 50 * index)})
            document.save(self.pdf)
        expected = [['https://example.invalid/help', 'pdfpc-vector:example'], ['pdfpc-vector:missing']]
        for name, reader in (("Poppler", pdf_inputs.poppler_links), ("TeX", pdf_inputs.tex_links)):
            with self.subTest(reader=name):
                pages = reader(self.pdf)
                self.assertEqual(len(pages), len(expected))
                for actual, wanted in zip(pages, expected):
                    self.assertCountEqual(actual, wanted)
                with patch.object(pdf_inputs, "inspect_pdf", side_effect=reader):
                    with self.assertRaisesRegex(ValueError, "page 2.*missing"):
                        pdf_inputs.validate_compiled_input(self.pdf, self.vectors)

    def test_each_reader_rejects_invalid_pdf_without_a_fallback_success(self):
        invalid = self.root / "invalid.pdf"
        invalid.write_text("Not a PDF")
        for name, reader in (("Poppler", pdf_inputs.poppler_links), ("TeX", pdf_inputs.tex_links)):
            with self.subTest(reader=name), self.assertRaisesRegex(ValueError, "PDF"):
                reader(invalid)
        with patch.object(pdf_inputs, "poppler_links", side_effect=ValueError("invalid PDF")), \
                patch.object(pdf_inputs, "tex_links") as fallback:
            with self.assertRaisesRegex(ValueError, "invalid PDF"):
                pdf_inputs.inspect_pdf(invalid)
            fallback.assert_not_called()

    def test_timeline_strings_reject_nul_and_lone_surrogates(self):
        for value in ('bad\x00suffix', 'bad\ud800suffix', 'bad\udfffsuffix'):
            for member in ('scene', 'object', 'label'):
                with self.subTest(value=repr(value), member=member):
                    data = timeline()
                    scene = data['scenes'][0]
                    if member == 'scene':
                        scene['id'] = value
                    elif member == 'object':
                        scene['objects'][0]['id'] = value
                    else:
                        scene['checkpoints'][0]['label'] = value
                    self.vectors.write_text(json.dumps(data, ensure_ascii=True))
                    with self.assertRaisesRegex(ValueError, 'string|Unicode|NUL'):
                        load_timeline(self.vectors)

    def test_escaped_and_literal_unicode_timelines_preserve_their_identifiers(self):
        identifier = 'θ / 例 / 🧮 / ("A") \\ source'
        for escaped in (False, True):
            with self.subTest(escaped=escaped):
                data = timeline(identifier)
                data['scenes'][0]['objects'][0]['id'] = '符号😀'
                data['scenes'][0]['checkpoints'][0]['label'] = 'Start θ😀'
                self.vectors.write_text(json.dumps(data, ensure_ascii=escaped), encoding='utf-8')
                before = self.vectors.read_bytes()
                scenes = load_timeline(self.vectors)
                self.assertEqual(list(scenes), [identifier])
                self.assertEqual(scenes[identifier], data['scenes'][0])
                self.assertEqual(self.vectors.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
