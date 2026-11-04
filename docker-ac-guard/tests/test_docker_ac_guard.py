#!/usr/bin/env -S python3 -I -B
"""Isolated protocol tests. They start only owned fake Docker clients."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import runpy
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "docker_ac_guard.py"
PowerMonitor = runpy.run_path(str(Path(__file__).with_name("power_support.py")))["PowerMonitor"]
CONTAINER_ID = "a" * 64
OTHER_ID = "b" * 64
CLIENT = '''#!/usr/bin/env -S python3 -I
import json
import os
import signal
import socket
import sys

with socket.socket(socket.AF_UNIX) as connection:
    connection.connect(os.environ["FAKE_DOCKER_SOCKET"])
    request = {"args": sys.argv[1:], "pid": os.getpid()}
    connection.sendall((json.dumps(request) + "\\n").encode())
    reply = json.loads(connection.makefile("rb").readline())
sys.stdout.write(reply.get("stdout", ""))
sys.stdout.flush()
sys.stderr.write(reply.get("stderr", ""))
sys.stderr.flush()
if reply.get("signal"):
    os.kill(os.getpid(), reply["signal"])
sys.exit(reply.get("exit", 0))
'''
ENTRY = '''import importlib.util
import json
import os
import signal
import socket
import sys

spec = importlib.util.spec_from_file_location("owned_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
mode = os.environ.get("GUARD_TEST_MODE", "")
injected = False


def event(value):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(os.environ["FAKE_DOCKER_SOCKET"])
        request = {"event": value, "pid": os.getpid()}
        connection.sendall((json.dumps(request) + "\\n").encode())
        connection.makefile("rb").readline()


def inject_signal():
    os.kill(os.getpid(), int(os.environ.get("GUARD_TEST_SIGNAL", signal.SIGTERM)))


original_observe = module.Guard.observe


def observe(self, *args, **kwargs):
    global injected
    count = self.sample_count
    if not injected and mode == "before-create" and self.phase == "initial":
        injected = True
        inject_signal()
    original_observe(self, *args, **kwargs)
    if self.sample_count != count:
        event({"kind": "sample", "phase": self.phase,
               "offline": self.offline_observed, "count": self.sample_count})


module.Guard.observe = observe
original_popen = module.subprocess.Popen


def popen(args, *values, **kwargs):
    global injected
    child = original_popen(args, *values, **kwargs)
    if not injected and mode.startswith("constructor-") and args[1] == mode.split("-", 1)[1]:
        injected = True
        inject_signal()
        event({"kind": "constructor-signal", "child": child.pid})
    return child


module.subprocess.Popen = popen
sys.argv = sys.argv[1:]
raise SystemExit(module.main())
'''


class FakeDocker:
    def __init__(self, directory):
        self.socket_path = directory / "docker.sock"
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.socket_path))
        self.server.listen()
        self.events = queue.Queue()
        self.condition = threading.Condition()
        self.requests = []
        self.all_requests = []
        self.states = {}
        self.create_gate = threading.Event()
        self.create_gate.set()
        self.start_gate = threading.Event()
        self.start_gate.set()
        self.finish_gate = threading.Event()
        self.finish_gate.set()
        self.inspect_gate = threading.Event()
        self.inspect_gate.set()
        self.failed_remove = False
        self.failed_stop = False
        self.bad_owner = False
        self.replace_name = False
        self.create_reply = CONTAINER_ID + "\n"
        self.create_exit = 0
        self.program_exit = 0
        self.attach_exit = 0
        self.attach_signal = None
        self.stdout = "--- PASS: TestOwned (0.00s)\nPASS\nok owned 0.1s\n"
        self.closed = False
        self.threads = []
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.closed:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            thread = threading.Thread(target=self.handle, args=(connection,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def handle(self, connection):
        with connection:
            request = json.loads(connection.makefile("rb").readline())
            if "event" in request:
                self.events.put(request["event"])
                reply = {}
            else:
                with self.condition:
                    self.requests.append(request)
                    self.all_requests.append(request)
                    self.condition.notify_all()
                reply = self.respond(request["args"])
            try:
                connection.sendall((json.dumps(reply) + "\n").encode())
            except BrokenPipeError:
                pass

    def respond(self, arguments):
        operation = arguments[0]
        if operation == "create":
            self.create_gate.wait()
            owner = next(item.split("=", 1)[1] for item in arguments if item.startswith("local.ac-guard.owner="))
            with self.condition:
                self.states[CONTAINER_ID] = {"Id": CONTAINER_ID, "Owner": owner, "Status": "created", "Running": False, "ExitCode": 0}
            return {"stdout": self.create_reply, "stderr": "owned create diagnostic\n", "exit": self.create_exit}
        if operation == "inspect":
            self.inspect_gate.wait()
            with self.condition:
                state = self.states.get(arguments[-1])
                if state is None:
                    return {"exit": 1}
                result = dict(state)
                if self.bad_owner:
                    result["Owner"] = "unrelated-owner"
                return {"stdout": json.dumps(result)}
        if operation == "ps":
            predicate = arguments[-1]
            with self.condition:
                if predicate.startswith("id="):
                    identifiers = [item for item in self.states if item == predicate[3:]]
                else:
                    token = predicate.split("=", 2)[2]
                    identifiers = [item for item, state in self.states.items() if state["Owner"] == token]
            return {"stdout": "".join(item + "\n" for item in identifiers)}
        if operation == "start":
            self.start_gate.wait()
            with self.condition:
                state = self.states.get(arguments[-1])
                if state is None:
                    return {"exit": 1}
                state.update(Status="running", Running=True)
                self.condition.notify_all()
            self.events.put({"kind": "running"})
            self.finish_gate.wait()
            with self.condition:
                state = self.states.get(arguments[-1])
                if state is None:
                    return {"stdout": "owned workload removed\n", "exit": 1}
                if state["Running"]:
                    state.update(Status="exited", Running=False, ExitCode=self.program_exit)
            return {"stdout": self.stdout, "exit": self.attach_exit, "signal": self.attach_signal}
        if operation == "stop":
            with self.condition:
                if self.failed_stop:
                    return {"exit": 1, "stderr": "owned simulated stop API failure\n"}
                state = self.states.get(arguments[-1])
                if state is None:
                    return {"exit": 1}
                state.update(Status="exited", Running=False, ExitCode=143)
            self.finish_gate.set()
            return {"stdout": arguments[-1] + "\n"}
        if operation == "rm":
            with self.condition:
                if self.failed_remove:
                    return {"exit": 1, "stderr": "owned simulated API failure\n"}
                state = self.states.get(arguments[-1])
                if state is not None and state["Running"]:
                    return {"exit": 1, "stderr": "owned running-container removal refusal\n"}
                state = self.states.pop(arguments[-1], None)
                if self.replace_name:
                    self.states[OTHER_ID] = {"Id": OTHER_ID, "Owner": "unrelated-owner", "Status": "running", "Running": True, "ExitCode": 0}
                self.condition.notify_all()
            self.finish_gate.set()
            self.events.put({"kind": "removed"})
            return {"stdout": arguments[-1] + "\n", "exit": 0 if state else 1}
        return {"stdout": "owned passthrough\n"}

    def wait_request(self, operation, count=1):
        with self.condition:
            good = self.condition.wait_for(
                lambda: sum(item["args"][0] == operation for item in self.requests) >= count,
                timeout=15,
            )
            if not good:
                raise AssertionError("Owned fake Docker did not receive " + operation)
            return [item for item in self.requests if item["args"][0] == operation][count - 1]

    def wait_event(self, kind, predicate=lambda value: True):
        while True:
            value = self.events.get(timeout=15)
            if value["kind"] == kind and predicate(value):
                return value

    def close(self):
        self.closed = True
        self.create_gate.set()
        self.start_gate.set()
        self.finish_gate.set()
        self.inspect_gate.set()
        self.server.shutdown(socket.SHUT_RDWR)
        self.server.close()
        self.thread.join(15)
        for thread in self.threads:
            thread.join(15)
        if any(thread.is_alive() for thread in self.threads) or self.thread.is_alive():
            raise AssertionError("Owned fake API thread did not finish")


class GuardTests(unittest.TestCase):
    def setUp(self):
        artifact_root = os.environ.get("DOCKER_AC_TEST_OUTPUT")
        self.temporary = None
        if artifact_root:
            self.root = Path(tempfile.mkdtemp(prefix="case.", dir=artifact_root))
            (self.root / "test-name.txt").write_text(self._testMethodName + "\n")
        else:
            self.temporary = tempfile.TemporaryDirectory(prefix="docker-ac-guard-test-")
            self.root = Path(self.temporary.name)
        self.logs = self.root / "logs"
        self.logs.mkdir(mode=0o700)
        self.ac = self.root / "online"
        self.ac.write_text("1\n")
        self.fake = FakeDocker(self.root)
        self.docker = self.root / "docker"
        self.docker.write_text(CLIENT)
        self.docker.chmod(0o700)
        self.entry = self.root / "entry.py"
        self.entry.write_text(ENTRY)
        self.children = []
        self.invocations = {}
        self.output_files = []
        self.physical = None
        if os.environ.get("DOCKER_AC_MONITOR_TESTS") == "1":
            self.physical = PowerMonitor(self.root, lambda: list(self.children))
            self.physical.start()

    def tearDown(self):
        self.fake.create_gate.set()
        self.fake.start_gate.set()
        self.fake.finish_gate.set()
        self.fake.inspect_gate.set()
        self.fake.bad_owner = False
        self.fake.failed_remove = False
        self.fake.failed_stop = False
        for child in self.children:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                    self.fail("Owned fixture required SIGKILL after TERM and released API gates")
        self.fake.close()
        for output in self.output_files:
            output.close()
        if self.physical is not None:
            self.physical.finish()
        if self.temporary is not None:
            self.temporary.cleanup()
        else:
            (self.root / "fake-protocol.json").write_text(json.dumps({"requests": self.fake.all_requests, "remaining_containers": self.fake.states}, indent=2) + "\n")

    def launch(self, mode="", number=signal.SIGTERM, stdout=subprocess.PIPE, stderr=subprocess.PIPE, arguments=None):
        environment = os.environ.copy()
        environment.update(
            DOCKER_AC_LOG_DIR=str(self.logs), DOCKER_AC_PATH=str(self.ac),
            DOCKER_AC_REAL_DOCKER=str(self.docker), FAKE_DOCKER_SOCKET=str(self.fake.socket_path),
            GUARD_TEST_MODE=mode, GUARD_TEST_SIGNAL=str(number),
        )
        args = ["run", "--rm", "--pull", "never", "--name", "owned-fixture", "--label", "cliproxyapi.run=fixture", "--label", "cliproxyapi.part=test", "--network", "none", "--cpus", "1", "--memory", "256m", "-e", "SYNTHETIC=value with spaces", "-v", "/owned fixture:/src", "-w", "/src", "owned-image", "echo", "--rm", "argument with spaces"]
        previous_directories = set(self.logs.iterdir())
        child = subprocess.Popen(
            [sys.executable, "-I", "-B", str(self.entry), str(SCRIPT), *(args if arguments is None else arguments)],
            env=environment, stdout=stdout, stderr=stderr, start_new_session=True,
        )
        self.children.append(child)
        self.invocations[child.pid] = previous_directories
        if self.physical is not None:
            self.physical.check_after_assignment(child)
        return child

    def complete(self, child, expected):
        output, error = child.communicate(timeout=20)
        self.assertEqual(child.returncode, expected, (output, error))
        directories = list(set(self.logs.iterdir()) - self.invocations[child.pid])
        self.assertEqual(len(directories), 1)
        result = json.loads((directories[0] / "result.json").read_text())
        self.assertTrue(result["cleanup_confirmed"], result)
        self.assertFalse(result["cleanup_uncertain"], result)
        self.assertEqual(result["exit_status"], expected)
        for request in self.fake.requests:
            self.assertFalse(Path("/proc", str(request["pid"])).exists(), request)
        return output, error, result

    def test_success_preserves_arguments_output_labels_and_exit(self):
        output, error, result = self.complete(self.launch(), 0)
        self.assertEqual(output, b"owned create diagnostic\n" + self.fake.stdout.encode())
        self.assertEqual(error, b"")
        self.assertEqual(result["container_exit_status"], 0)
        self.assertEqual(result["attach_client_status"], 0)
        create = self.fake.requests[0]["args"]
        self.assertEqual(create[-4:], ["owned-image", "echo", "--rm", "argument with spaces"])
        self.assertIn("SYNTHETIC=value with spaces", create)
        self.assertIn("cliproxyapi.run=fixture", create)
        self.assertIn("cliproxyapi.part=test", create)
        self.assertNotIn("--rm", create[:create.index("owned-image")])
        self.assertEqual(self.fake.states, {})
        for request in self.fake.requests:
            if request["args"][0] == "rm":
                self.assertEqual(request["args"][-1], CONTAINER_ID)

    def test_container_nonzero_is_not_attach_client_success(self):
        self.fake.program_exit = 7
        _, _, result = self.complete(self.launch(), 7)
        self.assertEqual(result["container_exit_status"], 7)
        self.assertEqual(result["attach_client_status"], 0)

    def test_attach_client_signal_is_normalized_and_recorded(self):
        self.fake.attach_signal = signal.SIGTERM
        _, _, result = self.complete(self.launch(), 143)
        self.assertEqual(result["attach_client_status"], 143)
        self.assertEqual(result["container_exit_status"], 0)

    def test_initial_offline_starts_neither_client_nor_container(self):
        self.ac.write_text("0\n")
        _, _, result = self.complete(self.launch(), 2)
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(result["valid_power_interval"])

    def test_mid_run_power_loss_removes_owned_container(self):
        self.fake.finish_gate.clear()
        child = self.launch()
        self.fake.wait_event("running")
        self.ac.write_text("0\n")
        self.fake.wait_event("sample", lambda value: value["offline"])
        _, _, result = self.complete(child, 3)
        self.assertEqual(self.fake.states, {})
        self.assertFalse(result["valid_power_interval"])

    def test_pending_create_signals_keep_owner_until_response_then_never_start(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                self.fake.requests.clear()
                self.fake.create_gate.clear()
                child = self.launch()
                self.fake.wait_request("create")
                child.send_signal(number)
                self.fake.wait_event("sample", lambda value: value["phase"] == "create" and value["count"] > 1)
                self.assertIsNone(child.poll())
                self.assertFalse(self.fake.states)
                self.fake.create_gate.set()
                self.complete(child, 128 + number)
                self.assertFalse(any(item["args"][0] == "start" for item in self.fake.requests))
                self.assertFalse(self.fake.states)

    def test_constructor_signals_after_child_start_before_assignment(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                self.fake.requests.clear()
                self.fake.create_gate.clear()
                child = self.launch("constructor-create", number)
                self.fake.wait_event("constructor-signal")
                self.fake.wait_request("create")
                self.assertIsNone(child.poll())
                self.fake.create_gate.set()
                self.complete(child, 128 + number)
                self.assertFalse(any(item["args"][0] == "start" for item in self.fake.requests))
                self.assertFalse(self.fake.states)

    def test_signals_before_create_do_not_launch_a_client(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                self.complete(self.launch("before-create", number), 128 + number)
                self.assertEqual(self.fake.requests, [])
                self.assertFalse(self.fake.states)

    def test_signals_during_before_start_state_query_never_start(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                self.fake.requests.clear()
                self.fake.inspect_gate.clear()
                child = self.launch()
                self.fake.wait_request("inspect")
                child.send_signal(number)
                self.fake.wait_event("sample", lambda value: value["phase"] == "before-start" and value["count"] > 2)
                self.assertIsNone(child.poll())
                self.fake.inspect_gate.set()
                self.complete(child, 128 + number)
                self.assertFalse(any(item["args"][0] == "start" for item in self.fake.requests))
                self.assertFalse(self.fake.states)

    def test_term_int_hup_while_running_clean_exact_owned_id(self):
        # Each signal gets a separate invocation and private owner in its subtest.
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                self.fake.finish_gate.clear()
                child = self.launch()
                self.fake.wait_event("running")
                child.send_signal(number)
                self.complete(child, 128 + number)
                self.assertFalse(self.fake.states)
                self.fake.requests.clear()

    def test_signals_after_start_dispatch_prevent_late_execution(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                self.fake.requests.clear()
                self.fake.start_gate.clear()
                child = self.launch("constructor-start", number)
                self.fake.wait_event("constructor-signal")
                self.fake.wait_request("start")
                self.fake.wait_event("removed")
                self.assertFalse(self.fake.states)
                # Releasing the pending API proves that a delayed start cannot
                # run the removed immutable ID.
                self.fake.start_gate.set()
                self.complete(child, 128 + number)
                self.assertFalse(self.fake.states)

    def test_create_failed_after_allocating_without_delivering_id(self):
        self.fake.create_reply = "truncated-id\n"
        self.fake.create_exit = 7
        _, _, result = self.complete(self.launch(), 7)
        self.assertFalse(result["started"])
        self.assertFalse(any(item["args"][0] == "start" for item in self.fake.requests))
        self.assertFalse(self.fake.states)

    def test_power_loss_during_create_never_starts_workload(self):
        self.fake.create_gate.clear()
        child = self.launch()
        self.fake.wait_request("create")
        self.ac.write_text("0\n")
        self.fake.wait_event("sample", lambda value: value["phase"] == "create" and value["offline"])
        self.assertIsNone(child.poll())
        self.fake.create_gate.set()
        self.complete(child, 3)
        self.assertFalse(any(item["args"][0] == "start" for item in self.fake.requests))
        self.assertFalse(self.fake.states)

    def test_missing_create_id_is_error_and_still_cleans_allocated_container(self):
        self.fake.create_reply = "\n"
        _, _, result = self.complete(self.launch(), 125)
        self.assertFalse(result["started"])
        self.assertFalse(self.fake.states)

    def test_api_error_keeps_cleanup_owned_until_state_can_be_confirmed(self):
        self.fake.failed_remove = True
        child = self.launch()
        self.fake.wait_request("rm", count=2)
        self.assertIsNone(child.poll())
        self.assertIn(CONTAINER_ID, self.fake.states)
        result_file = next(self.logs.iterdir()) / "result.json"
        pending = json.loads(result_file.read_text())
        self.assertTrue(pending["cleanup_uncertain"])
        self.assertIsNone(pending["exit_status"])
        self.fake.failed_remove = False
        _, _, result = self.complete(child, 125)
        self.assertTrue(result["api_error_observed"])
        self.assertFalse(self.fake.states)

    def test_stop_api_failure_keeps_running_container_owned_until_recovery(self):
        self.fake.failed_stop = True
        self.fake.finish_gate.clear()
        child = self.launch()
        self.fake.wait_event("running")
        child.send_signal(signal.SIGTERM)
        self.fake.wait_request("rm", count=2)
        self.assertIsNone(child.poll())
        self.assertTrue(self.fake.states[CONTAINER_ID]["Running"])
        pending = json.loads((next(self.logs.iterdir()) / "result.json").read_text())
        self.assertTrue(pending["cleanup_uncertain"])
        self.assertIsNone(pending["exit_status"])
        self.fake.failed_stop = False
        _, _, result = self.complete(child, 143)
        self.assertTrue(result["api_error_observed"])
        self.assertFalse(self.fake.states)

    def test_owner_mismatch_never_removes_unverified_resource(self):
        self.fake.bad_owner = True
        child = self.launch()
        self.fake.wait_request("inspect", count=2)
        self.assertIsNone(child.poll())
        self.assertFalse(any(item["args"][0] == "rm" for item in self.fake.requests))
        self.fake.bad_owner = False
        self.complete(child, 125)
        self.assertFalse(self.fake.states)

    def test_name_replacement_cannot_redirect_removal(self):
        self.fake.replace_name = True
        self.complete(self.launch(), 0)
        self.assertEqual(set(self.fake.states), {OTHER_ID})
        self.assertFalse(any(item["args"][0] == "rm" and item["args"][-1] == OTHER_ID for item in self.fake.requests))

    def test_power_sampling_continues_during_blocked_api(self):
        self.fake.inspect_gate.clear()
        child = self.launch()
        self.fake.wait_request("inspect")
        self.ac.write_text("0\n")
        self.fake.wait_event("sample", lambda value: value["offline"] and value["phase"] == "before-start")
        self.assertIsNone(child.poll())
        self.fake.inspect_gate.set()
        self.complete(child, 3)
        self.assertFalse(any(item["args"][0] == "start" for item in self.fake.requests))

    def test_output_replay_failure_is_not_success(self):
        output = open("/dev/full", "wb")
        self.output_files.append(output)
        child = self.launch(stdout=output)
        _, _, result = self.complete(child, 125)
        self.assertTrue(result["replay_failed"])
        self.assertFalse(self.fake.states)

    def test_failed_cleanup_diagnostic_does_not_abandon_ownership_or_power_log(self):
        output = open("/dev/full", "wb")
        self.output_files.append(output)
        self.fake.failed_remove = True
        child = self.launch(stderr=output)
        self.fake.wait_request("rm", count=2)
        self.assertIsNone(child.poll())
        self.fake.failed_remove = False
        _, _, result = self.complete(child, 125)
        self.assertTrue(result["logging_failed"])
        self.assertTrue(result["valid_power_interval"])
        self.assertFalse(result["power_log_failed"])
        self.assertFalse(self.fake.states)

    def test_non_run_interface_is_unchanged_even_when_offline(self):
        self.ac.write_text("0\n")
        child = self.launch(arguments=["image", "inspect", "owned-image"])
        output, error = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, error)
        self.assertEqual(output, b"owned passthrough\n")
        self.assertEqual(self.fake.requests[0]["args"], ["image", "inspect", "owned-image"])
        self.assertFalse(list(self.logs.iterdir()))


if __name__ == "__main__":
    unittest.main()
