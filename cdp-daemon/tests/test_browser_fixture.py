"""Check the owned browser's transport and the passive helper manifest."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_page_preparation as fixture
import browser_consent as native_fixture


class PassiveHelperManifestTests(unittest.TestCase):
    def test_passive_helper_declares_only_debugger_and_keeps_its_identity(self):
        manifest = json.loads(
            (fixture.ROOT / "browser-state-extension/manifest.json").read_text())
        self.assertEqual(manifest["permissions"], ["debugger"])
        for field in ("background", "content_scripts", "host_permissions",
                      "optional_permissions", "optional_host_permissions"):
            self.assertNotIn(field, manifest)
        self.assertEqual(fixture.CDP.page_preparation.extension_origin,
                         "chrome-extension://mjkdoldgccjabmdemjlgnmkbomppkofg")


class OwnedBrowserConnectionTests(unittest.TestCase):
    def test_acquisition_timeout_is_cleared_before_starting_the_reader(self):
        client, peer = socket.socketpair()
        client.settimeout(10)
        observed = []
        reader = mock.Mock()
        reader.start.side_effect = lambda: observed.append(client.gettimeout())
        try:
            with tempfile.TemporaryDirectory(prefix="cdp-fixture-socket-") as directory:
                profile = Path(directory)
                (profile / "DevToolsActivePort").write_text("43809\n/devtools/browser/owned-test\n")
                browser = fixture.OwnedBrowser.__new__(fixture.OwnedBrowser)
                browser.profile = profile
                with mock.patch.multiple(fixture.CDP, PORT=0, PATH="", cdp_sock=None, cdp_leftover=b""):
                    with mock.patch.object(fixture.CDP, "ws_upgrade", return_value=("ok", client, b"buffered")) as upgrade:
                        with mock.patch.object(fixture.threading, "Thread", return_value=reader) as thread:
                            browser.connect()
                        upgrade.assert_called_once_with(10)
                        thread.assert_called_once_with(target=fixture.CDP.reader_thread, daemon=True)
                        reader.start.assert_called_once_with()
                        self.assertEqual(observed, [None])
                        self.assertIsNone(client.gettimeout())
                        self.assertIs(fixture.CDP.cdp_sock, client)
                        self.assertEqual(fixture.CDP.cdp_leftover, b"buffered")
        finally:
            client.close()
            peer.close()


class OwnedWindowManagerReadinessTests(unittest.TestCase):
    root = '_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x4000ae\n'
    identity = root + '_NET_WM_NAME(UTF8_STRING) = "Xfwm4"\n'

    def setUp(self):
        power = mock.patch.object(native_fixture, "require_ac")
        power.start()
        self.addCleanup(power.stop)

    def result(self, stdout, returncode=0):
        return native_fixture.subprocess.CompletedProcess([], returncode, stdout=stdout)

    def check(self, results, expected_observations):
        process = mock.Mock()
        process.poll.return_value = None

        def wait(function, description, seconds):
            self.assertEqual(seconds, 10)
            for expected in expected_observations:
                self.assertEqual(function(), expected)
            if not expected_observations[-1]:
                raise TimeoutError(description)

        with tempfile.TemporaryDirectory(prefix="cdp-wm-readiness-") as directory:
            evidence = Path(directory) / "readiness.json"
            with mock.patch.object(native_fixture.subprocess, "run", side_effect=results) as run:
                with mock.patch.object(native_fixture, "wait_for", side_effect=wait):
                    if expected_observations[-1]:
                        native_fixture.wait_for_window_manager(process, evidence)
                    else:
                        with self.assertRaises(TimeoutError):
                            native_fixture.wait_for_window_manager(process, evidence)
            return json.loads(evidence.read_text()), run.call_args_list

    def test_missing_root_property_waits_until_xfwm4_identifies_itself(self):
        evidence, calls = self.check([
            self.result('_NET_SUPPORTING_WM_CHECK:  not found.\n'),
            self.result(self.root), self.result(self.identity),
        ], [False, True])
        self.assertTrue(evidence["ready"])
        self.assertFalse(evidence["observations"][0]["root_published"])
        self.assertTrue(evidence["observations"][1]["self_reference"])
        self.assertTrue(evidence["observations"][1]["xfwm4"])
        self.assertEqual(calls[-1].args[0], ["xprop", "-id", "0x4000ae",
                                            "_NET_SUPPORTING_WM_CHECK", "_NET_WM_NAME"])

    def test_another_window_manager_does_not_satisfy_readiness(self):
        evidence, calls = self.check([self.result(self.root),
            self.result(self.identity.replace('"Xfwm4"', '"Other Xfwm4 instance"'))], [False])
        self.assertFalse(evidence["ready"])
        self.assertFalse(evidence["observations"][0]["xfwm4"])

    def test_stale_root_reference_does_not_satisfy_readiness(self):
        evidence, calls = self.check([self.result(self.root),
            self.result(self.identity.replace("0x4000ae", "0x4000af"))], [False])
        self.assertFalse(evidence["observations"][0]["self_reference"])

    def test_failed_property_query_does_not_satisfy_readiness(self):
        evidence, calls = self.check([self.result(self.root, returncode=1)], [False])
        self.assertFalse(evidence["ready"])
        self.assertEqual(len(calls), 1)

    def test_property_query_timeout_is_recorded_and_retried(self):
        evidence, calls = self.check([
            native_fixture.subprocess.TimeoutExpired("xprop", 2),
            self.result(self.root), self.result(self.identity),
        ], [False, True])
        self.assertTrue(evidence["observations"][0]["query_timeout"])
        self.assertTrue(evidence["ready"])
        self.assertTrue(all(call.kwargs["timeout"] == 2 for call in calls))

    def test_exited_window_manager_fails_without_querying_the_display(self):
        process = mock.Mock()
        process.poll.return_value = 1
        with tempfile.TemporaryDirectory(prefix="cdp-wm-readiness-") as directory:
            evidence = Path(directory) / "readiness.json"
            with mock.patch.object(native_fixture.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "exited before readiness"):
                    native_fixture.wait_for_window_manager(process, evidence)
            run.assert_not_called()
            self.assertFalse(json.loads(evidence.read_text())["ready"])


if __name__ == "__main__":
    unittest.main()
