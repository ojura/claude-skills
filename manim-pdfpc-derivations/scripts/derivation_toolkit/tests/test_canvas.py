"""A scene owns visible and semantic coordinates even after another canvas is used."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

import fitz
import numpy as np

from ..animation.authoring import AuthoringScene
from ..animation.layout import Canvas, HandoutLayout
from ..animation.vector_scene import write_snapshot
from .support import PALETTES, configure


class IndependentCanvases(unittest.TestCase):
    def test_two_canvases_keep_moved_copied_labels_and_compact_states(self):
        configure("light")
        instances = []
        for canvas in (Canvas(720, 500, 80), Canvas(1280, 720, 100)):
            class Example(AuthoringScene):
                handout_layout = HandoutLayout(columns=3)
            Example.canvas = canvas
            scene = Example(PALETTES["light"])
            label = scene.prose("shared source", size=24).move_to([-1.4, .8, 0])
            copied = label.copy().scale(.7).rotate(.16).move_to([1.1, -.9, 0])
            scene.add(label, copied)
            scene.checkpoint("Initial", "Initial", handout=scene.compact(label, copied))
            scene.wait(.2)
            scene.checkpoint("Printable only", "Printable only", pause=False,
                             handout=scene.compact(label, copied))
            scene.wait(.2)
            scene.checkpoint("Final", "Final", handout=scene.compact(label, copied))
            paths = scene.current_paths()
            instances.append((scene, paths))
        folder = Path(os.environ.get("DERIVATION_TEST_BUILD", Path.cwd() / "build/test-toolkit"))
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="canvas-", dir=folder) as directory:
            for index, (scene, before) in enumerate(instances):
                self.assertEqual((scene.export()["width"], scene.export()["height"]),
                                 (scene.canvas.width, scene.canvas.height))
                self.assertEqual((len(scene.handout_states), len(scene.checkpoints)), (3, 2))
                after = scene.current_paths()
                self.assertEqual([item["coords"] for item in before], [item["coords"] for item in after])
                for a, b in zip(before, after):
                    if "glyph" in a:
                        np.testing.assert_array_equal(a["glyph"].baseline, b["glyph"].baseline)
                for kind, paths in (("full", after), ("compact", scene.handout_states[1]["paths"])):
                    path = Path(directory) / f"{index}-{kind}.pdf"
                    write_snapshot(path, paths, [1, 1, 1, 1], canvas=scene.canvas)
                    with fitz.open(path) as pdf:
                        self.assertEqual(tuple(pdf[0].rect)[2:], (scene.canvas.width, scene.canvas.height))
                        self.assertEqual(pdf[0].get_text().count("shared source"), 2)
                        self.assertTrue(pdf[0].get_fonts())
                        self.assertFalse(pdf[0].get_image_info())
