"""Exercise the compiled client's socket and daemonless transport."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest


class ClientTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="statusline-client-")
        cls.binary = Path(cls.build.name) / "client"
        source = Path(__file__).with_name("statusline-client.c")
        subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-o", str(cls.binary), str(source)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def run_client(self, native, view=None, fallback=False, task_metadata=False,
                   active=None, pid=None, pane=None):
        with tempfile.TemporaryDirectory(prefix="sl-") as directory:
            env = dict(os.environ, CLAUDE_CONFIG_DIR=directory)
            for key in ("CLAUDE_STATUSLINE_VIEW", "CLAUDE_STATUSLINE_ACTIVE", "CLAUDE_PID", "TMUX_PANE"):
                env.pop(key, None)
            if view is not None:
                env["CLAUDE_STATUSLINE_VIEW"] = view
            for key, value in (("CLAUDE_STATUSLINE_ACTIVE", active), ("CLAUDE_PID", pid), ("TMUX_PANE", pane)):
                if value is not None:
                    env[key] = str(value)
            raw = json.dumps(native).encode()
            command = [str(self.binary)] + (["--task-metadata"] if task_metadata else [])
            if fallback:
                Path(directory, "statusline-render.py").write_text(
                    "import sys\n"
                    "if '--daemon' not in sys.argv: sys.stdout.write(sys.stdin.read())\n")
                output = subprocess.run(command, input=raw, env=env,
                                        capture_output=True, timeout=5, check=True)
                return json.loads(output.stdout)
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(Path(directory, "statusline.sock")))
            server.listen(1)
            server.settimeout(5)
            errors = []
            def echo():
                try:
                    with server.accept()[0] as conn:
                        conn.settimeout(5)
                        chunks = []
                        while True:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        conn.sendall(b"".join(chunks))
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=echo)
            thread.start()
            try:
                output = subprocess.run(command, input=raw, env=env,
                                        capture_output=True, timeout=5, check=True)
            finally:
                thread.join(5)
                server.close()
            if errors:
                raise errors[0]
            self.assertFalse(thread.is_alive())
            return json.loads(output.stdout)

    def expected(self, native, view="", active="", process=None, **extra):
        return {"statusline_input": native, "statusline_view": view,
                "statusline_activation": active, "statusline_process": process or {}, **extra}

    def test_unselected_native_frame_is_preserved_for_registration(self):
        data = {"session_id": "main", "model": {"id": "model"}}
        self.assertEqual(self.run_client(data), self.expected(data))

    def test_view_is_a_string_and_special_characters_round_trip(self):
        data = {"session_id": "main", "note": "Ž"}
        view = json.dumps({"version": 2, "name": 'Ž "quoted" \\ slash\nnext'},
                          ensure_ascii=False) + "\n"
        result = self.run_client(data, view)
        self.assertEqual(result, self.expected(data, view))

    def test_malformed_view_does_not_corrupt_native_json(self):
        data = {"session_id": "main"}
        self.assertEqual(self.run_client(data, "{invalid")["statusline_input"], data)

    def test_oversized_view_is_ignored(self):
        data = {"session_id": "main"}
        self.assertEqual(self.run_client(data, "x" * 8193), self.expected(data))

    def test_large_input_survives_socket_and_fallback(self):
        data = {"session_id": "main", "padding": "x" * 100_000}
        view = '{"version":2,"session_id":"main","agent_id":"agent-a"}'
        expected = self.expected(data, view)
        self.assertEqual(self.run_client(data, view), expected)
        self.assertEqual(self.run_client(data, view, fallback=True), expected)

    def test_task_metadata_is_explicit_and_needs_no_selected_view(self):
        data = {"session_id": "main", "tasks": [{"id": "agent-a", "contextWindowSize": 200_000}]}
        expected = self.expected(data, statusline_task_metadata=True)
        self.assertEqual(self.run_client(data, task_metadata=True), expected)
        self.assertEqual(self.run_client(data, task_metadata=True, fallback=True), expected)

    def test_activation_and_native_process_metadata_round_trip(self):
        active = "v2_" + "a" * 32
        expected = self.expected({}, active=active, process={"pid": 1234, "pane": "%9"})
        self.assertEqual(self.run_client({}, active=active, pid=1234, pane="%9"), expected)
        self.assertEqual(self.run_client({}, active=active, pid=1234, pane="%9", fallback=True), expected)

    def test_invalid_process_ids_and_oversized_values_are_omitted(self):
        for pid in ("-1", "0", "+2", " 2", "2x", "2147483648", "9" * 20):
            with self.subTest(pid=pid):
                self.assertEqual(self.run_client({}, pid=pid)["statusline_process"], {})
        self.assertEqual(self.run_client({}, active="x" * 257, pane="x" * 257), self.expected({}))

    def test_old_owner_mode_is_removed(self):
        result = subprocess.run([str(self.binary), "--view-owner"], input=b"{}",
                                capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
