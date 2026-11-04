#!/usr/bin/env -S python3 -I -B
"""Explicit real controls using only owned, network-isolated containers."""
import datetime
import json
import os
from pathlib import Path
import secrets
import select
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "docker_ac_guard.py"
POWER_SUPPORT = runpy.run_path(str(Path(__file__).with_name("power_support.py")))
PHYSICAL_AC = POWER_SUPPORT["PHYSICAL_AC"]
PowerMonitor = POWER_SUPPORT["PowerMonitor"]


@unittest.skipUnless(os.environ.get("DOCKER_AC_REAL_TESTS") == "1", "explicit owned real-container controls")
class RealDockerTests(unittest.TestCase):
    def setUp(self):
        if PHYSICAL_AC.read_text().strip() != "1":
            self.skipTest("Physical AC is offline; real controls were not started")
        output_root = os.environ.get("DOCKER_AC_TEST_OUTPUT")
        self.temporary = None
        if output_root:
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + ".", dir=output_root))
        else:
            self.temporary = tempfile.TemporaryDirectory(prefix="docker-ac-real-")
            self.root = Path(self.temporary.name)
        self.logs = self.root / "logs"
        self.logs.mkdir(mode=0o700)
        self.power = self.root / "injected-online"
        self.power.write_text("1\n")
        self.ready = self.root / "ready"
        os.mkfifo(self.ready, 0o600)
        self.ready_fd = os.open(self.ready, os.O_RDONLY | os.O_NONBLOCK)
        self.name = "docker-ac-real-" + str(os.getpid()) + "-" + secrets.token_hex(6)
        self.child = None
        self.physical = PowerMonitor(self.root, lambda: [self.child] if self.child is not None else [])
        self.physical.start()

    def tearDown(self):
        # Keep the exact guard owned until it confirms cleanup. A Docker daemon
        # that cannot answer can make this wait unbounded; killing the guard
        # would discard that ownership and is not fixture cleanup.
        if self.child is not None and self.child.poll() is None:
            self.child.send_signal(signal.SIGTERM)
            self.child.wait()
        self.physical.finish()
        os.close(self.ready_fd)
        if self.temporary is not None:
            self.temporary.cleanup()

    def launch(self, command):
        self.assertEqual(PHYSICAL_AC.read_text().strip(), "1")
        self.assertFalse(self.physical.lost.is_set())
        environment = os.environ.copy()
        environment.update(DOCKER_AC_LOG_DIR=str(self.logs), DOCKER_AC_PATH=str(self.power))
        environment.pop("FAKE_DOCKER_SOCKET", None)
        image = os.environ.get("DOCKER_AC_TEST_IMAGE", "golang:1.27")
        self.child = subprocess.Popen(
            [sys.executable, "-I", "-B", str(SCRIPT), "run", "--rm", "--pull", "never",
             "--name", self.name, "--network", "none", "--cpus", "1", "--memory", "256m",
             "--user", str(os.getuid()) + ":" + str(os.getgid()),
             "-v", str(self.root) + ":/control", image, "/bin/sh", "-c", command],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        self.physical.check_after_assignment(self.child)
        return self.child

    def wait_ready(self):
        readable, _, _ = select.select([self.ready_fd], [], [], 30)
        self.assertTrue(readable, "Owned container did not report readiness")
        self.assertEqual(os.read(self.ready_fd, 32), b"ready\n")

    def complete(self, expected):
        output, error = self.child.communicate(timeout=30)
        self.assertEqual(self.child.returncode, expected, (output, error))
        directory, = self.logs.iterdir()
        result = json.loads((directory / "result.json").read_text())
        self.assertTrue(result["cleanup_confirmed"], result)
        self.assertEqual(result["exit_status"], expected)
        identifier = result["container_id"]
        if identifier is not None:
            docker = os.environ.get("DOCKER_AC_REAL_DOCKER", "/usr/bin/docker")
            check = subprocess.run([docker, "ps", "--all", "--quiet", "--no-trunc", "--filter", "id=" + identifier], capture_output=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(check.stdout, b"")
        (self.root / "control-stdout.log").write_bytes(output)
        (self.root / "control-stderr.log").write_bytes(error)
        return output, result

    def test_success_output(self):
        self.launch("printf 'owned real output\\n'")
        output, result = self.complete(0)
        self.assertEqual(output, b"owned real output\n")
        self.assertEqual(result["container_exit_status"], 0)

    def test_exit_seven(self):
        self.launch("printf 'owned nonzero output\\n'; exit 7")
        output, result = self.complete(7)
        self.assertEqual(output, b"owned nonzero output\n")
        self.assertEqual(result["container_exit_status"], 7)

    def test_workload_signal_status(self):
        self.launch("kill -TERM $$")
        _, result = self.complete(143)
        self.assertEqual(result["container_exit_status"], 143)

    def test_term_mid_run(self):
        self.launch("printf 'ready\\n' > /control/ready; exec sleep infinity")
        self.wait_ready()
        self.child.send_signal(signal.SIGTERM)
        self.complete(143)

    def test_hup_mid_run(self):
        self.launch("printf 'ready\\n' > /control/ready; exec sleep infinity")
        self.wait_ready()
        self.child.send_signal(signal.SIGHUP)
        self.complete(129)

    def test_injected_power_loss_mid_run(self):
        self.launch("printf 'ready\\n' > /control/ready; exec sleep infinity")
        self.wait_ready()
        self.power.write_text("0\n")
        _, result = self.complete(3)
        self.assertTrue(result["offline_observed"])
        self.assertFalse(result["valid_power_interval"])


if __name__ == "__main__":
    unittest.main()
