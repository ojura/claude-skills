"""Exercise standalone waiting with owned accessibility trees and a fake clock."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_consent_diagnostics import Node, load_modals


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    monotonic = time

    def sleep(self, seconds):
        self.now += seconds


class ClearModalsCliTests(unittest.TestCase):
    def setUp(self):
        self.modals = load_modals()
        self.clock = Clock()
        self.output = io.StringIO()
        self.chrome_initially_visible = True
        self.renderer_visible = False
        self.prompt_at = None
        self.button = Node('Allow', 'push button')
        self.modals.Atspi.get_desktop.side_effect = self.desktop
        self.health_results = []
        original_health = self.modals.chrome_accessibility_health

        def health():
            result = original_health()
            self.health_results.append((self.clock.now, result))
            return result

        for patch in (
                mock.patch.object(self.modals, 'time', self.clock),
                mock.patch.object(self.modals.sys, 'argv', ['clear_modals.py', '--wait']),
                mock.patch.object(self.modals, 'chrome_accessibility_health', side_effect=health)):
            patch.start()
            self.addCleanup(patch.stop)

    def desktop(self, index):
        self.assertEqual(index, 0)
        prompt_available = self.prompt_at is not None and self.clock.now >= self.prompt_at
        if not self.chrome_initially_visible and not prompt_available:
            return Node()
        children = []
        if self.renderer_visible:
            children.append(Node('Owned page', 'document web'))
        if prompt_available and not self.button.calls:
            children.append(Node('Allow remote debugging?', 'dialog', children=[self.button]))
        return Node(children=[Node('Google Chrome', children=children)])

    def assert_delayed_action(self, initial_health):
        self.prompt_at = 6.0
        with redirect_stdout(self.output):
            self.modals.main()
        self.assertEqual(self.button.calls, 1)
        self.assertEqual(len(self.health_results), 1)
        observed_at, observed_health = self.health_results[0]
        self.assertGreater(observed_at, 2.0)
        self.assertLess(observed_at, self.prompt_at)
        self.assertEqual(observed_health, initial_health)
        self.assertGreaterEqual(self.clock.now, self.prompt_at + 0.5)
        self.assertLess(self.clock.now, 60.0)
        self.assertIn('continuing to wait', self.output.getvalue())
        self.assertIn('completed 1 native action call(s)', self.output.getvalue())

    def test_wait_accepts_later_native_action_when_chrome_initially_missing(self):
        self.chrome_initially_visible = False
        self.assert_delayed_action((False, False))

    def test_wait_accepts_later_native_action_without_renderer_roles(self):
        self.assert_delayed_action((True, False))

    def assert_deadline(self, expected_health):
        with redirect_stdout(self.output):
            with self.assertRaises(SystemExit) as caught:
                self.modals.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertGreaterEqual(self.clock.now, 60.0)
        self.assertLess(self.clock.now, 60.3)
        self.assertEqual(self.button.calls, 0)
        self.assertEqual(len(self.health_results), 1)
        self.assertEqual(self.health_results[0][1], expected_health)
        self.assertIn('timeout (no Allow/Restore button found', self.output.getvalue())

    def test_wait_keeps_deadline_when_chrome_stays_missing(self):
        self.chrome_initially_visible = False
        self.assert_deadline((False, False))

    def test_wait_keeps_deadline_without_renderer_roles(self):
        self.assert_deadline((True, False))

    def test_wait_keeps_deadline_with_healthy_accessibility_and_no_prompt(self):
        self.renderer_visible = True
        self.assert_deadline((True, True))


if __name__ == '__main__':
    unittest.main()
