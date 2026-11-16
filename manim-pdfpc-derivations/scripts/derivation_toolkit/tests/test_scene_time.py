"""Actual Manim edits, waits and callbacks share the exported scene clock."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from manim import FadeOut, Line, ORIGIN, RIGHT, Rotate, UP, Wait, linear

from ..animation.vector_scene import VectorScene, state_at, write_json
from .support import PALETTES, configure


class SceneTime(unittest.TestCase):
    def setUp(self):
        configure("light")
        self.scene = VectorScene(PALETTES["light"], sample_rate=10)

    def line(self):
        line = Line(ORIGIN, RIGHT, stroke_width=2)
        self.scene.add(line)
        return line

    def round_trip(self, scene):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "timeline.json"
            write_json(destination, {"version": 1, "scenes": [scene.export()]})
            result = json.loads(destination.read_text())["scenes"][0]
        for track in result["objects"]:
            times = [frame["time"] for frame in track["keyframes"]]
            self.assertTrue(all(a < b for a, b in zip(times, times[1:])), times)
        return result

    def test_remove_and_move_preserve_the_completed_hold(self):
        for operation in ("remove", "move", "style"):
            with self.subTest(operation=operation):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                line = self.line()
                self.scene.wait(1)
                before = state_at(self.scene.export()["objects"][0], .5)
                if operation == "remove":
                    self.scene.remove(line)
                elif operation == "move":
                    line.shift(UP)
                else:
                    line.set_opacity(.25)
                self.scene.checkpoint("Edited", "Edited", pause=False)
                full = self.scene.handout_states[-1]["full_paths"]
                self.assertEqual(full, self.scene.current_paths())
                self.scene.wait(.5)
                track = self.round_trip(self.scene)["objects"][0]
                for time in (.5, .999, math.nextafter(1, -math.inf)):
                    state = state_at(track, time)
                    self.assertEqual(state["coords"], before["coords"])
                    self.assertEqual(state["stroke"], before["stroke"])
                for time in (1, 1.25, 1.5):
                    state = state_at(track, time)
                    if operation == "remove":
                        self.assertEqual(state["stroke"][3], 0)
                        self.assertEqual(full, [])
                    elif operation == "style":
                        self.assertEqual(state["stroke"][3], .25)
                    else:
                        actual = np.array(state["coords"]).reshape(-1, 2)
                        expected = np.array(before["coords"]).reshape(-1, 2) + [0, -100]
                        np.testing.assert_allclose(actual, expected, atol=1e-5)
                    if full:
                        for field in ("coords", "fill", "stroke", "stroke_width"):
                            self.assertEqual(state[field], full[0][field])

    def test_multiple_edits_keep_the_original_pre_boundary_state(self):
        line = self.line()
        self.scene.wait(1)
        before = state_at(self.scene.export()["objects"][0], .5)
        line.shift(RIGHT)
        self.scene.record()
        line.shift(UP)
        self.scene.record()
        line.set_opacity(.25)
        self.scene.record()
        self.scene.wait(.5)
        track = self.round_trip(self.scene)["objects"][0]
        old = state_at(track, math.nextafter(1, -math.inf))
        self.assertEqual(old["coords"], before["coords"])
        self.assertEqual(old["stroke"], before["stroke"])
        final = state_at(track, 1)
        expected = np.array(before["coords"]).reshape(-1, 2) + [100, -100]
        np.testing.assert_allclose(np.array(final["coords"]).reshape(-1, 2), expected)
        self.assertEqual(final["stroke"][3], .25)

    def test_new_paths_appear_at_their_introduction_not_during_the_hold(self):
        self.line()
        self.scene.wait(1)
        introduced = Line(UP, UP + RIGHT, color="#FF0000")
        self.scene.add(introduced)
        self.scene.wait(.5)
        track = self.round_trip(self.scene)["objects"][1]
        for time in (0, .5, .99, math.nextafter(1, -math.inf)):
            self.assertEqual(state_at(track, time)["stroke"][3], 0)
        self.assertEqual(state_at(track, 1)["stroke"][3], 1)
        self.assertEqual(state_at(track, 1.5)["stroke"][3], 1)

    def test_initial_edits_do_not_require_a_negative_timestamp(self):
        line = self.line()
        self.scene.record()
        line.shift(UP)
        self.scene.record()
        self.scene.wait(.5)
        track = self.round_trip(self.scene)["objects"][0]
        self.assertEqual(track["keyframes"][0]["time"], 0)
        np.testing.assert_allclose(state_at(track, 0)["coords"][:2], [600, 225])

    def test_occupied_predecessor_fails_without_overwriting_its_samples(self):
        line = self.line()
        self.scene.wait(1)
        key = next(iter(self.scene.tracks))
        frames = self.scene.tracks[key]["keyframes"]
        # A previously authored sample occupies the only representable slot.
        # It must not be mistaken for a pre-cut state created by the recorder.
        frames.insert(-1, {**deepcopy(frames[-1]), "time": math.nextafter(1, -math.inf)})
        before = deepcopy(frames)
        line.shift(UP)
        with self.assertRaisesRegex(ValueError, "free timestamp"):
            self.scene.record()
        self.assertEqual(frames, before)

    def test_zero_wait_and_zero_time_animation_do_not_create_duplicate_times(self):
        line = self.line()
        self.scene.wait(.2)
        self.scene.wait(0)
        self.scene.play(line.animate.shift(UP), run_time=0)
        self.scene.wait(.2)
        track = self.round_trip(self.scene)["objects"][0]
        np.testing.assert_allclose(state_at(track, math.nextafter(.2, -math.inf))["coords"][:2],
                                   [600, 325])
        np.testing.assert_allclose(state_at(track, .2)["coords"][:2], [600, 225])
        self.assertAlmostEqual(self.scene.time, .4)

    def test_wait_default_and_explicit_static_holds_keep_sparse_endpoints(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                line = self.line()
                calls = []
                line.add_updater(lambda mobject: calls.append(self.scene.time))
                self.scene.wait(.3, **({"frozen_frame": True} if explicit else {}))
                self.assertEqual(calls, [])
                frames = next(iter(self.scene.tracks.values()))["keyframes"]
                self.assertEqual([frame["time"] for frame in frames], [0, .3])

    def test_wait_updates_time_based_mobjects_and_its_export(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                line = self.line()
                line.add_updater(lambda mobject, dt: mobject.shift(RIGHT * dt))
                self.scene.wait(.4, **({"frozen_frame": False} if explicit else {}))
                np.testing.assert_allclose(line.get_start(), [.4, 0, 0], atol=1e-12)
                self.assertAlmostEqual(self.scene.time, .4)
                track = self.round_trip(self.scene)["objects"][0]
                for time in (.1, .2, .4):
                    np.testing.assert_allclose(state_at(track, time)["coords"][:2],
                                               [600 + 100 * time, 325], atol=1e-5)

    def test_scene_updaters_and_explicit_position_updates_run_during_wait(self):
        for kind in ("scene", "position", "always"):
            with self.subTest(kind=kind):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                line = self.line()
                if kind == "scene":
                    self.scene.add_updater(lambda dt: line.shift(RIGHT * dt))
                else:
                    line.add_updater(lambda mobject: mobject.put_start_and_end_on(
                        self.scene.time * RIGHT, (self.scene.time + 1) * RIGHT))
                if kind == "always":
                    self.scene.always_update_mobjects = True
                self.scene.wait(.3, **({"frozen_frame": False} if kind == "position" else {}))
                np.testing.assert_allclose(line.get_start(), [.3, 0, 0], atol=1e-12)

    def test_frozen_wait_and_pause_skip_both_kinds_of_updater(self):
        for operation in ("wait", "pause", "play"):
            with self.subTest(operation=operation):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                line = self.line()
                callbacks = []
                line.add_updater(lambda mobject, dt: callbacks.append(("object", dt)))
                self.scene.add_updater(lambda dt: callbacks.append(("scene", dt)))
                if operation == "wait":
                    self.scene.wait(.3, frozen_frame=True)
                elif operation == "pause":
                    self.scene.pause(.3)
                else:
                    self.scene.play(Wait(run_time=.3, frozen_frame=True))
                self.assertEqual(callbacks, [])
                self.assertAlmostEqual(self.scene.time, .3)

    def test_callbacks_observe_current_absolute_time_during_play_and_wait(self):
        for operation in ("play", "wait"):
            with self.subTest(operation=operation):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                observed = self.line()
                self.scene.wait(.5)
                object_times, scene_times = [], []
                observed.add_updater(lambda mobject, dt: object_times.append((dt, self.scene.time)))
                self.scene.add_updater(lambda dt: scene_times.append((dt, self.scene.time)))
                with patch.object(self.scene.renderer, "render", side_effect=AssertionError("raster rendering")):
                    if operation == "play":
                        moving = Line(UP, UP + RIGHT)
                        self.scene.add(moving)
                        self.scene.play(moving.animate.shift(RIGHT), run_time=.3, rate_func=linear)
                    else:
                        self.scene.wait(.3)
                for events in (object_times, scene_times):
                    positive = [(dt, time) for dt, time in events if dt > 0]
                    np.testing.assert_allclose(positive, [[.1, .6], [.1, .7], [.1, .8]], atol=1e-12)
                self.assertAlmostEqual(self.scene.time, .8)

    def test_wait_conditions_stop_at_the_current_recorded_sample(self):
        for operation in ("wait", "wait_until", "play"):
            with self.subTest(operation=operation):
                self.scene = VectorScene(PALETTES["light"], sample_rate=10)
                line = self.line()
                self.scene.wait(.5)
                line.add_updater(lambda mobject, dt: mobject.shift(RIGHT * dt))
                sampled = []

                def condition():
                    sampled.append((self.scene.time, line.get_start()[0]))
                    return line.get_start()[0] >= .35

                if operation == "wait":
                    self.scene.wait(1, stop_condition=condition)
                elif operation == "wait_until":
                    self.scene.wait_until(condition, max_time=1)
                else:
                    self.scene.play(Wait(run_time=1, stop_condition=condition))
                self.assertAlmostEqual(self.scene.time, .9)
                self.assertAlmostEqual(self.scene.vector_time, .9)
                np.testing.assert_allclose(sampled[-1], [.9, .4], atol=1e-12)
                self.assertEqual(self.round_trip(self.scene)["duration"], .9)

    def test_true_initial_stop_condition_does_not_advance_the_clock(self):
        self.line()
        self.scene.wait(.5)
        self.scene.wait_until(lambda: True, max_time=1)
        self.assertAlmostEqual(self.scene.vector_time, .5)
        self.assertAlmostEqual(self.scene.time, .5)

    def test_frozen_wait_with_a_condition_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "stop condition"):
            self.scene.wait(.2, frozen_frame=True, stop_condition=lambda: True)

    def test_invalid_durations_do_not_change_recorded_time(self):
        self.line()
        self.scene.wait(.2)
        for duration in (-1, math.inf, math.nan):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, "finite|nonnegative|positive"):
                    self.scene.wait(duration)
                self.assertAlmostEqual(self.scene.vector_time, .2)

    def test_ordinary_rotation_and_fade_keep_their_endpoints(self):
        line = self.line()
        self.scene.wait(.2)
        self.scene.play(Rotate(line, math.pi / 2, about_point=ORIGIN), run_time=.2)
        self.scene.play(FadeOut(line), run_time=.2)
        timeline = self.round_trip(self.scene)
        track = timeline["objects"][0]
        np.testing.assert_allclose(state_at(track, .4)["coords"][-2:], [600, 225], atol=1e-5)
        self.assertEqual(state_at(track, .6)["stroke"][3], 0)
        self.assertAlmostEqual(self.scene.time, .6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
