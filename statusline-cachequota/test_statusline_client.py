"""Exercise the compiled client's socket and daemonless transport."""
import json
import fcntl
import os
from pathlib import Path
import socket
import select
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

    def run_client(self, native, view=None, fallback=False, task_metadata=False):
        with tempfile.TemporaryDirectory(prefix="sl-") as directory:
            env = dict(os.environ, CLAUDE_CONFIG_DIR=directory)
            env.pop("CLAUDE_STATUSLINE_VIEW", None)
            if view is not None:
                env["CLAUDE_STATUSLINE_VIEW"] = view
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

    def test_ordinary_native_input_is_unchanged(self):
        data = {"session_id": "main", "model": {"id": "model"}}
        self.assertEqual(self.run_client(data), data)

    def test_view_is_a_string_and_special_characters_round_trip(self):
        data = {"session_id": "main", "note": "Ž"}
        view = json.dumps({"version": 1, "name": 'Ž "quoted" \\ slash\nnext'},
                          ensure_ascii=False) + "\n"
        result = self.run_client(data, view)
        self.assertEqual(result, {"statusline_input": data, "statusline_view": view})

    def test_malformed_view_does_not_corrupt_native_json(self):
        data = {"session_id": "main"}
        self.assertEqual(self.run_client(data, "{invalid")["statusline_input"], data)

    def test_oversized_view_is_ignored(self):
        data = {"session_id": "main"}
        self.assertEqual(self.run_client(data, "x" * 8193), data)

    def test_large_input_survives_socket_and_fallback(self):
        data = {"session_id": "main", "padding": "x" * 100_000}
        view = '{"version":1,"session_id":"main","agent_id":"agent-a"}'
        expected = {"statusline_input": data, "statusline_view": view}
        self.assertEqual(self.run_client(data, view), expected)
        self.assertEqual(self.run_client(data, view, fallback=True), expected)

    def test_task_metadata_is_explicit_and_needs_no_selected_view(self):
        data = {"session_id": "main", "tasks": [{"id": "agent-a", "contextWindowSize": 200_000}]}
        expected = {"statusline_input": data, "statusline_view": "", "statusline_task_metadata": True}
        self.assertEqual(self.run_client(data, task_metadata=True), expected)
        self.assertEqual(self.run_client(data, task_metadata=True, fallback=True), expected)

    def test_owner_lock_tracks_process_lifetime_including_forced_exit(self):
        for force in (False, True):
            with self.subTest(force=force), tempfile.TemporaryDirectory(prefix="sl-owner-") as directory:
                env = dict(os.environ, CLAUDE_CONFIG_DIR=directory)
                child = subprocess.Popen([str(self.binary), "--view-owner"], env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertTrue(select.select([child.stdout], [], [], 3)[0])
                    owner = json.loads(child.stdout.readline())["owner"]
                    self.assertRegex(owner, r"^statusline-view-[A-Za-z0-9]{6}$")
                    path = Path(directory, owner)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    with path.open("r+") as handle:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        (child.kill if force else child.terminate)()
                        child.wait(timeout=3)
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if not force:
                        self.assertFalse(path.exists())
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
