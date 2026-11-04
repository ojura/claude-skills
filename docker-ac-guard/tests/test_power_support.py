#!/usr/bin/env -S python3 -I -B
"""Fault controls for physical test observation, using only owned mock children."""
import errno
import json
from pathlib import Path
import os
import runpy
import signal
import tempfile
import threading
import unittest

PowerMonitor = runpy.run_path(str(Path(__file__).with_name("power_support.py")))["PowerMonitor"]


class OwnedChildRecorder:
    def __init__(self, fail_signal=False):
        self.signals = []
        self.fail_signal = fail_signal

    def poll(self):
        return None

    def send_signal(self, number):
        self.signals.append(number)
        if self.fail_signal:
            raise OSError(errno.EPERM, "owned simulated signal failure")


class PowerMonitorTests(unittest.TestCase):
    def setUp(self):
        output_root = os.environ.get("DOCKER_AC_TEST_OUTPUT")
        self.temporary = None
        if output_root:
            self.root = Path(tempfile.mkdtemp(prefix="monitor.", dir=output_root))
            (self.root / "test-name.txt").write_text(self._testMethodName + "\n")
        else:
            self.temporary = tempfile.TemporaryDirectory(prefix="docker-ac-monitor-")
            self.root = Path(self.temporary.name)
        self.monitors = []
        self.children = []

    def tearDown(self):
        records = []
        for monitor in self.monitors:
            monitor.done.set()
            if monitor.thread.ident is not None:
                monitor.thread.join(timeout=5)
            errors = [
                {"type": type(error).__name__, "errno": getattr(error, "errno", None)}
                for error in getattr(monitor, "errors", [])
            ]
            records.append({
                "test_input": "owned simulated physical input; mock children only",
                "lost": monitor.lost.is_set(), "errors": errors,
                "thread_alive": monitor.thread.is_alive(),
                "owned_child_signals": [child.signals for child in self.children],
            })
            self.assertFalse(monitor.thread.is_alive())
        (self.root / "monitor-results.json").write_text(json.dumps(records, indent=2) + "\n")
        if self.temporary is not None:
            self.temporary.cleanup()

    def monitor(self, directory=None, children=None):
        if directory is None:
            directory = self.root / "observation"
            directory.mkdir(mode=0o700)
        if children is None:
            child = OwnedChildRecorder()
            self.children.append(child)
            children = [child]
        monitor = PowerMonitor(directory, lambda: children)
        monitor.online = lambda: True
        self.monitors.append(monitor)
        return monitor

    def assert_failed_observer(self, monitor, expected_errno):
        monitor.thread.join(timeout=5)
        self.assertFalse(monitor.thread.is_alive())
        with self.assertRaises(RuntimeError):
            monitor.finish()
        self.assertTrue(monitor.failed)
        self.assertFalse(monitor.valid_interval)
        self.assertFalse(monitor.lost.is_set())
        self.assertEqual(monitor.errors[0].errno, expected_errno)
        self.assertIn(signal.SIGTERM, self.children[0].signals)

    def test_flush_failure_is_latched_cancels_owned_child_and_rejects_interval(self):
        directory = self.root / "fault"
        directory.mkdir(mode=0o700)
        # This is an owned fault-injection link, not a readable sample log.
        (directory / "physical-power.samples.jsonl").symlink_to("/dev/full")
        monitor = self.monitor(directory)
        monitor.start()
        self.assert_failed_observer(monitor, errno.ENOSPC)

    def test_open_failure_is_latched_cancels_owned_child_and_rejects_interval(self):
        monitor = self.monitor(self.root / "missing-parent")
        monitor.start()
        self.assert_failed_observer(monitor, errno.ENOENT)

    def test_failure_before_child_assignment_cancels_the_newly_recorded_child(self):
        directory = self.root / "fault"
        directory.mkdir(mode=0o700)
        (directory / "physical-power.samples.jsonl").symlink_to("/dev/full")
        monitor = self.monitor(directory, children=[])
        monitor.start()
        monitor.thread.join(timeout=5)
        self.assertFalse(monitor.thread.is_alive())
        child = OwnedChildRecorder()
        self.children.append(child)
        monitor.check_after_assignment(child)
        self.assertEqual(child.signals, [signal.SIGTERM])
        with self.assertRaises(RuntimeError):
            monitor.finish()
        self.assertTrue(monitor.failed)
        self.assertFalse(monitor.lost.is_set())

    def test_cancellation_error_does_not_skip_another_owned_child(self):
        bad = OwnedChildRecorder(fail_signal=True)
        good = OwnedChildRecorder()
        self.children.extend((bad, good))
        directory = self.root / "fault"
        directory.mkdir(mode=0o700)
        (directory / "physical-power.samples.jsonl").symlink_to("/dev/full")
        monitor = self.monitor(directory, children=[bad, good])
        monitor.start()
        self.assert_failed_observer(monitor, errno.ENOSPC)
        self.assertEqual(good.signals, [signal.SIGTERM])
        self.assertTrue(any(getattr(error, "errno", None) == errno.EPERM for error in monitor.errors))

    def test_read_failure_is_not_reported_as_an_actual_offline_value(self):
        monitor = self.monitor()

        def unreadable():
            raise OSError(errno.EIO, "owned simulated sensor failure")

        monitor.online = unreadable
        with self.assertRaises(RuntimeError):
            monitor.start()
        with self.assertRaises(RuntimeError):
            monitor.finish()
        self.assertTrue(monitor.failed)
        self.assertFalse(monitor.lost.is_set())
        self.assertEqual(self.children[0].signals, [signal.SIGTERM])

    def test_initial_offline_is_distinct_from_observer_failure(self):
        monitor = self.monitor()
        monitor.online = lambda: False
        with self.assertRaises(RuntimeError):
            monitor.start()
        with self.assertRaises(RuntimeError):
            monitor.finish()
        self.assertFalse(monitor.failed)
        self.assertTrue(monitor.lost.is_set())
        self.assertFalse(monitor.valid_interval)
        self.assertEqual(self.children[0].signals, [signal.SIGTERM])

    def test_completed_online_interval_still_succeeds(self):
        monitor = self.monitor()
        first_sample = threading.Event()
        original = monitor.record

        def record(output):
            original(output)
            first_sample.set()

        monitor.record = record
        monitor.start()
        self.assertTrue(first_sample.wait(timeout=5))
        self.assertFalse(monitor.valid_interval)
        monitor.finish()
        self.assertTrue(monitor.valid_interval)
        self.assertFalse(monitor.failed)
        self.assertFalse(monitor.lost.is_set())
        self.assertEqual(self.children[0].signals, [])
        rows = [json.loads(line) for line in (monitor.directory / "physical-power.samples.jsonl").read_text().splitlines()]
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(row["online"] for row in rows))


if __name__ == "__main__":
    unittest.main()
