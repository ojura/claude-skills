"""Exercise the managed workflow's consumer image and source-preparation steps."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / "ci/github-actions.yml"


def workflow_step(name):
    text = WORKFLOW.read_text()
    marker = "      - name: " + name + "\n"
    if marker not in text:
        raise AssertionError("The managed workflow has no " + name + " step")
    return text.split(marker, 1)[1].split("      - ", 1)[0]


def workflow_script(name):
    return textwrap.dedent(workflow_step(name).split("        run: |\n", 1)[1])


class ConsumerEnvironment(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="consumer workflow ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "source" / "project with spaces"
        self.project.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "docker-calls.json"
        docker = self.bin / "docker"
        docker.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['DOCKER_CALLS']).write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(int(os.environ.get('DOCKER_EXIT', '0')))\n"
        )
        docker.chmod(0o755)
        self.environment = dict(
            os.environ, GITHUB_WORKSPACE=str(self.project.parent), PROJECT=self.project.name,
            RUNNER_TEMP=str(self.root / "runner temporary"), DOCKER_CALLS=str(self.calls),
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            AUTHORING_DOCKERFILE="report.Dockerfile", AUTHORING_CONTEXT="image context",
        )
        (self.project / "report.Dockerfile").write_text(
            "ARG AUTHORING_IMAGE\nFROM ${AUTHORING_IMAGE}\n"
        )
        (self.project / "image context").mkdir()

    def run_step(self, name, **environment):
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", workflow_script(name)],
            cwd=self.root, env=self.environment | environment,
            capture_output=True, text=True,
        )

    def test_default_keeps_the_generic_image_and_consumer_override_is_optional(self):
        text = WORKFLOW.read_text()
        for name in ("authoring_dockerfile", "authoring_context", "prepare_command"):
            self.assertIn("      " + name + ":\n", text)
        step = workflow_step("Build the consumer authoring image")
        self.assertIn("if: inputs.authoring_dockerfile != ''", step)
        self.assertIn("BUILDX_BUILDER: default", step)
        base = workflow_step("Restore or build the locked authoring image")
        self.assertIn("derivation-assets:base", base)
        self.assertIn("derivation-assets:ci", base)
        execution = workflow_script("Test the toolkit, check source exports and build the project")
        self.assertIn("derivation-assets:ci bash -euc", execution)
        self.assertIn("--network none", execution)
        self.assertIn('dst=/src,readonly"', execution)
        self.assertIn('--user "$(id -u):$(id -g)"', execution)

    def test_actual_image_step_uses_loaded_base_and_quoted_consumer_paths(self):
        result = self.run_step("Build the consumer authoring image")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.calls.read_text()), [
            "build", "--platform", "linux/amd64", "--build-arg",
            "AUTHORING_IMAGE=derivation-assets:base", "--file",
            str(self.project / "report.Dockerfile"), "--tag", "derivation-assets:ci",
            str(self.project / "image context"),
        ])

    def test_missing_recipe_or_context_stops_before_docker(self):
        for variable in ("AUTHORING_DOCKERFILE", "AUTHORING_CONTEXT"):
            with self.subTest(variable=variable):
                result = self.run_step("Build the consumer authoring image", **{variable: "absent"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_failed_image_build_is_not_ignored(self):
        result = self.run_step("Build the consumer authoring image", DOCKER_EXIT="23")
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)

    def test_prepare_command_stages_sources_in_cache_from_the_consumer_directory(self):
        (self.project / "report.md").write_text("The committed report source.\n")
        command = 'cp report.md "$RUNNER_TEMP/derivation-cache/report.md"; printf prepared'
        result = self.run_step("Prepare external source inputs", PREPARE_COMMAND=command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.root / "runner temporary" / "derivation-cache" / "report.md"
        self.assertEqual(output.read_bytes(), (self.project / "report.md").read_bytes())
        self.assertEqual(result.stdout, "prepared")
        self.assertFalse((self.project / "build").exists())

    def test_prepare_failure_stops_the_step(self):
        result = self.run_step("Prepare external source inputs", PREPARE_COMMAND="exit 29")
        self.assertEqual(result.returncode, 29, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
