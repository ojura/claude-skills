"""Real Manim lifecycle operations must agree with the recorded visible scene."""
from __future__ import annotations

import unittest

import cairo
import numpy as np
from manim import AnimationGroup, Circle, FadeIn, FadeOut, LEFT, RIGHT, Square, Succession, UP, VGroup, Wait

from ..animation.vector_scene import VectorScene, draw_paths, scene_paths_at
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

    def overlapping_shapes(self):
        first = Square(side_length=2, fill_color="#FF0000", fill_opacity=1,
                       stroke_width=0).shift(.7 * LEFT)
        retained = Square(side_length=2, fill_color="#00FF00", fill_opacity=1,
                          stroke_width=0)
        second = Square(side_length=2, fill_color="#0000FF", fill_opacity=1,
                        stroke_width=0).shift(.7 * RIGHT)
        self.scene.add(first, retained, second)
        return first, retained, second

    def assert_retained_is_in_front_of_first(self, paths):
        canvas = self.scene.canvas
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas.width, canvas.height)
        context = cairo.Context(surface)
        context.set_source_rgb(1, 1, 1)
        context.paint()
        draw_paths(context, paths)
        surface.flush()
        # This interior point lies in first and retained, but not second.
        x, y = map(round, canvas.points(np.array([-.8, 0, 0]))[:2])
        pixels = np.frombuffer(surface.get_data(), dtype=np.uint32).reshape(
            canvas.height, surface.get_stride() // 4)
        self.assertEqual(int(pixels[y, x]), 0xFF00FF00,
                         "Automatic grouping changed the retained shape's occlusion")
        surface.finish()

    def test_subset_regrouping_keeps_intervening_path_order_and_cleanup(self):
        first, retained, second = self.overlapping_shapes()
        group = VGroup(first, second)
        self.scene.play(group.animate.shift(.2 * UP), run_time=.2)
        self.scene.checkpoint("Moved", "Moved")
        self.assertIn(group, self.scene.mobjects)
        self.assert_retained_is_in_front_of_first(self.scene.current_paths())
        self.assert_retained_is_in_front_of_first(self.scene.handout_states[-1]["full_paths"])
        timeline = self.scene.export()
        for time in (0, .1, .2):
            self.assert_retained_is_in_front_of_first(scene_paths_at(timeline, time))
        self.scene.play(FadeOut(group), run_time=.2)
        self.scene.wait(.1)
        family = self.scene.get_mobject_family_members()
        self.assertIn(retained, family)
        self.assertNotIn(first, family)
        self.assertNotIn(second, family)
        final = visible(scene_paths_at(self.scene.export(), .5))
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["fill"], [0, 1, 0, 1])

    def test_fading_a_new_subset_group_keeps_intervening_path_order(self):
        first, retained, second = self.overlapping_shapes()
        self.scene.play(FadeOut(VGroup(first, second)), run_time=.2)
        timeline = self.scene.export()
        for time in (0, .1, .2):
            self.assert_retained_is_in_front_of_first(scene_paths_at(timeline, time))
        self.assertEqual(visible(self.scene.current_paths())[0]["fill"], [0, 1, 0, 1])

    def test_authored_order_change_after_regrouping_is_still_rejected(self):
        first, _retained, second = self.overlapping_shapes()
        self.scene.play(VGroup(first, second).animate.shift(.2 * UP), run_time=.2)
        self.scene.mobjects.reverse()
        with self.assertRaisesRegex(ValueError, "reorder visible"):
            self.scene.record()

    def test_recording_order_is_independent_between_layers(self):
        upper = Square(fill_opacity=1, stroke_width=0).set_z_index(1)
        lower = Circle(fill_opacity=1, stroke_width=0).set_z_index(-1)
        self.scene.add(upper, lower)
        self.scene.play(VGroup(upper, lower).animate.shift(.2 * UP), run_time=.2)
        timeline = self.scene.export()
        self.assertEqual([self.scene.object_layers[id(item)] for item in (upper, lower)], [1, -1])
        self.assertEqual(scene_paths_at(timeline, .2)[0]["commands"],
                         next(track["commands"] for key, track in self.scene.tracks.items()
                              if key[0] == id(lower)))

    def test_hidden_authored_reorder_is_not_accepted_by_play_setup(self):
        for regroup in (False, True):
            with self.subTest(regroup=regroup):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                first, retained, second = self.overlapping_shapes()
                second.set_opacity(0)
                self.scene.record()
                # Only the hidden shape moves in the traversal, so rejection
                # must occur when it becomes visible, not in this record.
                self.scene.mobjects[:] = [second, first, retained]
                self.scene.record()
                target = VGroup(first, second) if regroup else second
                with self.assertRaisesRegex(ValueError, "reorder visible"):
                    self.scene.play(target.animate.set_opacity(1), run_time=.2)

    def test_introducer_registration_keeps_subset_occlusion(self):
        for mode in ("direct", "group", "succession", "nested-succession"):
            with self.subTest(mode=mode):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                first, retained, second = self.overlapping_shapes()
                group = VGroup(first, second)
                animation = FadeIn(group, run_time=.2)
                if mode == "group":
                    animation = AnimationGroup(animation)
                elif mode in ("succession", "nested-succession"):
                    animation = Succession(Wait(run_time=.2), animation)
                    if mode == "nested-succession":
                        animation = AnimationGroup(animation)
                self.scene.play(animation)
                self.scene.checkpoint("Introduced", "Introduced")
                self.assertIn(group, self.scene.get_mobject_family_members())
                self.assert_retained_is_in_front_of_first(self.scene.current_paths())
                self.assert_retained_is_in_front_of_first(self.scene.handout_states[-1]["full_paths"])
                timeline = self.scene.export()
                for time in np.linspace(0, timeline["duration"], 9):
                    self.assert_retained_is_in_front_of_first(scene_paths_at(timeline, time))
                self.scene.play(FadeOut(group), run_time=.2)
                self.scene.wait(.1)
                final = visible(self.scene.current_paths())
                self.assertEqual(len(final), 1)
                self.assertEqual(final[0]["fill"], [0, 1, 0, 1])

    def test_introducer_does_not_accept_hidden_authored_reorder(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                first, retained, second = self.overlapping_shapes()
                second.set_opacity(0)
                self.scene.record()
                self.scene.mobjects[:] = [second, first, retained]
                self.scene.record()
                animation = FadeIn(VGroup(first, second), run_time=.2)
                if deferred:
                    animation = Succession(Wait(run_time=.2), animation)
                # Registration succeeds while the reordered child is hidden.
                # Its later reveal must still expose the authored permutation.
                self.scene.play(animation)
                with self.assertRaisesRegex(ValueError, "reorder visible"):
                    self.scene.play(second.animate.set_opacity(1), run_time=.2)

    def test_readding_a_present_object_does_not_accept_authored_reorder(self):
        first, _retained, _second = self.overlapping_shapes()
        self.scene.record()
        self.scene.add(first)
        with self.assertRaisesRegex(ValueError, "reorder visible"):
            self.scene.record()

    def test_explicit_group_order_operations_remain_checked(self):
        for operation in ("bring_to_front", "bring_to_back", "add_foreground_mobjects"):
            with self.subTest(operation=operation):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                first, _retained, second = self.overlapping_shapes()
                self.scene.record()
                getattr(self.scene, operation)(VGroup(first, second))
                with self.assertRaisesRegex(ValueError, "reorder visible"):
                    self.scene.record()


if __name__ == "__main__":
    unittest.main(verbosity=2)
