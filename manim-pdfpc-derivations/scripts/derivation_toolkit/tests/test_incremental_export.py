"""Real scene exports keep shared metadata and every retained theme consistent."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import fitz

from ..catalog import Catalog
from ..export import export_deck
from ..paths import get_paths
from .. import publication
from .support import PALETTES


PROVIDER = r'''
import json
from pathlib import Path
from manim import Circle, RIGHT
from derivation_toolkit.animation.authoring import AuthoringScene
from derivation_toolkit.animation.layout import Canvas, HandoutLayout

class Sample(AuthoringScene):
    scene_id = "sample"
    canvas = Canvas(320, 180, 40)

    def construct(self):
        settings = json.loads(Path(__file__).with_name("settings.json").read_text())
        values = settings.get(self.scene_id, {})
        theme = "light" if self.palette["background"] == "#FFFFFF" else "dark"
        values = {**values, **values.get(theme, {})}
        self.handout_layout = HandoutLayout(columns=values.get("columns", 2))
        circle = Circle(radius=.4, stroke_width=0, fill_opacity=1,
                        fill_color=self.palette["accent"]).shift(
                            values.get("offset", 0) * RIGHT)
        label = self.math("x", size=20).shift(1.5 * RIGHT)
        self.add(circle, label)
        count = values.get("steps", 2)
        for index in range(count):
            if index:
                self.play(circle.animate.shift(.1 * RIGHT),
                          run_time=values.get("interval", .2))
            pause = not values.get("print_only", False) or index in (0, count-1)
            self.checkpoint(str(index), values.get("caption", "State") + " " + str(index),
                            crop=values.get("crop"), pause=pause)
        if values.get("fail", False):
            raise RuntimeError("Authored scene failed")

class Other(Sample):
    scene_id = "other"

SCENES = [Sample, Other]
'''


def file_bytes(folder):
    return {str(path.relative_to(folder)): path.read_bytes()
            for path in folder.rglob("*") if path.is_file()}


class IncrementalExport(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get("DERIVATION_TEST_BUILD", Path.cwd() / "build/test-toolkit"))
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="incremental-export-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.output = self.project / "build/figures"
        self.previews = self.project / "build/previews"
        (self.project / "scene.py").write_text(textwrap.dedent(PROVIDER))
        (self.project / "settings.json").write_text("{}")
        (self.project / "slides.tex").write_text("% Export-only test document\n")
        palette = "\n".join(
            r"\definecolor{fixture" + theme.title() + role.title() + "}{HTML}{" + color[1:] + "}"
            for theme, colors in PALETTES.items() for role, color in colors.items())
        (self.project / "palette.tex").write_text(palette + "\n")
        (self.project / "presentations.json").write_text(json.dumps({
            "version": 2, "default": "fixture",
            "palette": {"file": "palette.tex", "prefix": "fixture"},
            "decks": [{"id": "fixture", "title": "Export fixture", "source": "slides.tex",
                       "animation": {"provider": "scene.py", "assets": "generated",
                                     "manifest": "fixture.vector.json"}}],
        }))
        self.deck = Catalog(self.project).get()
        self.paths = get_paths(self.deck, self.project / "build")

    def configure_scene(self, **values):
        (self.project / "settings.json").write_text(json.dumps({"sample": values}))

    def export(self, themes=("light", "dark"), *, scene=None, previews=True):
        with redirect_stdout(io.StringIO()):
            export_deck(self.deck, list(themes), self.paths, self.output, scene_id=scene,
                        sample_rate=10, preview_output=self.previews if previews else None)

    def assert_complete(self):
        metadata = json.loads((self.output / "steps.json").read_text())
        expected_ids = {item["id"] for item in metadata["scenes"]}
        for theme in metadata["themes"]:
            timeline = json.loads((self.output / theme / metadata["manifest"]).read_text())
            scenes = {scene["id"]: scene for scene in timeline["scenes"]}
            self.assertEqual(set(scenes), expected_ids)
            for definition in metadata["scenes"]:
                scene = scenes[definition["id"]]
                self.assertEqual(scene["duration"], definition["duration"])
                self.assertEqual([point["time"] for point in scene["checkpoints"]],
                                 [step["time"] for step in definition["steps"] if step["full_figure"]])
                for step in definition["steps"]:
                    for name in (step["figure"], step["full_figure"]):
                        if name:
                            with fitz.open(self.output / theme / name) as pdf:
                                self.assertEqual(len(pdf), 1)
                                self.assertIn("x", pdf[0].get_text())
                                self.assertFalse(pdf[0].get_image_info())

    def test_single_theme_structural_change_preserves_previous_complete_export(self):
        self.export()
        self.assert_complete()
        before, previews = file_bytes(self.output), file_bytes(self.previews)
        self.configure_scene(steps=3, offset=.7)
        with self.assertRaisesRegex(ValueError, "both themes"):
            self.export(("light",), scene="sample")
        self.assertEqual(file_bytes(self.output), before)
        self.assertEqual(file_bytes(self.previews), previews)
        self.assert_complete()

    def test_full_export_rejects_disagreeing_theme_metadata_without_writing(self):
        self.output.mkdir(parents=True)
        (self.output / "notes.txt").write_text("Keep this user's file.\n")
        before = file_bytes(self.output)
        self.configure_scene(dark={"steps": 3})
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.export()
        self.assertEqual(file_bytes(self.output), before)
        self.assertEqual(file_bytes(self.previews), {})

    def test_single_theme_geometry_change_preserves_other_scenes_and_theme(self):
        self.export()
        before = file_bytes(self.output)
        (self.output / "notes.txt").write_text("Keep this user's file.\n")
        self.configure_scene(offset=.7)
        self.export(("light",), scene="sample")
        after = file_bytes(self.output)
        self.assert_complete()
        self.assertNotEqual(before["light/sample-step-01.pdf"], after["light/sample-step-01.pdf"])
        for name, content in before.items():
            if name.startswith("dark/") or "/other-" in name or name in ("steps.json", "steps.tex"):
                self.assertEqual(after[name], content, name)
        self.assertEqual(after["notes.txt"], b"Keep this user's file.\n")

    def test_same_count_metadata_changes_require_both_themes(self):
        self.configure_scene(steps=3)
        self.export()
        before, previews = file_bytes(self.output), file_bytes(self.previews)
        for change in ({"caption": "Changed"}, {"interval": .3}, {"columns": 3},
                       {"print_only": True}, {"crop": [0, 0, 300, 170]}):
            with self.subTest(change=change):
                self.configure_scene(steps=3, **change)
                with self.assertRaisesRegex(ValueError, "both themes"):
                    self.export(("light",), scene="sample")
                self.assertEqual(file_bytes(self.output), before)
                self.assertEqual(file_bytes(self.previews), previews)

    def test_render_failure_preserves_previous_figures_and_previews(self):
        self.export()
        before, previews = file_bytes(self.output), file_bytes(self.previews)
        self.configure_scene(offset=.7, dark={"fail": True})
        with self.assertRaisesRegex(RuntimeError, "Authored scene failed"):
            self.export()
        self.assertEqual(file_bytes(self.output), before)
        self.assertEqual(file_bytes(self.previews), previews)

    def test_complete_update_removes_only_obsolete_declared_figures_and_previews(self):
        self.configure_scene(steps=3)
        self.export()
        unrelated = self.output / "light/manual-step-03.pdf"
        unrelated.write_bytes(b"Keep this unrelated file")
        note = self.previews / "dark/notes.txt"
        note.write_text("Keep preview notes")
        self.configure_scene(steps=2)
        self.export(scene="sample")
        self.assert_complete()
        for theme in ("light", "dark"):
            for stem in ("sample-step-03", "sample-full-step-03"):
                self.assertFalse((self.output / theme / (stem + ".pdf")).exists())
                self.assertFalse((self.previews / theme / (stem + ".png")).exists())
        self.assertEqual(unrelated.read_bytes(), b"Keep this unrelated file")
        self.assertEqual(note.read_text(), "Keep preview notes")

    def test_new_theme_partial_scene_does_not_create_an_incomplete_theme(self):
        self.export(("light",))
        before = file_bytes(self.output)
        with self.assertRaisesRegex(ValueError, "both themes"):
            self.export(("dark",), scene="sample")
        self.assertEqual(file_bytes(self.output), before)
        self.export(("dark",))
        self.assert_complete()

    def test_publication_failure_restores_figures_metadata_and_previews(self):
        self.export()
        before, previews = file_bytes(self.output), file_bytes(self.previews)
        self.configure_scene(offset=.7)
        replace = publication.os.replace
        failed = False

        def fail_on_preview(source, destination):
            nonlocal failed
            if not failed and Path(source).name.startswith("new-") and self.previews in Path(destination).parents:
                failed = True
                raise OSError("Injected preview publication failure")
            return replace(source, destination)

        with patch.object(publication.os, "replace", side_effect=fail_on_preview):
            with self.assertRaisesRegex(OSError, "Injected preview publication failure"):
                self.export()
        self.assertTrue(failed)
        self.assertEqual(file_bytes(self.output), before)
        self.assertEqual(file_bytes(self.previews), previews)
        self.assertFalse(list(self.project.rglob(".derivation-publish-*")))

    def test_mismatched_retained_timeline_is_rejected_before_publication(self):
        self.export()
        filename = self.output / "dark/fixture.vector.json"
        timeline = json.loads(filename.read_text())
        timeline["scenes"][0]["duration"] += 1
        filename.write_text(json.dumps(timeline))
        before = file_bytes(self.output)
        with self.assertRaisesRegex(ValueError, "duration.*metadata"):
            self.export(("light",), scene="sample")
        self.assertEqual(file_bytes(self.output), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
