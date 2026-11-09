"""Portable vector interpolation, checkpoint and geometry regressions."""
from __future__ import annotations

import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from manim import Annulus, Dot, FadeOut, Line, ORIGIN, Rotate, smooth

from ..animation.geometry import MarkedAngle
from ..animation.vector_scene import VectorScene, paths_from_mobjects, state_at, write_snapshot
from .support import PALETTES, configure


def rotate(point, angle):
    x, y, z = point
    c, s = math.cos(angle), math.sin(angle)
    return np.array([c*x - s*y, s*x + c*y, z])


class VectorPlayback(unittest.TestCase):
    def setUp(self):
        configure("light")

    def test_marked_angle_transforms_its_reference_arc_and_label_together(self):
        origin = np.array([0.4, -0.3, 0.0])
        label_offset = np.array([0.7, 0.5, 0.0])
        angle = MarkedAngle(origin, 0.2, 0.6, Dot(), radius=0.5,
                            reference_length=0.95, label_offset=label_offset,
                            arc_color="#333333", reference_color="#999999")
        turn, scale = -0.7, 1.4
        displacement = np.array([1.1, 0.6, 0.0])
        angle.rotate(turn, about_point=ORIGIN).scale(scale, about_point=ORIGIN).shift(displacement)
        center = scale * rotate(origin, turn) + displacement
        direction = rotate(np.array([1.0, 0.0, 0.0]), 0.2 + turn)
        np.testing.assert_allclose(angle.reference.get_start(), center, atol=1e-12)
        np.testing.assert_allclose(angle.arc.get_arc_center(), center, atol=1e-12)
        np.testing.assert_allclose(angle.reference.get_end(), center + scale * 0.95 * direction, atol=1e-12)
        np.testing.assert_allclose(angle.arc.get_start(), center + scale * 0.5 * direction, atol=1e-12)
        np.testing.assert_allclose(angle.label.get_center(), center + scale * rotate(label_offset, turn), atol=1e-12)

    def test_rotation_interpolates_at_arbitrary_refresh_times_and_preserves_holds(self):
        scene = VectorScene(PALETTES["light"])
        line = Line(ORIGIN, [1.9, 0, 0], color="#821421")
        scene.add(line)
        scene.wait(0.4)
        with patch.object(scene.renderer, "render", side_effect=AssertionError("raster render called")):
            scene.play(Rotate(line, math.pi / 6, about_point=ORIGIN),
                       run_time=2.1, rate_func=smooth)
        scene.wait(0.3)
        track = scene.export()["objects"][0]
        self.assertEqual(state_at(track, 0)["coords"], state_at(track, 0.399)["coords"])
        worst = 0.0
        for time in np.linspace(0.4, 2.5, 253):
            theta = math.pi / 6 * smooth((time - 0.4) / 2.1)
            expected = [600 + 190 * math.cos(theta), 325 - 190 * math.sin(theta)]
            actual = state_at(track, float(time))["coords"][-2:]
            worst = max(worst, float(np.linalg.norm(np.asarray(actual) - expected)))
        self.assertLess(worst, 0.06, f"interpolation error {worst:.5f} px")
        self.assertEqual(state_at(track, 2.501)["coords"], state_at(track, 2.8)["coords"])

    def test_sparse_style_frames_preserve_geometry_and_holes(self):
        annulus = Annulus(inner_radius=0.4, outer_radius=0.9, color="#333333",
                          fill_opacity=1, stroke_width=0)
        paths = paths_from_mobjects([annulus])
        self.assertGreaterEqual(paths[0]["commands"].count("M"), 2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hole.png"
            write_snapshot(path, paths, [1, 1, 1, 1], png_scale=1)
            pixels = np.asarray(Image.open(path).convert("RGB"))
            np.testing.assert_array_equal(pixels[325, 600], [255, 255, 255])
            np.testing.assert_array_equal(pixels[325, 650], [51, 51, 51])
        initial = {name: paths[0][name] for name in ("coords", "fill", "stroke", "stroke_width")}
        track = {"keyframes": [{"time": 0, **initial},
                               {"time": 1, "fill": [0.2, 0.2, 0.2, 0],
                                "stroke": initial["stroke"], "stroke_width": 0}]}
        halfway = state_at(track, 0.5)
        self.assertEqual(halfway["coords"], initial["coords"])
        self.assertAlmostEqual(halfway["fill"][3], 0.5)

    def test_fast_turn_stays_subpixel(self):
        scene = VectorScene(PALETTES["light"])
        line = Line(ORIGIN, [-3.1, 0, 0])
        scene.add(line)
        scene.play(Rotate(line, -math.radians(48), about_point=ORIGIN),
                   run_time=1.15, rate_func=smooth)
        track = scene.export()["objects"][0]
        worst = 0.0
        for time in np.linspace(0, 1.15, 1001):
            theta = -math.radians(48) * smooth(time / 1.15)
            expected = [600 - 310 * math.cos(theta), 325 + 310 * math.sin(theta)]
            actual = state_at(track, float(time))["coords"][-2:]
            worst = max(worst, float(np.linalg.norm(np.asarray(actual) - expected)))
        self.assertLess(worst, 0.3, f"fast turn interpolation error {worst:.5f} px")

    def test_static_snapshots_can_share_one_continuous_live_passage(self):
        scene = VectorScene(PALETTES["light"])
        scene.add(Line(ORIGIN, [1, 0, 0]))
        for index in range(4):
            scene.checkpoint(str(index), str(index), pause=index in (0, 3))
            if index < 3:
                scene.wait(0.2)
        self.assertEqual(len(scene.handout_states), 4)
        self.assertEqual([point["time"] for point in scene.checkpoints], [0, 0.6])

    def test_removed_tokens_stay_hidden_and_paths_keep_identity(self):
        scene = VectorScene(PALETTES["light"])
        first = Line([-1, 0, 0], [0, 0, 0])
        second = Line([0, 1, 0], [1, 1, 0])
        scene.add(first, second)
        scene.play(FadeOut(first), run_time=0.3)
        scene.wait(0.2)
        tracks = scene.export()["objects"]
        self.assertEqual(len(tracks), 2)
        self.assertEqual(state_at(tracks[0], 0.5)["stroke"][3], 0)
        self.assertEqual(state_at(tracks[1], 0.5)["stroke"][3], 1)
        self.assertIs(scene.recorded_mobjects[id(first)], first)

    def test_unrepresentable_layer_changes_fail_early(self):
        scene = VectorScene(PALETTES["light"])
        first = Line([-1, 0, 0], [1, 0, 0])
        second = Line([0, -1, 0], [0, 1, 0])
        scene.add(first, second)
        scene.record()
        scene.mobjects.reverse()
        with self.assertRaisesRegex(ValueError, "reorder visible"):
            scene.record()
        scene.mobjects.reverse()
        first.set_z_index(1)
        with self.assertRaisesRegex(ValueError, "fixed z_index"):
            scene.record()


if __name__ == "__main__":
    unittest.main(verbosity=2)
