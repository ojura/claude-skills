#!/usr/bin/env python3
"""Independent positive and mutation controls for serialized glyph bounds."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          FloatObject, NameObject, NumberObject)

from ..verify.pdf_type3_bounds import (Type3BoundsError, serialized_glyph_ink,
                             validate_type3_bounds)


CURVE = b"0 0 m 0 1000 1000 1000 1000 0 c h f\n"


def array(values):
    return ArrayObject([FloatObject(value) for value in values])


def fixture(path, *, body=CURVE, glyph_box=(-1, -1, 1001, 751),
            font_box=None, text="A", extra_program=False, missing_map=False,
            metric_operator="d1"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)

    def stream(data):
        value = DecodedStreamObject()
        value.set_data(data)
        return writer._add_object(value)

    header = "600 0 " + " ".join(map(str, glyph_box)) + " " + metric_operator + "\n"
    programs = DictionaryObject({NameObject("/A"): stream(header.encode() + body)})
    if extra_program:
        programs[NameObject("/unaccounted")] = stream(header.encode() + body)
    cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /BoundsFixture def\n/CMapType 2 def\n1 begincodespacerange\n"
            "<00> <ff>\nendcodespacerange\n1 beginbfchar\n"
            f"<{42 if missing_map else 41}> <{text.encode('utf-16-be').hex()}>\n"
            "endbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type3"),
        NameObject("/FontBBox"): array(glyph_box if font_box is None else font_box),
        NameObject("/FontMatrix"): array((.001, 0, 0, .001, 0, 0)),
        NameObject("/CharProcs"): programs,
        NameObject("/Encoding"): DictionaryObject({NameObject("/Differences"):
            ArrayObject([NumberObject(65), NameObject("/A")])}),
        NameObject("/FirstChar"): NumberObject(65), NameObject("/LastChar"): NumberObject(65),
        NameObject("/Widths"): array((600,)), NameObject("/Resources"): DictionaryObject(),
        NameObject("/ToUnicode"): stream(cmap.encode()),
    }))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Contents")] = stream(b"BT /F1 12 Tf 10 10 Td (A) Tj ET\n")
    writer.write(path)


class Type3Bounds(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="psr-type3-bounds-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "fixture.pdf"

    def ink(self):
        reader = PdfReader(self.path)
        font = reader.pages[0]["/Resources"]["/Font"]["/F1"]
        return serialized_glyph_ink(font["/CharProcs"]["/A"], reader)

    def test_analytic_cubic_extrema_accept_bounds_inside_control_hull(self):
        fixture(self.path)
        self.assertEqual(self.ink(), (0., 0., 1000., 750.))
        self.assertEqual(validate_type3_bounds(self.path), {
            "type3_fonts": 1, "glyph_programs": 1, "painted_glyphs": 1, "blank_spaces": 0})

    def test_each_d1_and_font_edge_must_enclose_actual_ink(self):
        for field in ("glyph_box", "font_box"):
            for edge, value in enumerate((1, 1, 999, 749)):
                box = [-1, -1, 1001, 751]
                box[edge] = value
                with self.subTest(field=field, edge=edge):
                    fixture(self.path, **{field: box})
                    with self.assertRaisesRegex(Type3BoundsError, "strictly enclose serialized ink"):
                        validate_type3_bounds(self.path)

    def test_exact_baseline_requires_outer_coverage(self):
        for baseline in (0.0, -0.0):
            fixture(self.path, glyph_box=(-1, baseline, 1001, 751), text="B")
            with self.assertRaisesRegex(Type3BoundsError, "d1 does not strictly enclose"):
                validate_type3_bounds(self.path)
        fixture(self.path, glyph_box=(-1, -1, 1001, 751), text="B")
        self.assertEqual(validate_type3_bounds(self.path)["painted_glyphs"], 1)

    def test_transform_stack_and_rectangle_are_evaluated_in_glyph_space(self):
        body = b"q 0 2 -3 0 10 20 cm\n" + CURVE + b"Q\n2000 3000 100 200 re f\n"
        fixture(self.path, body=body, glyph_box=(-2241, 19, 2101, 3201))
        self.assertEqual(self.ink(), (-2240., 20., 2100., 3200.))
        self.assertEqual(validate_type3_bounds(self.path)["painted_glyphs"], 1)

    def test_implicit_curve_controls_and_even_odd_fill_are_supported(self):
        body = b"0 0 m 500 1000 1000 0 v 1500 1000 2000 0 y h f*\n"
        fixture(self.path, body=body, glyph_box=(-1, -1, 2001, 1001))
        self.assertEqual(validate_type3_bounds(self.path)["painted_glyphs"], 1)

    def test_empty_program_is_allowed_only_for_source_whitespace(self):
        fixture(self.path, body=b"q Q\n", glyph_box=(0, -200, 333, 700), text=" ")
        self.assertEqual(validate_type3_bounds(self.path)["blank_spaces"], 1)
        self.assertIsNone(self.ink())
        fixture(self.path, body=b"q Q\n", text="B")
        with self.assertRaisesRegex(Type3BoundsError, "zero-ink glyph is not source-mapped whitespace"):
            validate_type3_bounds(self.path)

    def test_discarded_paths_are_not_painted_ink(self):
        fixture(self.path, body=b"0 0 m 9999 9999 l n\n", text=" ")
        self.assertIsNone(self.ink())
        self.assertEqual(validate_type3_bounds(self.path)["blank_spaces"], 1)

    def test_unsupported_geometry_and_malformed_states_fail_explicitly(self):
        for body, message in ((b"0 0 m 1 1 l S\n", "unsupported"),
                              (b"/image Do\n", "unsupported"),
                              (b"0 0 1 1 re W n\n", "unsupported"),
                              (b"Q\n", "unbalanced"), (b"q\n", "unfinished"),
                              (b"0 0 m\n", "unfinished"), (b"1 2 l f\n", "no current"),
                              (b"0 m\n", "expected 2")):
            with self.subTest(body=body):
                fixture(self.path, body=body)
                with self.assertRaisesRegex(Type3BoundsError, message):
                    validate_type3_bounds(self.path)

    def test_source_identity_and_metrics_are_required(self):
        for changes, message in (({"extra_program": True}, "no source encoding"),
                                 ({"missing_map": True}, "identities differ"),
                                 ({"glyph_box": (2, 0, 1, 1)}, "reversed"),
                                 ({"font_box": (2, 0, 1, 1)}, "reversed"),
                                 ({"metric_operator": "d0"}, "begin with d1")):
            with self.subTest(changes=changes):
                fixture(self.path, **changes)
                with self.assertRaisesRegex(Type3BoundsError, message):
                    validate_type3_bounds(self.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
