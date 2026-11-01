"""Exercise API listener ownership without acquiring a Chrome connection."""
import concurrent.futures
import errno
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_page_preparation import load_daemon


class DaemonStartupTests(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.servers = []

    def server(self, address, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.servers.append(server)
        return server

    def assert_released(self):
        self.assertEqual(len(self.servers), 1)
        server = self.servers[0]
        self.assertEqual(server.socket.fileno(), -1)
        with socket.socket() as replacement:
            replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            replacement.bind(server.server_address)
            self.assertEqual(replacement.getsockname(), server.server_address)

    def test_occupied_api_port_prevents_logging_threads_and_acquisition(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            with mock.patch.object(self.daemon, "DAEMON_PORT", port):
                with mock.patch.object(self.daemon, "log") as log:
                    with mock.patch.object(self.daemon.threading, "Thread") as thread:
                        with mock.patch.object(self.daemon, "presser_main") as presser:
                            with mock.patch.object(self.daemon, "connection_manager") as acquire:
                                with self.assertRaises(OSError) as caught:
                                    self.daemon.main()
            self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
            log.assert_not_called()
            thread.assert_not_called()
            presser.assert_not_called()
            acquire.assert_not_called()
            self.assertEqual(occupied.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)

    def test_status_is_served_while_fake_acquisition_is_pending(self):
        entered, finish = threading.Event(), threading.Event()
        result = concurrent.futures.Future()
        events = []

        def bind(address, handler):
            server = self.server(address, handler)
            events.append("bound")
            return server

        def log(message):
            if message.startswith("HTTP API on "):
                events.append("available")

        def acquire():
            events.append("acquisition")
            self.daemon.set_state("connecting", "Owned fake acquisition")
            entered.set()
            finish.wait()

        def run():
            try:
                result.set_result(self.daemon.main())
            except BaseException as error:
                result.set_exception(error)

        main_thread = threading.Thread(target=run, daemon=True)
        try:
            with mock.patch.object(self.daemon, "ThreadingHTTPServer", side_effect=bind):
                with mock.patch.object(self.daemon, "log", side_effect=log):
                    with mock.patch.object(self.daemon, "presser_main"):
                        with mock.patch.object(self.daemon, "connection_manager", side_effect=acquire):
                            main_thread.start()
                            self.assertTrue(entered.wait(2))
                            host, port = self.servers[0].server_address
                            with urllib.request.urlopen(f"http://{host}:{port}/status", timeout=2) as response:
                                status = json.load(response)
                            self.assertEqual(status["state"], "connecting")
                            self.assertFalse(status["connected"])
                            self.assertEqual(events[:3], ["bound", "available", "acquisition"])
                            self.assertFalse(result.done())
                            finish.set()
                            result.result(2)
            self.assert_released()
        finally:
            finish.set()
            main_thread.join(2)
            self.assertFalse(main_thread.is_alive())

    def test_http_thread_start_failure_releases_the_bound_listener(self):
        with mock.patch.object(self.daemon, "ThreadingHTTPServer", side_effect=self.server):
            with mock.patch.object(self.daemon.threading.Thread, "start", side_effect=RuntimeError("owned start failure")):
                with mock.patch.object(self.daemon, "log") as log:
                    with mock.patch.object(self.daemon, "presser_main") as presser:
                        with mock.patch.object(self.daemon, "connection_manager") as acquire:
                            with self.assertRaises(RuntimeError):
                                self.daemon.main()
        self.assert_released()
        log.assert_not_called()
        presser.assert_not_called()
        acquire.assert_not_called()

    def test_failure_after_http_start_shuts_down_and_releases_the_listener(self):
        with mock.patch.object(self.daemon, "ThreadingHTTPServer", side_effect=self.server):
            with mock.patch.object(self.daemon, "log"):
                with mock.patch.object(self.daemon, "presser_main"):
                    with mock.patch.object(self.daemon, "connection_manager", side_effect=RuntimeError("owned acquisition failure")):
                        with self.assertRaises(RuntimeError):
                            self.daemon.main()
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
