"""Raster comparisons distinguish edge coverage from missing or displaced paths."""
from __future__ import annotations

import unittest

import numpy as np

from ..verify.browser_format import compare_rasters


class BrowserFormatRasterChecks(unittest.TestCase):
    def stroke(self):
        image = np.full((30, 40, 4), 255, dtype=np.uint8)
        image[5:25, 19, :3] = 0
        image[5:25, 18, :3] = 120
        image[5:25, 20, :3] = 180
        return image

    def test_different_edge_coverage_retains_matching_interiors(self):
        native = self.stroke()
        browser = native.copy()
        browser[5:25, 18, :3] = 95
        browser[5:25, 20, :3] = 190
        report = compare_rasters(browser, native)
        self.assertEqual(report["constantInteriorMaximum"], 0)
        self.assertEqual(report["maximumDifference"], 25)

    def test_missing_thin_path_fails_in_both_directions(self):
        native = self.stroke()
        absent = np.full_like(native, 255)
        for first, second in ((native, absent), (absent, native)):
            with self.subTest(first_is_native=first is native):
                with self.assertRaisesRegex(AssertionError, "Constant-colour interior"):
                    compare_rasters(first, second)

    def test_displaced_thin_path_is_not_an_antialiasing_difference(self):
        native = self.stroke()
        moved = np.roll(native, 3, axis=1)
        with self.assertRaisesRegex(AssertionError, "Constant-colour interior"):
            compare_rasters(moved, native)

    def test_missing_stroke_core_is_rejected_with_fringe_pixels_present(self):
        native = self.stroke()
        missing_core = native.copy()
        missing_core[5:25, 19, :3] = 255
        for first, second in ((native, missing_core), (missing_core, native)):
            with self.subTest(first_is_native=first is native):
                with self.assertRaisesRegex(AssertionError, "coverage"):
                    compare_rasters(first, second)

    def test_one_pixel_displacement_is_rejected_in_both_directions(self):
        native = self.stroke()
        moved = np.roll(native, 1, axis=1)
        for first, second in ((native, moved), (moved, native)):
            with self.subTest(first_is_native=first is native):
                with self.assertRaisesRegex(AssertionError, "coverage"):
                    compare_rasters(first, second)

    def test_changed_fill_and_different_dimensions_are_rejected(self):
        native = self.stroke()
        changed = native.copy()
        changed[10:20, 5:15, :3] = [255, 0, 0]
        with self.assertRaises(AssertionError):
            compare_rasters(changed, native)
        with self.assertRaisesRegex(AssertionError, "Raster sizes"):
            compare_rasters(native[:-1], native)


if __name__ == "__main__":
    unittest.main(verbosity=2)
