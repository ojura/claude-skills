"""Check source-owned TeX text through transformed snapshots and real PDF readers.

Run in the authoring venv after installing requirements-test.lock. MuPDF and
Poppler are required: missing readers must fail rather than skip selection tests.
All temporary TeX/PDF products use the external authoring cache.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

try:
    import fitz
except ImportError as error:
    raise RuntimeError(
        "MuPDF selection tests are required; install animations/requirements-test.lock "
        "into the authoring venv."
    ) from error

import numpy as np
from manim import Line, MathTex, Tex, Transform, VGroup, tempconfig
from pypdf import PdfReader
from pypdf.generic import ContentStream

from .support import PALETTES, TEMPLATE, configure
from ..animation.authoring import AuthoringScene as Derivation
from ..animation import pdf_glyphs, tex_semantics
from ..animation.tex_semantics import TextMappingError, bind_tex, snapshot_glyph, validate_text_transform
from ..animation.tex_font_source import (FontSourceError, probe_font_program,
                             probe_glyph_unicode_map, select_font_map)
from ..animation.vector_scene import HEIGHT, SCALE, WIDTH, paths_from_mobjects, write_snapshot
from ..animation.narrative import StepDescriptions

from ..verify.verify_exports import compare_pdf


def flattened(text):
    """Ignore reader line wrapping while retaining actual Unicode and spaces."""
    return " ".join(text.split())


def ink_rectangle(mobject):
    points = mobject.get_all_points()
    return fitz.Rect(
        WIDTH / 2 + SCALE * float(points[:, 0].min()),
        HEIGHT / 2 - SCALE * float(points[:, 1].max()),
        WIDTH / 2 + SCALE * float(points[:, 0].max()),
        HEIGHT / 2 - SCALE * float(points[:, 1].min()),
    )


def serialized_glyph_ink(program, reader):
    """Independently find painted extrema in the actual PDF glyph program."""
    transform = np.eye(3)
    stack, path, painted = [], [], []
    width, cap, join = 1.0, 0, 0
    current = start = None

    def point(values):
        return (transform @ np.array([*map(float, values), 1.0]))[:2]

    def segment(points):
        if len(points) == 2:
            values = np.asarray(points)
            return (*values.min(axis=0), *values.max(axis=0))
        p0, p1, p2, p3 = np.asarray(points)
        low, high = [], []
        for axis in range(2):
            polynomial = np.polynomial.Polynomial([
                p0[axis], 3 * (p1[axis] - p0[axis]),
                3 * (p0[axis] - 2 * p1[axis] + p2[axis]),
                -p0[axis] + 3 * p1[axis] - 3 * p2[axis] + p3[axis]])
            samples = [0.0, 1.0]
            samples.extend(float(root.real) for root in polynomial.deriv().roots()
                           if abs(root.imag) < 1e-12 and 0 < root.real < 1)
            values = polynomial(samples)
            low.append(min(values))
            high.append(max(values))
        return (*low, *high)

    for operands, operator in ContentStream(program, reader).operations:
        if operator == b"d":
            assert not operands[0], "the source renderer uses solid strokes"
            continue
        values = list(map(float, operands))
        if operator in (b"d0", b"d1"):
            continue
        if operator == b"q":
            stack.append((transform.copy(), width, cap, join))
        elif operator == b"Q":
            transform, width, cap, join = stack.pop()
        elif operator == b"cm":
            a, b, c, d, e, f = values
            transform = transform @ np.array([[a, c, e], [b, d, f], [0, 0, 1]])
        elif operator == b"w":
            width = values[0]
        elif operator == b"J":
            cap = values[0]
        elif operator == b"j":
            join = values[0]
        elif operator == b"M":
            continue  # Miter limit is irrelevant to the required round joins.
        elif operator == b"m":
            current = start = point(values)
        elif operator == b"l":
            target = point(values)
            path.append(segment((current, target)))
            current = target
        elif operator == b"c":
            p1, p2, target = (point(values[i:i + 2]) for i in (0, 2, 4))
            path.append(segment((current, p1, p2, target)))
            current = target
        elif operator == b"h":
            path.append(segment((current, start)))
            current = start
        elif operator in (b"f", b"f*", b"S"):
            if operator == b"S":
                assert cap == join == 1, "the source renderer uses round caps and joins"
                radii = width / 2 * np.linalg.norm(transform[:2, :2], axis=1)
                painted.extend((box[0] - radii[0], box[1] - radii[1],
                                box[2] + radii[0], box[3] + radii[1]) for box in path)
            else:
                painted.extend(path)
            path = []
            current = start = None
        elif operator == b"n":
            path = []
            current = start = None
        else:
            raise AssertionError(f"Unreviewed Type3 drawing operator: {operator!r}")
    assert not stack and not path
    if not painted:
        return None
    values = np.asarray(painted)
    return (*values[:, :2].min(axis=0), *values[:, 2:].max(axis=0))


class SemanticAssertions(unittest.TestCase):
    palettes = PALETTES
    scene_type = Derivation
    configure = staticmethod(configure)

    @classmethod
    def setUpClass(cls):
        if not shutil.which("pdftotext"):
            raise RuntimeError("Poppler pdftotext is required for selection regression tests")
        cls.poppler_probe = Path(__file__).resolve().parents[1] / "animation/poppler_text_probe.py"
        try:
            result = subprocess.run(
                ["/usr/bin/python3", "-B", str(cls.poppler_probe), "--version"],
                capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(
                "Poppler area and drag selection tests require /usr/bin/python3, "
                "python3-gi and gir1.2-poppler-0.18. "
                + getattr(error, "stderr", "")) from error
        cls.poppler_reader = json.loads(result.stdout)["reader"]
        cache = Path(os.environ.get("DERIVATION_TEST_BUILD", Path.cwd() / "build/test-toolkit"))
        cache.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="tex-semantics-tests-", dir=cache)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.folder = Path(cls.temporary.name)

    def setUp(self):
        self.configure("light")
        self.scene = self.scene_type(self.palettes["light"], sample_rate=10)
        self.output_index = 0

    def pdf(self, mobjects, *, theme="light", crop=None, paths=None):
        self.output_index += 1
        destination = self.folder / f"{self._testMethodName}-{self.output_index}.pdf"
        if paths is None:
            paths = paths_from_mobjects(mobjects)
        palette = self.palettes[theme]
        background = [int(palette["background"][i:i + 2], 16) / 255 for i in (1, 3, 5)] + [1]
        write_snapshot(destination, paths, background, crop=crop)
        self.assertTrue(destination.is_file())
        return destination

    def poppler_text(self, path):
        return subprocess.check_output(["pdftotext", "-layout", str(path), "-"], text=True)

    def poppler_words(self, path):
        xml = subprocess.check_output(["pdftotext", "-bbox", str(path), "-"], text=True)
        return [element.text for element in ET.fromstring(xml).iter()
                if element.tag.endswith("word")]

    def assert_poppler_selection(self, path, *phrases, counts=None):
        result = subprocess.run(
            ["/usr/bin/python3", "-B", str(self.poppler_probe), str(path), *phrases],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["reader"], self.poppler_reader)
        self.assertEqual([item["phrase"] for item in report["phrases"]], list(phrases))
        for item in report["phrases"]:
            phrase = item["phrase"]
            self.assertEqual(len(item["matches"]), (counts or {}).get(phrase, 1), item)
            for match in item["matches"]:
                self.assertEqual(flattened(match["area_text"]), phrase,
                                 (phrase, "area extraction", match))
                self.assertEqual(flattened(match["drag_text"]), phrase,
                                 (phrase, "same-line drag", match))
        return report

    def assert_visible_font_text(self, page):
        fonts = page.get_fonts(full=True)
        self.assertTrue(fonts, "the snapshot must carry genuine font text")
        self.assertTrue(all(font[2] == "Type3" for font in fonts), fonts)
        self.assertFalse(page.get_images(full=True), "text must retain its vector outlines")
        self.assertNotIn("\ufffd", page.get_text())
        self.assertNotIn("\x00", page.get_text())

    def assert_search_hits(self, page, text, count=1):
        hits = page.search_for(text)
        self.assertEqual(len(hits), count, (text, page.get_text(), hits))
        for rectangle in hits:
            self.assertGreater(rectangle.width, 0, (text, rectangle))
            self.assertGreater(rectangle.height, 0, (text, rectangle))
            self.assertTrue(all(math.isfinite(value) for value in rectangle))
        return hits
