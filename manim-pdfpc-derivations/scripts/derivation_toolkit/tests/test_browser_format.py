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

    def test_outer_fringe_uses_the_adjacent_stroke_contrast(self):
        native = self.stroke()
        browser = native.copy()
        browser[5, 21, :3] = 236
        browser[6, 21, :3] = 239
        # The actual quadratic pair has two such outer-fringe pixels. A 3x3
        # window at their outside edge sees only the fringe, not the stroke.
        report = compare_rasters(browser, native)
        self.assertLessEqual(report["maximumCoverageRatio"], 1)

    def test_shared_closing_edge_can_have_two_different_coverage_samples(self):
        # Red-channel samples from the actual MQZQZ baseline at one-pixel
        # stroke width. Both rasterizers paint the same two adjacent rows.
        native = np.full((16, 24, 4), 255, dtype=np.uint8)
        native[:, :, :3] = 230
        browser = native.copy()
        native[6, :, :3] = native[9, :, :3] = 221
        native[7, :, :3] = 123
        native[8, :, :3] = 107
        browser[7:9, :, :3] = 57
        for first, second in ((native, browser), (browser, native)):
            report = compare_rasters(first, second)
            self.assertLessEqual(report["maximumCoverageRatio"], 1)

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
