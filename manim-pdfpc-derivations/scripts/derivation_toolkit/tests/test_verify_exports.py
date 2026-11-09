#!/usr/bin/env python3
"""Mutation controls for the source-to-committed-figure release gate."""
from __future__ import annotations

from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.dont_write_bytecode = True

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, ByteStringObject, DecodedStreamObject,
                          DictionaryObject, FloatObject, NameObject,
                          NumberObject, TextStringObject)

from ..verify.verify_exports import (COORDINATE_DECIMALS, DRAWING_WARNING_LIMIT, ExportMismatch,
                            compare_pdf, compare_trees, external_output,
                            first_difference, main)


def numbers(values):
    return ArrayObject([FloatObject(value) if isinstance(value, float) else NumberObject(value)
                        for value in values])


def write_fixture(filename, *, compressed=False, date="D:20260101000000Z",
                  identifier=b"first-document-id", unused=False, duplicate_shared=False,
                  outline=500, width=500, bbox=700, unicode_hex="0041",
                  matrix=.001, color="0", producer="export fixture",
                  contents=None, glyph_program=None):
    writer = PdfWriter()
    if unused:
        writer._add_object(DictionaryObject({NameObject("/Unused"): NumberObject(42)}))
    page = writer.add_blank_page(width=200, height=100)

    def stream(data):
        value = DecodedStreamObject()
        value.set_data(data)
        return writer._add_object(value.flate_encode() if compressed else value)

    glyph = stream(glyph_program if glyph_program is not None else
                   f"500 0 0 0 500 700 d1\n0 0 m {outline} 0 l 250 700 l h f\n".encode())
    cmap = stream((
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Fixture def\n/CMapType 2 def\n1 begincodespacerange\n"
        "<00> <ff>\nendcodespacerange\n1 beginbfchar\n"
        f"<41> <{unicode_hex}>\nendbfchar\nendcmap\n"
        "CMapName currentdict /CMap defineresource pop\nend\nend\n").encode())
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type3"),
        NameObject("/FontBBox"): numbers([0, 0, 500, bbox]),
        NameObject("/FontMatrix"): numbers([matrix, 0, 0, .001, 0, 0]),
        NameObject("/CharProcs"): DictionaryObject({NameObject("/A"): glyph}),
        NameObject("/Encoding"): DictionaryObject({
            NameObject("/Type"): NameObject("/Encoding"),
            NameObject("/Differences"): ArrayObject([NumberObject(65), NameObject("/A")])}),
        NameObject("/FirstChar"): NumberObject(65), NameObject("/LastChar"): NumberObject(65),
        NameObject("/Widths"): numbers([width]), NameObject("/Resources"): DictionaryObject(),
        NameObject("/ToUnicode"): cmap,
    }))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Contents")] = stream(contents if contents is not None else
        f"q {color} g BT /F1 12 Tf 1 0 0 1 10 10 Tm (A) Tj ET Q\n".encode())

    def cycle():
        value = DictionaryObject({NameObject("/Value"): NumberObject(7)})
        reference = writer._add_object(value)
        value[NameObject("/Self")] = reference
        return reference

    shared = cycle()
    writer.root_object[NameObject("/SharedProof")] = ArrayObject(
        [shared, cycle() if duplicate_shared else shared])
    writer.add_metadata({"/Producer": producer, "/CreationDate": date, "/ModDate": date})
    writer._ID = ArrayObject([ByteStringObject(identifier), ByteStringObject(identifier)])
    writer.write(filename)


class ExportConsistency(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="psr-export-consistency-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.original, self.changed = self.directory / "original.pdf", self.directory / "changed.pdf"
        write_fixture(self.original)

    def test_dates_document_ids_compression_and_xref_renumbering_are_ignored(self):
        write_fixture(self.changed, compressed=True, date="D:20261109123456Z",
                      identifier=b"another-document-id", unused=True)
        self.assertTrue(compare_pdf(self.original, self.changed)["equal"])

    def test_geometry_unicode_advances_bounds_matrix_and_color_are_compared(self):
        for mutation in ({"outline": 501}, {"unicode_hex": "0042"}, {"width": 501},
                         {"bbox": 699}, {"matrix": .001001}, {"color": ".1"}):
            with self.subTest(mutation=mutation):
                write_fixture(self.changed, **mutation)
                result = compare_pdf(self.original, self.changed)
                self.assertFalse(result["equal"])
                self.assertTrue(result["difference"]["path"])
                self.assertEqual(result["release_status"], "error")

    def test_small_drawing_operands_warn_without_losing_exact_graph_evidence(self):
        self.assertEqual(DRAWING_WARNING_LIMIT, Decimal(1).scaleb(-COORDINATE_DECIMALS - 2))
        for mutation in ({"color": "0.000001"}, {"outline": "500.000001"},
                         {"outline": "499.999999"}):
            with self.subTest(mutation=mutation):
                write_fixture(self.changed, **mutation)
                result = compare_pdf(self.original, self.changed)
                self.assertFalse(result["equal"])
                self.assertEqual(result["release_status"], "warning")
                self.assertNotEqual(result["committed_graph_sha256"], result["generated_graph_sha256"])
                changes = [change for stream in result["classified_differences"]
                           for change in stream["operand_changes"]]
                self.assertTrue(changes)
                self.assertTrue(all(change["kind"] == "numeric-operand" for change in changes))
                self.assertTrue(all(abs(Decimal(change["delta"])) == DRAWING_WARNING_LIMIT for change in changes))

    def test_signed_zero_is_a_visible_equivalent_drawing_warning(self):
        write_fixture(self.changed, color="-0")
        result = compare_pdf(self.original, self.changed)
        self.assertFalse(result["equal"])
        self.assertEqual(result["release_status"], "warning")
        self.assertEqual(result["classified_differences"][0]["operand_changes"], [])
        self.assertEqual(result["classified_differences"][0]["reason"], "identical parsed drawing operations")

    def test_numeric_limit_is_not_applied_to_resource_or_metadata_values(self):
        for mutation in ({"matrix": .0010001}, {"bbox": 700.000001}, {"width": 500.000001},
                         {"outline": "500.000002"}, {"color": "0.000002"}):
            with self.subTest(mutation=mutation):
                write_fixture(self.changed, **mutation)
                self.assertEqual(compare_pdf(self.original, self.changed)["release_status"], "error")

    def test_numeric_text_bytes_are_never_classified_as_numeric_operands(self):
        for operator in (b"Tj", b"TJ"):
            with self.subTest(operator=operator):
                def content(value):
                    operand = b"(" + value + b")"
                    if operator == b"TJ":
                        operand = b"[" + operand + b" 0 (A)]"
                    return b"BT /F1 12 Tf " + operand + b" " + operator + b" ET\n"
                write_fixture(self.original, contents=content(b"A0.000000"))
                write_fixture(self.changed, contents=content(b"A0.000001"))
                result = compare_pdf(self.original, self.changed)
                self.assertEqual(result["release_status"], "error")
                changes = result["classified_differences"][0]["operand_changes"]
                self.assertEqual(changes[0]["reason"], "text bytes changed")

    def test_numeric_array_operands_warn_but_operator_and_array_structure_changes_fail(self):
        old = b"BT /F1 12 Tf [(A) 0 (A)] TJ ET\n"
        write_fixture(self.original, contents=old)
        write_fixture(self.changed, contents=old.replace(b" 0 ", b" 0.000001 "))
        self.assertEqual(compare_pdf(self.original, self.changed)["release_status"], "warning")
        for changed in (old.replace(b"(A)]", b"]"), old.replace(b" TJ ", b" Tj ")):
            write_fixture(self.changed, contents=changed)
            self.assertEqual(compare_pdf(self.original, self.changed)["release_status"], "error")

    def test_later_glyph_error_is_not_hidden_by_an_earlier_numeric_warning(self):
        write_fixture(self.changed, color="0.000001",
                      glyph_program=b"500 0 0 0 500 700 d1\n0 0 m 500 0 l 250 700 l h S\n")
        result = compare_pdf(self.original, self.changed)
        self.assertEqual(result["release_status"], "error")
        self.assertEqual({item["severity"] for item in result["classified_differences"]}, {"warning", "error"})

    def test_discrete_graphics_and_text_selectors_require_exact_values(self):
        for operator in ("J", "j", "Tr"):
            def content(value):
                if operator == "Tr":
                    return f"BT /F1 12 Tf {value} Tr (A) Tj ET\n".encode()
                return f"q {value} {operator} 0 0 m 1 1 l S Q\n".encode()
            write_fixture(self.original, contents=content("0"))
            for value in ("0.000001", "-0.000001", "1"):
                with self.subTest(operator=operator, value=value):
                    write_fixture(self.changed, contents=content(value))
                    result = compare_pdf(self.original, self.changed)
                    self.assertEqual(result["release_status"], "error")
                    changes = result["classified_differences"][0]["operand_changes"]
                    self.assertEqual(changes[0]["operator"], operator)
                    self.assertEqual(changes[0]["severity"], "error")

    def test_marked_content_metadata_and_unparsed_streams_fail(self):
        old = b"/Span << /ActualText (0.000000) >> BDC BT /F1 12 Tf (A) Tj ET EMC\n"
        write_fixture(self.original, contents=old)
        write_fixture(self.changed, contents=old.replace(b"0.000000", b"0.000001"))
        self.assertEqual(compare_pdf(self.original, self.changed)["release_status"], "error")
        write_fixture(self.changed, contents=b"BT /F1 12 Tf [")
        self.assertEqual(compare_pdf(self.original, self.changed)["release_status"], "error")

    def test_stream_difference_reports_exact_active_path_and_first_byte(self):
        write_fixture(self.changed, color="1")
        result = compare_pdf(self.original, self.changed)
        self.assertFalse(result["equal"])
        difference = result["difference"]
        expected = "/Root/Pages/Kids[0]/Contents"
        self.assertEqual(difference["object_paths"],
                         {"committed": expected, "generated": expected})
        self.assertTrue(difference["path"].endswith(".data"))
        self.assertEqual(difference["byte_offset"], 2)
        self.assertEqual(difference["committed_byte"], ord("0"))
        self.assertEqual(difference["generated_byte"], ord("1"))
        self.assertEqual(difference["committed"]["bytes"], difference["generated"]["bytes"])
        self.assertNotEqual(difference["committed"]["sha256"], difference["generated"]["sha256"])
        self.assertTrue(bytes.fromhex(difference["committed_context"]["hex"]).startswith(b"q 0 g"))
        self.assertTrue(bytes.fromhex(difference["generated_context"]["hex"]).startswith(b"q 1 g"))

    def test_byte_diagnostics_are_bounded_and_distinguish_binary_and_eof(self):
        old, new = b"a" * 1000 + b"\xff" + b"z" * 1000, b"a" * 1000 + b"\x00" + b"z" * 1000
        difference = first_difference(old, new)
        self.assertEqual(difference["byte_offset"], 1000)
        for key, value in (("committed_context", old), ("generated_context", new)):
            context = difference[key]
            self.assertEqual((context["start"], context["end"]), (968, 1064))
            self.assertEqual(bytes.fromhex(context["hex"]), value[968:1064])
            self.assertLessEqual(context["end"] - context["start"], 96)
        for old, new in ((b"", b"x"), (b"prefix", b"prefixX"), (b"prefixX", b"prefix")):
            with self.subTest(old=old, new=new):
                difference = first_difference(old, new)
                self.assertEqual(difference["byte_offset"], min(len(old), len(new)))
                self.assertEqual(difference["committed_byte"], None if len(old) < len(new) else ord("X"))
                self.assertEqual(difference["generated_byte"],
                                 None if len(new) < len(old) else (ord("x") if not old else ord("X")))

    def test_shared_references_and_cycles_remain_structural(self):
        write_fixture(self.changed, duplicate_shared=True)
        self.assertFalse(compare_pdf(self.original, self.changed)["equal"])

    def test_nonvolatile_document_information_is_compared(self):
        write_fixture(self.changed, producer="changed producer")
        self.assertFalse(compare_pdf(self.original, self.changed)["equal"])

    def trees(self):
        committed, fresh = self.directory / "committed", self.directory / "fresh"
        committed.mkdir()
        metadata = {"version": 1, "scenes": [{"id": "test", "poster": "full.pdf",
                     "steps": [{"figure": "compact.pdf", "full_figure": "full.pdf"}]}]}
        (committed / "steps.json").write_text(json.dumps(metadata) + "\n")
        (committed / "steps.tex").write_text("% exact scene metadata\n")
        (committed / "palettes.tex").write_text("% exact palette\n")
        for theme in ("light", "dark"):
            folder = committed / theme
            folder.mkdir()
            (folder / "derivations.vector.json").write_text('{"version":1,"scenes":[]}\n')
            for name in ("compact.pdf", "full.pdf"):
                shutil.copyfile(self.original, folder / name)
        shutil.copytree(committed, fresh)
        return committed, fresh

    def test_declared_tree_compares_every_figure_and_exact_metadata(self):
        committed, fresh = self.trees()
        result = compare_trees(committed, fresh)
        self.assertTrue(result["ok"])
        self.assertEqual(result["figures"], 4)
        self.assertEqual(result["declared_files"], 9)
        for relative in ("palettes.tex", "steps.tex", "steps.json",
                         "light/derivations.vector.json", "dark/derivations.vector.json"):
            with self.subTest(file=relative):
                filename = fresh / relative
                original = filename.read_bytes()
                filename.write_bytes(original + b"\n")
                result = compare_trees(committed, fresh)
                self.assertFalse(result["ok"])
                self.assertIn(relative, [item["file"] for item in result["differences"]])
                filename.write_bytes(original)

    def test_unused_and_missing_files_fail(self):
        committed, fresh = self.trees()
        (committed / "unused.pdf").write_bytes(self.original.read_bytes())
        result = compare_trees(committed, fresh)
        self.assertFalse(result["ok"])
        self.assertIn("unused.pdf", result["differences"][0]["unused"])
        (committed / "unused.pdf").unlink()
        (committed / "light/compact.pdf").unlink()
        self.assertFalse(compare_trees(committed, fresh)["ok"])

    def test_warning_only_release_succeeds_and_emits_ci_annotation(self):
        committed, fresh = self.trees()
        write_fixture(fresh / "light/compact.pdf", color="0.000001")
        comparison = compare_trees(committed, fresh)
        self.assertTrue(comparison["ok"])
        self.assertFalse(comparison["exact_match"])
        self.assertEqual(len(comparison["warnings"]), 1)
        self.assertEqual(comparison["errors"], [])
        self.assertEqual(len(comparison["differences"]), 1)

        def export(command, **_kwargs):
            shutil.copytree(fresh, Path(command[-1]))
            return SimpleNamespace(returncode=0)

        output = io.StringIO()
        project = self.directory / "project"
        project.mkdir()
        (project / "presentations.json").write_text(json.dumps({
            "version": 2, "default": "sample", "decks": [{"id": "sample", "title": "Sample",
            "source": "sample.tex", "animation": {"provider": "scene.py", "assets": "generated"}}]}))
        arguments = ["verify_exports.py", "--project", str(project), "--committed-dir", str(committed),
                     "--output-dir", str(self.directory / "release")]
        with patch.object(sys, "argv", arguments), patch("derivation_toolkit.verify.verify_exports.subprocess.run", side_effect=export), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn("::warning title=PDF export numeric difference::light/compact.pdf", output.getvalue())
        saved = list((self.directory / "release").glob("run-*/report.json"))
        self.assertEqual(len(saved), 1)
        report = json.loads(saved[0].read_text())
        self.assertTrue(report["ok"])
        self.assertFalse(report["exact_match"])
        self.assertEqual(report["warnings"][0]["file"], "light/compact.pdf")
        write_fixture(fresh / "dark/full.pdf", unicode_hex="0042")
        self.assertFalse(compare_trees(committed, fresh)["ok"])

    def test_source_tree_output_is_rejected_before_creation(self):
        root = self.directory / "source"
        with self.assertRaises(ExportMismatch):
            external_output(root / "generated", root)
        self.assertEqual(external_output(root / "build/check", root), root / "build/check")
        self.assertEqual(external_output(self.directory, root), self.directory.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
