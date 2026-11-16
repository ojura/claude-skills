"""Exercise actual PDF/Cairo/FFmpeg output and failure cleanup."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cairo
import fitz
import numpy as np
from PIL import Image

from ..catalog import Catalog
from ..cli import main as project_command
from .. import export
from ..web import render_video


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Real video checks require FFmpeg and ffprobe")
class VideoExport(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="video export ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        (self.project / "presentations.json").write_text(json.dumps({
            "version": 2, "default": "article", "decks": [{
                "id": "article", "title": "Independent video fixture",
                "source": "article.pdf", "build": {"kind": "external"},
            }],
        }))
        self.pdf = self.project / "article.pdf"
        with fitz.open() as document:
            page = document.new_page(width=320, height=240)
            page.draw_rect(fitz.Rect(0, 0, 320, 240), color=None, fill=(.1, .2, .3))
            page.insert_text((20, 40), "A real PDF page", color=(1, 1, 1))
            document.save(self.pdf)
        self.output = self.project / "build/clip.mp4"
        self.output.parent.mkdir()
        self.catalog = Catalog(self.project)
        self.args = argparse.Namespace(
            deck="article", pdf=self.pdf, vectors=None, page_map=None,
            output=self.output, theme="light", first_page=1, last_page=None,
            width=320, fps=10, static_hold=.2, checkpoint_hold=.1,
            threads=1, snapshots=None, cache_root=None,
        )
        self.children = []
        self.real_popen = subprocess.Popen
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        # A failing baseline must not leave its known encoder behind.
        for child in self.children:
            if child.poll() is None:
                child.kill()
            if child.stdin and not child.stdin.closed:
                try:
                    child.stdin.close()
                except OSError:
                    pass
            child.wait(timeout=5)

    def start_encoder(self, command, **kwargs):
        child = self.real_popen(command, **kwargs)
        self.children.append(child)
        return child

    def render(self):
        with redirect_stdout(io.StringIO()):
            return render_video.render(self.args, self.catalog)

    def assert_cleaned(self):
        for child in self.children:
            self.assertIsNotNone(child.poll(), "The video exporter left its encoder running")
            self.assertTrue(child.stdin.closed, "The encoder input pipe remains open")
        self.assertFalse(list(self.output.parent.glob(".derivation-video-*")))

    def test_actual_movie_has_expected_stream_and_pixels(self):
        with patch.object(render_video.subprocess, "Popen", side_effect=self.start_encoder):
            result = self.render()
        self.assertEqual(result["frames"], 2)
        self.assertEqual((result["width"], result["height"]), (320, 240))
        info = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,nb_frames",
            "-of", "json", str(self.output),
        ], text=True))
        stream = info["streams"][0]
        self.assertEqual(stream["codec_name"], "h264")
        self.assertEqual(int(stream["nb_frames"]), 2)
        pixels = subprocess.check_output([
            "ffmpeg", "-v", "error", "-i", str(self.output), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ])
        offset = (120 * 320 + 160) * 3
        self.assertTrue(all(abs(a-b) <= 4 for a, b in zip(pixels[offset:offset+3], (26, 51, 77))))
        self.assert_cleaned()

    def test_deck_and_chapter_titles_round_trip_through_ffprobe(self):
        title = "Frame \\theta_B = 3; # report\nnext line"
        chapter = "A \\ B = C; # step"
        metadata = render_video.chapters([
            render_video.Section(1, chapter, 0, 0, 2, False),
        ], 10, title)
        result = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-f", "ffmetadata",
            "-show_entries", "format_tags=title:chapter_tags=title",
            "-of", "json", "-i", "pipe:0",
        ], input=metadata, text=True))
        self.assertEqual(result["format"]["tags"]["title"], title.replace("\n", " "))
        self.assertEqual(result["chapters"][0]["tags"]["title"], chapter)

    def test_existing_output_is_preserved_without_launching_encoder(self):
        original = b"previous delivery"
        self.output.write_bytes(original)
        with patch.object(render_video.subprocess, "Popen") as launch:
            with self.assertRaises(FileExistsError):
                self.render()
            launch.assert_not_called()
        self.assertEqual(self.output.read_bytes(), original)

    def test_original_timeline_reaches_its_final_frame_in_the_encoded_movie(self):
        with fitz.open(self.pdf) as document:
            page = document[0]
            page.insert_link({"kind": fitz.LINK_URI, "uri": "pdfpc-vector:move",
                              "from": fitz.Rect(40, 60, 240, 210)})
            document.saveIncr()
        shape = {"fill": [1, 0, 0, 1], "stroke": [0, 0, 0, 0], "stroke_width": 0}
        vectors = self.pdf.with_suffix(".vector.json")
        vectors.write_text(json.dumps({"version": 1, "scenes": [{
            "id": "move", "width": 100, "height": 75, "duration": .4,
            "background": [1, 1, 1, 1],
            "checkpoints": [{"time": 0, "label": "Start"}, {"time": .4, "label": "End"}],
            "objects": [{"id": "square", "z": 0, "commands": "MLLLZ",
                         "keyframes": [
                             {"time": 0, "coords": [10, 10, 30, 10, 30, 30, 10, 30], **shape},
                             {"time": .4, "coords": [50, 10, 70, 10, 70, 30, 50, 30], **shape},
                         ]}],
        }]}))
        result = self.render()
        self.assertEqual(result["frames"], 34)
        frames = subprocess.check_output([
            "ffmpeg", "-v", "error", "-i", str(self.output),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ])
        frame_size = 320 * 240 * 3
        self.assertEqual(len(frames), 34 * frame_size)
        for number, x, expected in ((0, 70, (255, 0, 0)), (0, 150, (255, 255, 255)),
                                    (33, 70, (255, 255, 255)), (33, 150, (255, 0, 0))):
            offset = number * frame_size + (90 * 320 + x) * 3
            self.assertTrue(all(abs(a-b) <= 5 for a, b in zip(frames[offset:offset+3], expected)),
                            (number, x, list(frames[offset:offset+3])))
        self.assert_cleaned()

    def test_fitted_scene_replaces_fractional_poster_and_preserves_pdf_margins(self):
        fixture = Path(__file__).with_name("fixtures") / "format-options.vector.json"
        for width in (320, 640):
            for region_size in ((240, 144), (160, 160)):
                for theme, colour in (("light", [1, 1, 1, 1]), ("dark", [.2, .2, .2, 1])):
                    with self.subTest(width=width, region_size=region_size, theme=theme):
                        scene = json.loads(fixture.read_text())["scenes"][0]
                        scene["background"] = colour
                        for item in scene["objects"]:
                            initial = item["keyframes"][0]
                            item["keyframes"].append({
                                "time": scene["duration"], "fill": [*initial["fill"][:3], 0],
                                "stroke": [0, 0, 0, 0], "stroke_width": 0,
                            })
                        # A visible path outside the scene must not enter the PDF margin.
                        scene["objects"].append({"id": "outside-top", "commands": "MLLLZ",
                            "keyframes": [{"time": 0, "coords": [-20, -20, 140, -20, 140, -5, -20, -5],
                                           "fill": [0, 0, 1, 1], "stroke": [0, 0, 0, 0], "stroke_width": 0}]})
                        rw, rh = region_size
                        region = fitz.Rect(40.25, 50.375, 40.25 + rw, 50.375 + rh)
                        scale = min(rw / scene["width"], rh / scene["height"])
                        left = region.x0 + (rw - scene["width"] * scale) / 2
                        top = region.y0 + (rh - scene["height"] * scale) / 2
                        fitted = (left, top, left + scene["width"] * scale,
                                  top + scene["height"] * scale)
                        with fitz.open() as document:
                            page = document.new_page(width=320, height=240)
                            page.draw_rect(page.rect, color=None, fill=(.1, .2, .3))
                            # The static poster has a saturated shape touching every edge.
                            page.draw_rect(fitz.Rect(fitted), color=None, fill=(1, 0, 1))
                            page.insert_link({"kind": fitz.LINK_URI, "uri": "pdfpc-vector:" + scene["id"],
                                              "from": region})
                            document.save(self.pdf)
                        self.pdf.with_suffix(".vector.json").write_text(json.dumps({"version": 1, "scenes": [scene]}))
                        name = f"composition-{width}-{rw}-{rh}-{theme}"
                        self.output = self.project / "build" / (name + ".mp4")
                        self.args.output = self.output
                        self.args.snapshots = self.project / "build" / (name + "-frames")
                        self.args.width, self.args.fps, self.args.theme = width, 2, theme
                        with patch.object(render_video.subprocess, "Popen", side_effect=self.start_encoder):
                            result = self.render()
                        height = result["height"]
                        pixel_scale = width / 320
                        x0, y0 = (math.floor(value * pixel_scale) for value in fitted[:2])
                        x1, y1 = (math.ceil(value * pixel_scale) for value in fitted[2:])
                        with Image.open(self.args.snapshots / "page-01-time-002.00.png") as image:
                            final = np.asarray(image.convert("RGB"))
                        expected = np.array([round(value * 255) for value in colour[:3]], dtype=np.uint8)
                        covered = np.zeros((height, width), dtype=bool)
                        covered[y0:y1, x0:x1] = True
                        damaged = np.any(final != expected, axis=2) & covered
                        self.assertFalse(damaged.any(),
                                         f"{np.count_nonzero(damaged)} fitted pixels retain the old PDF poster")
                        with fitz.open(self.pdf) as document:
                            poster = document[0].get_pixmap(matrix=fitz.Matrix(pixel_scale, pixel_scale),
                                                            alpha=False, annots=False)
                            baseline = np.frombuffer(poster.samples, dtype=np.uint8).reshape(height, width, 3)
                        np.testing.assert_array_equal(final[~covered], baseline[~covered])
                        frames = subprocess.check_output([
                            "ffmpeg", "-v", "error", "-i", str(self.output),
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                        ])
                        decoded = np.frombuffer(frames, dtype=np.uint8).reshape(-1, height, width, 3)
                        self.assertEqual(len(decoded), result["frames"])
                        self.assertEqual(result["frames"], 10)
                        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
                        np.testing.assert_allclose(decoded[0, cy, cx], [255, 0, 0], rtol=0, atol=5)
                        np.testing.assert_allclose(decoded[-1, cy, cx], expected, rtol=0, atol=5)
                        # Lossless snapshots assert edge composition exactly; codec
                        # tolerance applies only to these decoded interior colours.
                        self.assert_cleaned()

    def test_output_created_during_encoding_is_never_replaced(self):
        original = b"a competing producer owns this path"

        def competing_producer(command, **kwargs):
            self.output.write_bytes(original)
            return self.start_encoder(command, **kwargs)

        with patch.object(render_video.subprocess, "Popen", side_effect=competing_producer):
            with self.assertRaises(FileExistsError):
                self.render()
        self.assertEqual(self.output.read_bytes(), original)
        self.assert_cleaned()

    def test_dangling_output_symlink_is_preserved(self):
        missing = self.output.parent / "absent.mp4"
        self.output.symlink_to(missing)
        with patch.object(render_video.subprocess, "Popen") as launch:
            with self.assertRaises(FileExistsError):
                self.render()
            launch.assert_not_called()
        self.assertTrue(self.output.is_symlink())
        self.assertFalse(missing.exists())

    def test_cairo_startup_failure_cleans_encoder(self):
        for constructor in ("ImageSurface", "Context"):
            with self.subTest(constructor=constructor):
                with patch.object(render_video.subprocess, "Popen", side_effect=self.start_encoder), \
                        patch.object(cairo, constructor, side_effect=RuntimeError("allocation failed")):
                    with self.assertRaisesRegex(RuntimeError, "allocation failed"):
                        self.render()
                self.assert_cleaned()
                self.assertFalse(self.output.exists())

    def test_render_interruption_cleans_encoder_and_partial_movie(self):
        with patch.object(render_video.subprocess, "Popen", side_effect=self.start_encoder), \
                patch.object(fitz.Page, "get_pixmap", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.render()
        self.assert_cleaned()
        self.assertFalse(self.output.exists())

    def test_shared_command_renders_real_video(self):
        snapshots = self.project / "build/snapshots"
        arguments = ["video", "article", "--pdf", str(self.pdf), "--output", str(self.output),
                     "--fps", "10", "--width", "320", "--static-hold", ".2",
                     "--threads", "1", "--snapshots", str(snapshots)]
        # Use the known complete test interpreter without acquiring packages.
        # The bootstrap still executes the actual public CLI in a fresh process.
        with patch.object(export, "authoring_python", return_value=Path(sys.executable)) as select:
            self.assertEqual(project_command(arguments, default_project=self.project), 0)
        select.assert_called_once()
        self.assertTrue(self.output.is_file())
        self.assertEqual(len(list(snapshots.glob("*.png"))), 1)
        self.assertFalse(list(self.output.parent.glob(".derivation-video-*")))

    def test_bootstrap_preserves_arguments_cwd_and_selected_project(self):
        arguments = ["video", "article", "--pdf", "relative input.pdf", "--vectors", "motion.json",
                     "--page-map", "pages.json", "--output", "build/new clip.mp4", "--theme", "dark",
                     "--first-page", "2", "--last-page", "3", "--width", "640", "--fps", "24",
                     "--static-hold", "2.5", "--checkpoint-hold", ".5", "--threads", "2",
                     "--snapshots", "build/pictures", "--cache-root", str(self.project / "build/cache")]
        chosen = Path("/complete/environment/bin/python")
        with patch.object(export, "authoring_python", return_value=chosen) as select, \
                patch.object(render_video.subprocess, "run") as execute:
            self.assertEqual(project_command(arguments, default_project=self.project), 0)
        self.assertEqual(select.call_args.args[0].cache_root, self.project / "build/cache")
        command = execute.call_args.args[0]
        self.assertEqual(command, [str(chosen), "-B", "-m", "derivation_toolkit",
                         "--project", str(self.project), *arguments, "--in-authoring-environment"])
        self.assertNotIn("cwd", execute.call_args.kwargs)
        self.assertTrue(execute.call_args.kwargs["check"])
        self.assertEqual(execute.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(execute.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0],
                         str(Path(render_video.__file__).resolve().parents[2]))

    def test_options_are_shared_and_help_needs_no_render_dependencies(self):
        # The standalone module and shared command both import this lightweight
        # option definition. Blocking renderer imports catches help regressions.
        program = """
import importlib.abc, sys
class BlockRenderer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'manim', 'cairo', 'numpy', 'pypdf'}:
            raise AssertionError('Help imported renderer dependency: ' + fullname)
sys.meta_path.insert(0, BlockRenderer())
from derivation_toolkit.cli import main
main(['video', '--help'])
"""
        result = subprocess.run([sys.executable, "-B", "-c", program],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ("--pdf", "--vectors", "--page-map", "--snapshots", "--fps", "--cache-root"):
            self.assertIn(option, result.stdout)

    def test_early_encoder_failure_reports_diagnostic_and_reaps_child(self):
        def failing_encoder(_command, **kwargs):
            return self.start_encoder([
                sys.executable, "-c",
                "import sys; sys.stderr.write('encoder rejected fixture\\n'); sys.exit(17)",
            ], **kwargs)

        with patch.object(render_video.subprocess, "Popen", side_effect=failing_encoder):
            with self.assertRaisesRegex(RuntimeError, "encoder rejected fixture"):
                self.render()
        self.assert_cleaned()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
