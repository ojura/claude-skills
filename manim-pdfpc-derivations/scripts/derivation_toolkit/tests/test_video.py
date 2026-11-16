"""Exercise actual PDF/Cairo/FFmpeg output and failure cleanup."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
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
