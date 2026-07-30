import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
RENDERER = SKILL_DIR / "statusline-render.py"


def render(payload, env=None):
    proc = subprocess.run(
        [sys.executable, "-ES", str(RENDERER)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    return proc


class RendererTests(unittest.TestCase):
    def test_minimal_payload_renders_model_and_context_bar(self):
        proc = render({
            "model": {"display_name": "Claude"},
            "context_window": {"used_percentage": 42},
        })

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        output = proc.stdout.decode("utf-8")
        self.assertIn("Claude", output)
        self.assertIn("ctx", output)
        self.assertIn("█", output)
        self.assertIn("░", output)

    def test_output_is_utf8_when_parent_forces_ascii_stdio(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "ascii"
        proc = render({
            "model": {"display_name": "Čet emoji 🚀"},
            "context_window": {"used_percentage": 50},
        }, env)

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        output = proc.stdout.decode("utf-8")
        self.assertIn("Čet emoji 🚀", output)
        self.assertIn("█", output)
        self.assertNotIn(b"UnicodeEncodeError", proc.stderr)

    def test_unicode_config_directory_is_supported(self):
        with tempfile.TemporaryDirectory(prefix="claude status č ") as config_dir:
            env = os.environ.copy()
            env["CLAUDE_CONFIG_DIR"] = config_dir
            proc = render({
                "model": {"display_name": "Claude"},
                "context_window": {"used_percentage": 10},
            }, env)

            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            self.assertIn("Claude", proc.stdout.decode("utf-8"))


@unittest.skipUnless(os.name == "nt", "PowerShell installer is Windows-only")
class WindowsInstallerTests(unittest.TestCase):
    def test_installer_copies_renderer_and_smoke_tests_it(self):
        with tempfile.TemporaryDirectory(prefix="claude install č ") as config_dir:
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-File", str(SKILL_DIR / "install.ps1"),
                    "-ClaudeConfigDir", config_dir,
                    "-PythonExe", sys.executable,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            stderr = proc.stderr.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertTrue((Path(config_dir) / "statusline-render.py").is_file())
            output = proc.stdout.decode("utf-8", "replace")
            self.assertIn('"statusLine"', output)
            self.assertIn('"refreshInterval": 1', output)
            self.assertIn("-X utf8 -ES", output)
            self.assertIn("--config-dir", output)
            self.assertIn(config_dir.replace("\\", "/"), output)


if __name__ == "__main__":
    unittest.main()
