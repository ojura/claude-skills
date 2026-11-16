"""Validate text when each real Manim animation creates its target."""
from __future__ import annotations

from unittest.mock import patch

import fitz
import numpy as np
from manim import (AnimationGroup, ApplyMethod, FadeIn, FadeOut, Mobject,
                   ORIGIN, RIGHT, Succession, Transform, Wait)

from .semantic_support import SemanticAssertions, flattened
from .support import PALETTES, configure
from ..animation.authoring import AuthoringScene
from ..animation.tex_semantics import TextMappingError
from ..animation.vector_scene import scene_paths_at


class SampledScene(AuthoringScene):
    def __init__(self, *args, **kwargs):
        self.samples = []
        super().__init__(*args, **kwargs)

    def record(self):
        super().record()
        self.samples.append((self.vector_time, self.current_paths()))


def composed(animation, mode):
    if mode == "direct":
        return animation
    if mode == "group":
        return AnimationGroup(animation)
    delayed = Succession(Wait(run_time=.2), animation)
    return delayed if mode == "succession" else AnimationGroup(delayed)


class SemanticAnimationLifecycle(SemanticAssertions):
    scene_type = SampledScene

    def test_same_text_fades_and_motion_follow_their_scheduled_start(self):
        for theme in ("light", "dark"):
            for kind in ("math", "prose"):
                for action in ("fade-in", "fade-out", "method", "animate"):
                    for mode in ("direct", "group", "succession", "nested"):
                        with self.subTest(theme=theme, kind=kind, action=action, mode=mode):
                            configure(theme)
                            scene = SampledScene(PALETTES[theme], sample_rate=20)
                            word = "x" if kind == "math" else "office"
                            text = getattr(scene, kind)(word)
                            initial = text.get_all_points().copy()
                            if action != "fade-in":
                                scene.add(text)
                            if action == "fade-in":
                                animation = FadeIn(text, run_time=.2)
                            elif action == "fade-out":
                                animation = FadeOut(text, run_time=.2)
                            elif action == "method":
                                animation = ApplyMethod(text.shift, RIGHT, run_time=.2)
                            else:
                                animation = text.animate(run_time=.2).shift(RIGHT)
                            scene.play(composed(animation, mode))
                            delayed = mode in ("succession", "nested")
                            self.assertAlmostEqual(scene.vector_time, .4 if delayed else .2)
                            timeline = scene.export()
                            if delayed:
                                early = next(paths for time, paths in scene.samples
                                             if abs(time - .1) < 1e-9)
                                if action == "fade-in":
                                    self.assertEqual(early, [], "the later FadeIn began during Wait")
                                else:
                                    np.testing.assert_allclose(early[0]["coords"],
                                        scene_paths_at(timeline, 0)[0]["coords"], atol=1e-6)
                                    self.assertTrue(all(path["fill"][3] == 1 for path in early))
                            final = scene.current_paths()
                            if action == "fade-out":
                                self.assertEqual(final, [])
                                self.assertTrue(all(path["fill"][3] == 0
                                                    for path in scene_paths_at(timeline, scene.vector_time)))
                            else:
                                expected = initial + RIGHT if action in ("method", "animate") else initial
                                np.testing.assert_allclose(text.get_all_points(), expected, atol=1e-10)
                                path = self.pdf([], theme=theme, paths=final)
                                self.assertEqual(flattened(self.poppler_text(path)), word)
                                with fitz.open(path) as document:
                                    self.assert_visible_font_text(document[0])
                                    self.assertEqual(flattened(document[0].get_text()), word)

    def test_later_method_target_uses_the_completed_earlier_state(self):
        text = self.scene.math("x")
        self.scene.add(text)
        initial = text.get_all_points().copy()
        first = ApplyMethod(text.shift, RIGHT, run_time=.2)
        second = ApplyMethod(text.scale, 2, {"about_point": ORIGIN}, run_time=.2)
        observed = []
        create_target = second.create_target

        def target_after_first():
            observed.append(text.get_all_points().copy())
            return create_target()

        with patch.object(second, "create_target", side_effect=target_after_first) as factory:
            self.scene.play(AnimationGroup(Succession(first, second)))
            self.assertIs(second.create_target, factory)
            self.assertEqual(factory.call_count, 1)
        self.assertEqual(len(observed), 1)
        np.testing.assert_allclose(observed[0], initial + RIGHT, atol=1e-10)
        np.testing.assert_allclose(text.get_all_points(), 2 * (initial + RIGHT), atol=1e-10)
        self.assertAlmostEqual(self.scene.vector_time, .4)
        self.assertEqual(flattened(self.poppler_text(self.pdf([text]))), "x")

    def test_different_and_empty_targets_are_rejected_at_the_delayed_start(self):
        for target_kind in ("different", "empty"):
            for mode in ("direct", "succession", "nested"):
                for duration in (0, .2):
                    with self.subTest(target=target_kind, mode=mode, duration=duration):
                        scene = SampledScene(PALETTES["light"], sample_rate=20)
                        text = scene.math("x")
                        target = scene.math("y") if target_kind == "different" else Mobject()
                        scene.add(text)
                        initial = text.get_all_points().copy()
                        animation = Transform(text, target, run_time=duration)
                        with patch.object(animation, "create_target", wraps=animation.create_target) as factory, \
                                patch.object(text, "align_data", wraps=text.align_data) as align:
                            with self.assertRaisesRegex(TextMappingError, "changes TeX content/topology"):
                                scene.play(composed(animation, mode))
                            self.assertEqual(factory.call_count, 1,
                                             "deferred targets must be checked at their real creation")
                            self.assertEqual(align.call_count, 0,
                                             "reject before changing the source glyph topology")
                            self.assertIs(animation.create_target, factory)
                        np.testing.assert_array_equal(text.get_all_points(), initial)
                        self.assertTrue(scene.samples)
                        if mode != "direct":
                            self.assertGreaterEqual(scene.vector_time, .15)
                        for _time, paths in scene.samples:
                            self.assertEqual([path["glyph"].source.copy_text for path in paths], ["x"])

    def test_lazy_different_content_cannot_escape_validation(self):
        scene = self.scene
        text = scene.math("x")
        target = scene.math("y")
        scene.add(text)
        calls = []

        class DeferredContent(Transform):
            def create_target(self):
                calls.append(scene.vector_time)
                return target

        animation = DeferredContent(text, run_time=.2)
        with self.assertRaisesRegex(TextMappingError, "changes TeX content/topology"):
            scene.play(AnimationGroup(Succession(Wait(run_time=.2), animation)))
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0], .1)
        self.assertNotIn("create_target", animation.__dict__)
        self.assertNotIn("begin", animation.__dict__)

    def test_animation_methods_and_classes_are_restored_after_play(self):
        original_begin, original_factory = Transform.begin, Transform.create_target
        text = self.scene.prose("office")
        self.scene.add(text)
        animation = ApplyMethod(text.shift, RIGHT, run_time=.1)
        for _ in range(2):
            self.scene.play(Succession(Wait(run_time=.1), animation))
            self.assertNotIn("create_target", animation.__dict__)
            self.assertNotIn("begin", animation.__dict__)
            self.assertIs(Transform.begin, original_begin)
            self.assertIs(Transform.create_target, original_factory)
        self.assertEqual(flattened(self.poppler_text(self.pdf([text]))), "office")

    def test_adapters_are_restored_when_animation_code_raises(self):
        for phase in ("create_target", "begin", "clean_up_from_scene"):
            with self.subTest(phase=phase):
                scene = SampledScene(PALETTES["light"], sample_rate=20)
                text = scene.math("x")
                scene.add(text)
                animation = ApplyMethod(text.shift, RIGHT, run_time=.1)
                with patch.object(animation, phase, side_effect=RuntimeError("animation failure")) as original:
                    with self.assertRaisesRegex(RuntimeError, "animation failure"):
                        scene.play(Succession(Wait(run_time=.1), animation))
                    self.assertIs(getattr(animation, phase), original)
                self.assertNotIn("create_target", animation.__dict__)
                self.assertNotIn("begin", animation.__dict__)

    def test_custom_begin_without_a_factory_retains_target_checking(self):
        class OwnBegin(Transform):
            def begin(self):
                # A subclass can prepare a target without Transform.begin.
                # The check must still run for this started child.
                self.starting_mobject = self.mobject.copy()

        text, target = self.scene.math("x"), self.scene.math("y")
        self.scene.add(text)
        animation = OwnBegin(text, target, run_time=.1)
        with patch.object(animation, "create_target", wraps=animation.create_target) as factory:
            with self.assertRaisesRegex(TextMappingError, "changes TeX content/topology"):
                self.scene.play(Succession(Wait(run_time=.1), animation))
            self.assertEqual(factory.call_count, 0)
            self.assertIs(animation.create_target, factory)
        self.assertNotIn("begin", animation.__dict__)
