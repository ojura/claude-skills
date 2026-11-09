"""Real Manim lifecycle operations must agree with the recorded visible scene."""
from __future__ import annotations

import unittest

import numpy as np
from manim import Circle, FadeOut, LEFT, RIGHT, Square, UP, VGroup

from ..animation.vector_scene import VectorScene, scene_paths_at
from .support import PALETTES, configure


def visible(paths):
    return [path for path in paths if path["fill"][3] > 0 or
            (path["stroke"][3] > 0 and path["stroke_width"] > 0)]


class VectorAnimationLifecycle(unittest.TestCase):
    def setUp(self):
        configure("light")
        self.scene = VectorScene(PALETTES["light"], sample_rate=10)

    def test_fade_out_new_group_removes_existing_members(self):
        first = Circle(fill_opacity=1, stroke_width=0).shift(LEFT)
        second = Circle(fill_opacity=1, stroke_width=0).shift(RIGHT)
        retained = Square(fill_opacity=1, stroke_width=0).shift(UP).set_z_index(1)
        self.scene.add(first, second, retained)
        self.scene.play(FadeOut(VGroup(first, second)), run_time=.2)
        self.scene.checkpoint("Remaining shape", "Remaining shape")
        family = self.scene.get_mobject_family_members()
        self.assertIn(retained, family)
        self.assertNotIn(first, family)
        self.assertNotIn(second, family)
        self.assertEqual(len(visible(self.scene.handout_states[-1]["full_paths"])), 1)
        timeline = self.scene.export()
        self.assertEqual(len(visible(scene_paths_at(timeline, 0))), 3)
        self.assertEqual(len(visible(scene_paths_at(timeline, timeline["duration"]))), 1)

    def test_animating_an_unadded_object_introduces_its_paths(self):
        shape = Circle(fill_opacity=1, stroke_width=0)
        self.scene.play(shape.animate.shift(RIGHT), run_time=.2)
        self.assertIn(shape, self.scene.get_mobject_family_members())
        timeline = self.scene.export()
        first = visible(scene_paths_at(timeline, 0))
        last = visible(scene_paths_at(timeline, timeline["duration"]))
        self.assertEqual((len(first), len(last)), (1, 1))
        start = np.array(first[0]["coords"]).reshape(-1, 2)
        end = np.array(last[0]["coords"]).reshape(-1, 2)
        np.testing.assert_allclose(end-start, np.tile([self.scene.canvas.scale, 0], (len(start), 1)), atol=1e-4)

    def test_regrouped_move_preserves_paths_then_removes_them(self):
        first = Circle(fill_opacity=1, stroke_width=0).shift(LEFT)
        second = Square(fill_opacity=1, stroke_width=0).shift(RIGHT)
        self.scene.add(first, second)
        group = VGroup(first, second)
        self.scene.play(group.animate.shift(UP), run_time=.2)
        self.assertEqual(len(visible(self.scene.current_paths())), 2)
        self.scene.play(FadeOut(group), run_time=.2)
        timeline = self.scene.export()
        self.assertEqual(len(timeline["objects"]), 2)
        self.assertEqual(len(visible(scene_paths_at(timeline, .2))), 2)
        self.assertEqual(len(visible(scene_paths_at(timeline, timeline["duration"]))), 0)
        self.assertEqual(len(visible(self.scene.current_paths())), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
