"""The managed authoring job must not silently skip real video regressions."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


class VideoCiPrerequisites(unittest.TestCase):
    def test_authoring_preflight_requires_both_video_executables(self):
        workflow = Path(__file__).resolve().parents[1] / "ci/github-actions.yml"
        body = workflow.read_text().split("derivation-assets:ci bash -euc '", 1)[1]
        preflight = textwrap.dedent(body.split("python3 -B -m unittest discover", 1)[0])
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        for missing in ("ffmpeg", "ffprobe", None):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory(prefix="video CI path ") as folder:
                for program in ("ffmpeg", "ffprobe"):
                    if program == missing:
                        continue
                    executable = Path(folder) / program
                    executable.write_text('#!/bin/sh\ntest "$1" = -version\n')
                    executable.chmod(0o755)
                result = subprocess.run([bash, "-euc", preflight],
                                        env={**os.environ, "PATH": folder},
                                        capture_output=True, text=True)
                if missing is None:
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0,
                                        "Authoring CI would discover and skip video tests without " + missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
