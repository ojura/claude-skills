"""The actual package desktop entry must reference its staged native executable."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


class PackageDesktop(unittest.TestCase):
    def test_configured_identity_keeps_an_existing_desktop_executable(self):
        native = Path(__file__).resolve().parents[1] / "native"
        script = (native / "build-appimage.sh").read_text()
        # Run the production staging commands, before its external validator
        # and linuxdeploy. Full packaging exercises those tools in Focal.
        begin = script.index("desktop_file=")
        end = script.index("\ndesktop-file-validate", begin)
        staging = script[begin:end]
        with tempfile.TemporaryDirectory(prefix="package desktop with spaces ") as temporary:
            root = Path(temporary)
            appdir = root / "AppDir"
            executable = appdir / "usr/bin/pdfpc"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            for identity in ("pdfpc", "derivation-package-example", "derivation-presentations"):
                with self.subTest(identity=identity):
                    environment = dict(os.environ, packaging_dir=str(native),
                                       work_dir=str(root), identity=identity)
                    result = subprocess.run(["bash", "-euc", staging], env=environment,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    entry = configparser.ConfigParser(interpolation=None)
                    entry.read(root / (identity + ".desktop"))
                    desktop = entry["Desktop Entry"]
                    command = shlex.split(desktop["Exec"])
                    self.assertTrue(command)
                    target = appdir / "usr/bin" / command[0]
                    self.assertTrue(target.is_file() and os.access(target, os.X_OK),
                                    "Desktop Exec has no staged executable: " + str(target))
                    self.assertEqual(command, ["pdfpc"])
                    self.assertEqual(desktop["Icon"], identity)
                    self.assertEqual((root / (identity + ".svg")).read_bytes(),
                                     (native / "presentation.svg").read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
