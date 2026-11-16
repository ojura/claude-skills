"""Evaluate optional layer values and quadratic geometry from native-format input."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cairo
import fitz
import numpy as np

from ..animation import vector_scene
from ..animation.vector_scene import append_path, draw_paths, draw_scene_fitted, scene_paths_at
from ..web.build import pdf_manifest


FIXTURE = Path(__file__).with_name("fixtures") / "format-options.vector.json"
SCENES = json.loads(FIXTURE.read_text())["scenes"]


def render(scene, time, size=None, background=None):
    width, height = size or (scene["width"], scene["height"])
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    if background is not None:
        context.set_source_rgba(*background)
        context.paint()
    draw_scene_fitted(context, scene, time, width, height)
    surface.flush()
    return surface


def pixels(surface):
    return np.frombuffer(surface.get_data(), dtype=np.uint8).reshape(
        surface.get_height(), surface.get_stride()).copy()


def native_fixed_paths(scene):
    """Isolate native Cairo's unscaled copy_path preparation from our drawing.

    Cairo rounds a copied path's control points before the output transform.
    Python constructs paths under that transform, so otherwise equivalent
    curves can differ at antialiased edges. Require exact native pixels after
    reproducing just this preparation; do not permit an unexplained tolerance.
    """
    cached = {}
    for track in scene["objects"]:
        first = track["keyframes"][0]["coords"]
        if all(frame.get("coords", first) == first for frame in track["keyframes"]):
            context = cairo.Context(cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None))
            append_path(context, {**track, "coords": first})
            cached[track["id"]] = context.copy_path()

    def append_prepared(context, item):
        if item["id"] not in cached:
            return append_path(context, item)
        context.append_path(cached[item["id"]])
        context.set_fill_rule(cairo.FILL_RULE_EVEN_ODD if item.get("fill_rule") == "evenodd"
                              else cairo.FILL_RULE_WINDING)

    return patch.object(vector_scene, "append_path", append_prepared)


class FormatEvaluation(unittest.TestCase):
    def test_missing_layers_are_zero_and_equal_layers_keep_input_order(self):
        first = deepcopy(SCENES[0])
        self.assertEqual([item["id"] for item in scene_paths_at(first, 1)],
                         ["background-layer", "default-layer", "foreground"])
        self.assertEqual(first, SCENES[0])
        self.assertEqual([item["id"] for item in scene_paths_at(SCENES[1], 1)],
                         ["first-default", "explicit-zero", "last-default"])

    def test_quadratic_after_close_matches_its_cubic_conversion(self):
        surface = cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None)
        actual, expected = cairo.Context(surface), cairo.Context(surface)
        append_path(actual, {"commands": "MQZQZ", "coords": [0, 0, 3, 3, 6, 0, 3, -3, 6, 0]})
        expected.move_to(0, 0)
        expected.curve_to(2, 2, 4, 2, 6, 0)
        expected.close_path()
        expected.curve_to(2, -2, 4, -2, 6, 0)
        expected.close_path()
        self.assertEqual(list(actual.copy_path()), list(expected.copy_path()))

    def test_quadratic_after_cubic_and_line_matches_its_cubic_conversion(self):
        surface = cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None)
        actual, expected = cairo.Context(surface), cairo.Context(surface)
        append_path(actual, {"commands": "MCQLQ", "coords": [
            0, 0, 1, 1, 5, 1, 6, 0, 9, 3, 12, 0, 15, 0, 18, -3, 21, 0]})
        expected.move_to(0, 0)
        expected.curve_to(1, 1, 5, 1, 6, 0)
        expected.curve_to(8, 2, 10, 2, 12, 0)
        expected.line_to(15, 0)
        expected.curve_to(17, -2, 19, -2, 21, 0)
        self.assertEqual(list(actual.copy_path()), list(expected.copy_path()))

    def test_real_pdf_annotation_can_have_a_different_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "square-region.pdf"
            with fitz.open() as document:
                page = document.new_page(width=200, height=220)
                page.draw_rect(page.rect, fill=(0.2, 0.3, 0.7), color=None)
                page.insert_link({"kind": fitz.LINK_URI, "uri": "pdfpc-vector:default-layers",
                                  "from": fitz.Rect(20, 30, 140, 150)})
                document.save(destination)
            metadata = pdf_manifest(destination, FIXTURE, "light")
            self.assertEqual(metadata["pages"][0]["scene"], {
                "id": "default-layers", "rect": [20, 30, 140, 150]})

    def test_fitted_scene_preserves_margins_and_the_callers_transform(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 180)
        context = cairo.Context(surface)
        context.set_source_rgb(1, 0, 1)
        context.paint()
        context.translate(20, 30)
        before = context.get_matrix()
        draw_scene_fitted(context, SCENES[0], 1, 120, 120)
        self.assertEqual(context.get_matrix(), before)
        surface.flush()
        rows = np.frombuffer(surface.get_data(), dtype=np.uint32).reshape(180, 200)
        # The square annotation contains a 120x80 scene with 20-pixel margins.
        for x, y in ((30, 40), (30, 140), (10, 60), (150, 60)):
            self.assertEqual(int(rows[y, x]), 0xFFFF00FF)
        self.assertEqual(int(rows[90, 60]), 0xFFFF0000)
        self.assertEqual(int(rows[90, 115]), 0xFF0000FF)
        self.assertEqual(int(rows[55, 25]), 0xFF00FF00)
        surface.finish()

    def test_matching_aspect_uses_the_existing_path_rendering(self):
        for scene in SCENES:
            for time in (0, .5, 1, 2):
                with self.subTest(scene=scene["id"], time=time):
                    expected = cairo.ImageSurface(cairo.FORMAT_ARGB32, scene["width"], scene["height"])
                    context = cairo.Context(expected)
                    context.set_source_rgba(*scene["background"])
                    context.paint()
                    draw_paths(context, scene_paths_at(scene, time))
                    expected.flush()
                    actual = render(scene, time)
                    np.testing.assert_array_equal(pixels(actual), pixels(expected))
                    actual.finish()
                    expected.finish()

    @unittest.skipUnless(os.environ.get("DERIVATION_VECTOR_PROBE"), "Set DERIVATION_VECTOR_PROBE to compare the native renderer")
    def test_fixture_pixels_match_native_with_fixed_path_preparation(self):
        probe = os.environ["DERIVATION_VECTOR_PROBE"]
        with tempfile.TemporaryDirectory() as temporary:
            for scene in SCENES:
                for size in ((120, 80), (120, 120), (240, 80), (240, 160)):
                    for time in (0, 0.5, 1, 2):
                        with self.subTest(scene=scene["id"], time=time, size=size):
                            destination = Path(temporary) / "native.png"
                            subprocess.run([probe, "render", str(FIXTURE), scene["id"], str(time),
                                            str(destination), *map(str, size)],
                                           check=True, capture_output=True, text=True)
                            native = cairo.ImageSurface.create_from_png(str(destination))
                            actual = render(scene, time, size)
                            actual_pixels, native_pixels = pixels(actual), pixels(native)
                            if not np.array_equal(actual_pixels, native_pixels):
                                with native_fixed_paths(scene):
                                    prepared = render(scene, time, size)
                                np.testing.assert_array_equal(pixels(prepared), native_pixels)
                                prepared.finish()
                                difference = np.abs(actual_pixels.astype(np.int16) - native_pixels)
                                print(f"Cairo fixed-path rounding: {scene['id']} at {time}s, {size}: "
                                      f"{np.count_nonzero(difference)} differing channels, "
                                      f"maximum {difference.max()}/255; identical with native path preparation")
                            actual.finish()
                            native.finish()


if __name__ == "__main__":
    unittest.main(verbosity=2)
