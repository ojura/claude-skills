"""Exercise HiDPI selection and comparison with real non-course PDF fixtures."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from pypdf.annotations import Link
from pypdf.generic import DecodedStreamObject, NameObject, RectangleObject

from ..verify.smoke_hidpi import (Binding, compare_region, drawing_box, redraw_size, render_reference,
                                 select_binding, viewport_geometry, window_sizes)


class HiDPIInputs(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "article.pdf"
        self.vectors = self.root / "article.vector.json"

    def write(self, pages, scenes=None):
        writer = PdfWriter()
        for index, item in enumerate(pages):
            page = writer.add_blank_page(*item.get("size", (595, 842)))
            if "crop" in item:
                page.cropbox = RectangleObject(item["crop"])
            if item.get("rotate"):
                page.rotate(item["rotate"])
            for identifier, rect in item.get("links", []):
                writer.add_annotation(index, Link(rect=rect, url="pdfpc-vector:" + identifier))
            if "paint" in item:
                x0, y0, x1, y1 = item["paint"]
                stream = DecodedStreamObject()
                stream.set_data((f"q .6 .1 .1 rg {x0} {y0} {x1-x0} {y1-y0} re f "
                                 f"1 1 1 rg {x0+20} {y0+20} 10 10 re f Q").encode())
                page[NameObject("/Contents")] = writer._add_object(stream)
        writer.set_page_label(0, len(pages)-1, style="/r")
        with self.pdf.open("wb") as output:
            writer.write(output)
        scenes = scenes or [{"id": "toast", "width": 16, "height": 9}]
        self.vectors.write_text(json.dumps({"version": 1, "scenes": [
            {"background": [1, 1, 1, 1], "checkpoints": [{"time": 0}], **scene}
            for scene in scenes]}))

    def test_portrait_article_selects_actual_scene_and_physical_page(self):
        self.write([{}, {"links": [("toast", (100, 400, 420, 580))]}])
        binding = select_binding(self.pdf, self.vectors)
        self.assertEqual((binding.page, binding.label, binding.scene), (2, "ii", "toast"))
        self.assertEqual(binding.region, (100, 262, 420, 442))
        self.assertEqual(binding.page_size, (595, 842))
        self.assertEqual(window_sizes(binding), [(530, 750), (636, 900)])

    def test_explicit_scene_and_page_disambiguate_repeated_bindings(self):
        self.write([{"links": [("warm", (0, 0, 160, 90))]},
                    {"links": [("toast", (0, 0, 160, 90))]},
                    {"links": [("warm", (0, 0, 160, 90))]}],
                   [{"id": name, "width": 16, "height": 9} for name in ("warm", "toast")])
        self.assertEqual(select_binding(self.pdf, self.vectors).scene, "warm")
        self.assertEqual(select_binding(self.pdf, self.vectors, "toast").page, 2)
        self.assertEqual(select_binding(self.pdf, self.vectors, "warm", 3).page, 3)
        with self.assertRaisesRegex(ValueError, "No matching"):
            select_binding(self.pdf, self.vectors, "warm", 2)
        with self.assertRaisesRegex(ValueError, "physical page"):
            select_binding(self.pdf, self.vectors, physical_page=4)

    def test_selected_inputs_must_resolve_the_same_scene(self):
        self.write([{"links": [("missing", (0, 0, 160, 90))]}])
        with self.assertRaisesRegex(ValueError, "absent"):
            select_binding(self.pdf, self.vectors)
        timeline = json.loads(self.vectors.read_text())
        timeline["scenes"] *= 2
        self.vectors.write_text(json.dumps(timeline))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            select_binding(self.pdf, self.vectors)

    def test_multiple_regions_on_the_selected_page_are_rejected(self):
        self.write([{"links": [("toast", (0, 0, 160, 90)), ("toast", (200, 0, 360, 90))]}])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            select_binding(self.pdf, self.vectors)

    def test_poster_checkpoint_contract_is_checked(self):
        for checkpoints in ([], [{"time": 1}]):
            with self.subTest(checkpoints=checkpoints):
                self.write([{"links": [("toast", (0, 0, 160, 90))]}],
                           [{"id": "toast", "width": 16, "height": 9, "checkpoints": checkpoints}])
                with self.assertRaisesRegex(ValueError, "poster checkpoint"):
                    select_binding(self.pdf, self.vectors)

    def test_region_geometry_must_match_the_selected_scene(self):
        for rectangle, error in (((0, 0, 160, 100), "aspect ratios"),
                                 ((-10, 0, 150, 90), "outside")):
            with self.subTest(rectangle=rectangle):
                self.write([{"links": [("toast", rectangle)]}])
                with self.assertRaisesRegex(ValueError, error):
                    select_binding(self.pdf, self.vectors)

    @unittest.skipUnless(shutil.which("pdftocairo"), "Poppler rasterizer is required")
    def test_actual_raster_preserves_portrait_and_wide_page_aspect(self):
        for page_size in ((595, 842), (960, 540)):
            with self.subTest(page_size=page_size):
                rectangle = (100, 150, 420, 330)
                self.write([{"size": page_size, "links": [("toast", rectangle)], "paint": rectangle}])
                binding = select_binding(self.pdf, self.vectors)
                size = (1000, 750)
                reference = render_reference(self.pdf, binding, size, self.root / "poster")
                pixels = np.asarray(reference)
                colored = (pixels[:, :, 0] > 100) & (pixels[:, :, 1] < 80)
                y, x = np.where(colored)
                actual = (x.min(), y.min(), x.max()+1, y.max()+1)
                expected = viewport_geometry(binding, size)[2]
                self.assertTrue(all(abs(a-b) <= 1 for a, b in zip(actual, expected)), (actual, expected))

    @unittest.skipUnless(shutil.which("pdftocairo"), "Poppler rasterizer is required")
    def test_cropped_rotated_regions_match_actual_poppler_pixels(self):
        expected = {0: (100, 600, 300, 700), 90: (100, 100, 200, 300),
                    180: (200, 100, 400, 200), 270: (600, 200, 700, 400)}
        rectangle = (150, 180, 350, 280)
        for rotation, region in expected.items():
            with self.subTest(rotation=rotation):
                dimensions = (2, 1) if rotation in (0, 180) else (1, 2)
                self.write([{"size": (600, 950), "crop": (50, 80, 550, 880),
                             "rotate": rotation, "links": [("toast", rectangle)], "paint": rectangle}],
                           [{"id": "toast", "width": dimensions[0], "height": dimensions[1]}])
                binding = select_binding(self.pdf, self.vectors)
                self.assertEqual(binding.region, region)
                reference = render_reference(self.pdf, binding, (1000, 750), self.root / "poster")
                pixels = np.asarray(reference)
                y, x = np.where((pixels[:, :, 0] > 100) & (pixels[:, :, 1] < 80))
                actual = (x.min(), y.min(), x.max()+1, y.max()+1)
                box = viewport_geometry(binding, (1000, 750))[2]
                self.assertTrue(all(abs(a-b) <= 1 for a, b in zip(actual, box)), (actual, box))


class HiDPIForeground(unittest.TestCase):
    def binding(self, color):
        return Binding(1, "i", "toast", 0, (0, 0, 100, 100), (100, 100),
                       (100, 100), (*[value/255 for value in color], 1))

    def test_redraw_uses_actual_gtk_size_and_selected_scene_page_time(self):
        binding = Binding(1, "i", "toast", 0, (0, 0, 160, 90), (160, 90),
                          (160, 90), (0, 0, 0, 1))
        frame = {"event": "frame", "view": "presentation", "scene": "toast", "page": 1,
                 "displayed_time": 0, "width": 1200, "height": 674}
        self.assertEqual(redraw_size([frame], binding, (1200, 676)), (1200, 674))
        for changes in ({"scene": "other"}, {"page": 2}, {"displayed_time": 1},
                        {"view": "presenter"}, {"height": 670}):
            with self.subTest(changes=changes):
                self.assertIsNone(redraw_size([{**frame, **changes}], binding, (1200, 676)))
        self.assertEqual(redraw_size([frame], binding, (1200, 900)), (1200, 674))

    def test_window_padding_uses_logical_pixel_alignment(self):
        self.assertEqual(drawing_box((1200, 676), (1200, 674)), (0, 0, 1200, 674))
        self.assertEqual(drawing_box((1200, 900), (1200, 674)), (0, 112, 1200, 786))
        self.assertEqual(drawing_box((1000, 750), (530, 750)), (234, 0, 764, 750))
        reference = Image.new("RGB", (100, 100), (15, 18, 22))
        ImageDraw.Draw(reference).line((25, 30, 25, 59), fill="white", width=1)
        window = Image.new("RGB", (102, 104), "black")
        window.paste(reference, (0, 2))
        crop = window.crop(drawing_box(window.size, reference.size))
        result = compare_region(crop, reference, self.binding((15, 18, 22)), reference.size)
        self.assertEqual(result["mean_channel_error"], 0)
        self.assertEqual(result["unmatched_ink_fraction"], 0)
        for window_size, viewport_size in (((100, 100), (102, 100)),
                                           ((100, 100), (100, 0)),
                                           ((101, 100), (100, 100))):
            with self.subTest(window_size=window_size, viewport_size=viewport_size):
                with self.assertRaisesRegex(ValueError, "whole GDK logical pixels"):
                    drawing_box(window_size, viewport_size)

    def test_missing_or_shifted_thin_dark_foreground_cannot_pass(self):
        color = (15, 18, 22)
        reference = Image.new("RGB", (100, 100), color)
        ImageDraw.Draw(reference).line((25, 30, 25, 59), fill="white", width=1)
        for shifted in (False, True):
            with self.subTest(shifted=shifted):
                actual = Image.new("RGB", (100, 100), color)
                if shifted:
                    ImageDraw.Draw(actual).line((29, 30, 29, 59), fill="white", width=1)
                result = compare_region(actual, reference, self.binding(color), (100, 100))
                # The former min(channel)<180 mask counted this entire dark region as ink;
                # both its tolerance check and mean-channel threshold accepted these faults.
                self.assertLess(result["mean_channel_error"], 2)
                self.assertGreater(result["unmatched_ink_fraction"], 0.99)

    def test_matching_foreground_passes_in_light_and_dark(self):
        for background, foreground in (((255, 255, 255), "black"), ((15, 18, 22), "white")):
            with self.subTest(background=background):
                reference = Image.new("RGB", (100, 100), background)
                ImageDraw.Draw(reference).rectangle((20, 30, 25, 60), fill=foreground)
                result = compare_region(reference, reference, self.binding(background), (100, 100))
                self.assertEqual(result["unmatched_ink_fraction"], 0)
                self.assertEqual(result["mean_channel_error"], 0)

    def test_subpixel_separator_coverage_can_span_two_pixels(self):
        background = (15, 18, 22)
        reference = Image.new("RGB", (100, 100), background)
        actual = reference.copy()
        ImageDraw.Draw(reference).line((25, 30, 25, 59),
                                       fill=tuple(value+44 for value in background), width=1)
        ImageDraw.Draw(actual).rectangle((25, 30, 26, 59),
                                        fill=tuple(value+22 for value in background))
        result = compare_region(actual, reference, self.binding(background), (100, 100))
        self.assertEqual(result["unmatched_ink_fraction"], 0)
        self.assertLess(result["mean_channel_error"], 2)

    def test_blank_reference_has_no_alignment_evidence(self):
        reference = Image.new("RGB", (100, 100), "white")
        with self.assertRaisesRegex(AssertionError, "no visible foreground"):
            compare_region(reference, reference, self.binding((255, 255, 255)), (100, 100))


if __name__ == "__main__":
    unittest.main(verbosity=2)
