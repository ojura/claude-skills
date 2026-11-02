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


if __name__ == "__main__":
    unittest.main()
