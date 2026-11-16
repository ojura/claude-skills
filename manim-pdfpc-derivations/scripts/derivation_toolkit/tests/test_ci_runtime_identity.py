"""Keep the managed image and runtime on the same real nonroot account."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .test_ci_consumer_environment import workflow_script, workflow_step


class RuntimeIdentity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime account workflow ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "docker.json"
        docker = self.bin / "docker"
        docker.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['DOCKER_CALLS']).write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(int(os.environ.get('DOCKER_EXIT', '0')))\n"
        )
        docker.chmod(0o755)
        identity = self.bin / "id"
        identity.write_text('#!/bin/sh\ncase "$1" in -u) printf "%s\\n" "$TEST_RUNNER_UID";; '
                            '-g) printf "%s\\n" "$TEST_RUNNER_GID";; *) exit 2;; esac\n')
        identity.chmod(0o755)
        self.output = self.root / "runner-output"
        self.runner = self.root / "runner temporary"
        (self.runner / "derivation-runtime").mkdir(parents=True)
        (self.runner / "derivation-runtime" / "example-data-only.appimage").write_bytes(b"fixture")
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            DOCKER_CALLS=str(self.calls), GITHUB_OUTPUT=str(self.output),
            RUNNER_TEMP=str(self.runner), ARTIFACT_NAME="example", PACKAGE_SUFFIX="-data-only",
            TEST_RUNNER_UID="1001", TEST_RUNNER_GID="1002", RUNTIME_UID="1001", RUNTIME_GID="1002")

    def run_step(self, name, **values):
        script = workflow_script(name).replace("${{ matrix.ubuntu }}", "20.04")
        return subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                              env=self.environment | values, cwd=self.root,
                              capture_output=True, text=True)

    def test_runner_identity_is_captured_once_for_image_and_launch(self):
        for uid, gid in (("1000", "1000"), ("1001", "1002")):
            with self.subTest(uid=uid, gid=gid):
                self.output.unlink(missing_ok=True)
                result = self.run_step("Match the runtime account to the runner",
                                       TEST_RUNNER_UID=uid, TEST_RUNNER_GID=gid)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.output.read_text(), f"uid={uid}\ngid={gid}\n")
        identity = workflow_step("Match the runtime account to the runner")
        self.assertIn("id: runtime_account", identity)
        image = workflow_step("Build the minimal runtime image")
        self.assertIn("TEST_UID=${{ steps.runtime_account.outputs.uid }}", image)
        self.assertIn("TEST_GID=${{ steps.runtime_account.outputs.gid }}", image)
        launch = workflow_step("Exercise the exact relocated package with networking disabled")
        self.assertIn("RUNTIME_UID: ${{ steps.runtime_account.outputs.uid }}", launch)
        self.assertIn("RUNTIME_GID: ${{ steps.runtime_account.outputs.gid }}", launch)

    def test_invalid_or_root_identity_is_rejected_before_publication(self):
        for uid, gid in (("0", "1000"), ("1000", "0"), ("invalid", "1000")):
            with self.subTest(uid=uid, gid=gid):
                self.output.unlink(missing_ok=True)
                result = self.run_step("Match the runtime account to the runner",
                                       TEST_RUNNER_UID=uid, TEST_RUNNER_GID=gid)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.calls.exists())

    def test_actual_runtime_step_uses_captured_identity_and_readonly_inputs(self):
        result = self.run_step("Exercise the exact relocated package with networking disabled",
                               TEST_RUNNER_UID="7777", TEST_RUNNER_GID="8888")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        call = json.loads(self.calls.read_text())
        self.assertEqual(call[call.index("--user") + 1], "1001:1002")
        self.assertEqual(call[call.index("--network") + 1], "none")
        mounts = [call[index + 1] for index, value in enumerate(call) if value == "--mount"]
        self.assertEqual(mounts, [
            f"type=bind,src={self.runner}/derivation-runtime/example-data-only.appimage,dst=/bundle/presentation.appimage,readonly",
            f"type=bind,src={self.runner}/derivation-fixtures/runtime-fixtures.json,dst=/input/fixtures.json,readonly",
            f"type=bind,src={self.runner}/derivation-runtime-checks,dst=/evidence",
        ])
        self.assertTrue((self.runner / "derivation-runtime-checks").is_dir())
        self.assertTrue(os.access(self.runner / "derivation-runtime/example-data-only.appimage", os.X_OK))

    def test_runtime_failure_is_not_hidden(self):
        result = self.run_step("Exercise the exact relocated package with networking disabled", DOCKER_EXIT="23")
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
