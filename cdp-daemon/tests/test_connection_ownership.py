"""Deterministic connection and ownership transitions without a browser."""
import concurrent.futures
import copy
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_page_preparation import DOCUMENT, load_daemon


class BrowserModel:
    """Keep native sessions and owned targets as separate browser resources."""

    def __init__(self, daemon):
        self.daemon = daemon
        self.a, self.b = object(), object()
        daemon.cdp_sock = self.a
        self.attempts = []
        self.sessions = {}
        self.targets = {}
        self.next_session = 0
        self.next_target = 0
        self.detach_first_helper = False
        self.detached_helper = False
        self.after_attach = None
        self.lock = threading.Lock()

    def disconnect(self, connection):
        self.daemon.cdp_sock = None
        with self.lock:
            self.sessions = {sid: value for sid, value in self.sessions.items()
                             if value[1] is not connection}
            self.targets = {target: value for target, value in self.targets.items()
                            if value is not connection}
        self.daemon.notify_preparations({"method": "daemon.disconnected", "connection": connection})

    def owners(self):
        with self.lock:
            return {sid: value for sid, value in self.sessions.items() if value[0] == "page"}

    def call(self, method, params=None, **kwargs):
        guard = kwargs.get("_page_state")
        if guard is not None:
            guard.check()
        connection = self.daemon.cdp_sock
        expected = kwargs.get("_expected_socket")
        with self.lock:
            self.attempts.append((method, connection, expected))
        if connection is None:
            raise RuntimeError("The model has disconnected")
        if expected is not None and expected is not connection:
            raise RuntimeError("The model connection changed")
        event = None
        attached = None
        with self.lock:
            if method == "Target.getTargetInfo":
                result = {"targetInfo": {"targetId": "page", "type": "page", "browserContextId": "profile"}}
            elif method == "Target.createTarget":
                self.next_target += 1
                target = "helper-" + str(self.next_target)
                self.targets[target] = connection
                result = {"targetId": target}
            elif method == "Target.attachToTarget":
                target = params["targetId"]
                self.next_session += 1
                sid = ("bridge-" if target.startswith("helper-") else "owner-") + str(self.next_session)
                self.sessions[sid] = (target, connection)
                result = {"sessionId": sid}
                attached = (target, sid)
                if target.startswith("helper-") and self.detach_first_helper and not self.detached_helper:
                    self.detached_helper = True
                    self.sessions.pop(sid)
                    event = {"method": "Target.detachedFromTarget", "params": {"sessionId": sid}}
            elif method == "Target.detachFromTarget":
                self.sessions.pop(params["sessionId"], None)
                result = {}
            elif method == "Target.closeTarget":
                target = params["targetId"]
                self.targets.pop(target, None)
                self.sessions = {sid: value for sid, value in self.sessions.items() if value[0] != target}
                result = {"success": True}
            elif method == "Target.getTargets":
                result = {"targetInfos": [{"targetId": target} for target in self.targets] + [{"targetId": "page"}]}
            elif method == "Runtime.evaluate":
                if str(kwargs.get("sessionId", "")).startswith("bridge-"):
                    value = {"available": True, "found": True, "targetId": "page", "tabId": 123,
                             "status": "complete", "discarded": False, "frozen": False, "active": False}
                else:
                    value = copy.deepcopy(DOCUMENT)
                    if 'const mode = "animation"' in params["expression"]:
                        value["animationCallbacks"] = 2
                result = {"result": {"type": "object", "value": value}}
            elif method == "Page.getFrameTree":
                result = {"frameTree": {"frame": {"id": "main"}}}
            elif method == "Page.getLayoutMetrics":
                result = {"cssVisualViewport": {"clientWidth": 800, "clientHeight": 600}}
            else:
                result = {}
        if event is not None:
            self.daemon.notify_preparations(event)
        if attached is not None and self.after_attach is not None:
            self.after_attach(*attached)
        if guard is not None:
            guard.check()
        return {"result": result}


class ConnectionOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.model = BrowserModel(self.daemon)
        self.manager = self.daemon.page_preparation
        self.patch = mock.patch.object(self.daemon, "cdp_call", side_effect=self.model.call)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.threads = []
        self.addCleanup(self.finish)

    def finish(self):
        self.daemon.notify_preparations({"method": "daemon.disconnected"})
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def start(self, function, *args):
        future = concurrent.futures.Future()

        def run():
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return future

    def assert_failed(self, future, kind):
        with self.assertRaises(self.daemon.PagePreparationError) as caught:
            future.result(2)
        self.assertEqual(caught.exception.kind, kind)

    def test_disconnected_start_does_not_publish_a_none_connection(self):
        self.daemon.cdp_sock = None
        with self.assertRaises(self.daemon.PagePreparationError) as caught:
            self.daemon.prepare_page("work", "javascript", None)
        self.assertEqual(caught.exception.kind, "disconnected")
        self.assertEqual(self.manager.status(), [])
        self.assertEqual(self.manager.bridge_status(), [])
        self.assertEqual(self.model.attempts, [])
        self.daemon.cdp_sock = self.model.b
        self.daemon.prepare_page("fresh", "javascript", 1)
        self.daemon.release_page("fresh", 1)
        self.assertEqual(self.model.owners(), {})

    def publication_interleaving(self, notify_before_resume):
        checked, resume = threading.Event(), threading.Event()
        original = self.daemon.PagePreparationState.check
        intercepted = False

        def check(state):
            nonlocal intercepted
            original(state)
            if state.session_id == "old" and state.stage == "control" and not intercepted:
                intercepted = True
                checked.set()
                self.assertTrue(resume.wait(2))

        try:
            with mock.patch.object(self.daemon.PagePreparationState, "check", check):
                old = self.start(self.daemon.prepare_page, "old", "javascript", None)
                self.assertTrue(checked.wait(2))
                if notify_before_resume:
                    self.model.disconnect(self.model.a)
                    self.daemon.cdp_sock = self.model.b
                else:
                    self.daemon.cdp_sock = None
                resume.set()
                self.assert_failed(old, "disconnected")
            self.assertEqual(self.manager.status(), [])
            self.assertEqual(self.manager.bridge_status(), [])
            if not notify_before_resume:
                self.daemon.notify_preparations({"method": "daemon.disconnected", "connection": self.model.a})
                self.daemon.cdp_sock = self.model.b
            self.daemon.prepare_page("fresh", "javascript", 1)
            self.daemon.release_page("fresh", 1)
            self.assertEqual(self.manager.status(), [])
            self.assertEqual(self.model.owners(), {})
            self.assertTrue(all(expected is not None for _, _, expected in self.model.attempts))
        finally:
            resume.set()

    def test_old_request_cannot_register_on_a_replacement_connection(self):
        self.publication_interleaving(True)

    def test_socket_none_before_disconnect_notification_does_not_publish(self):
        self.publication_interleaving(False)

    def delayed_start(self, role, before_thread_start):
        entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.manager._start
        held = []

        def start(resource, function, args, completion, failure_phase=None):
            is_bridge = hasattr(resource, "context_id")
            if (not held and function == self.manager._attach_control and
                    is_bridge == (role == "bridge")):
                held.append(resource)
                if before_thread_start:
                    entered.set()
                    self.assertTrue(resume.wait(2))
                    try:
                        return original(resource, function, args, completion, failure_phase)
                    finally:
                        finished.set()

                def worker(*values):
                    entered.set()
                    self.assertTrue(resume.wait(2))
                    try:
                        function(*values)
                    finally:
                        finished.set()

                return original(resource, worker, args, completion, failure_phase)
            return original(resource, function, args, completion, failure_phase)

        try:
            with mock.patch.object(self.manager, "_start", side_effect=start):
                old = self.start(self.daemon.prepare_page, "old", "javascript", None)
                self.assertTrue(entered.wait(2))
                self.assertIs(held[0].connection, self.model.a)
                self.model.disconnect(self.model.a)
                self.daemon.cdp_sock = self.model.b
                self.daemon.prepare_page("fresh", "javascript", 1)
                self.daemon.release_page("fresh", 1)
                attempts = len(self.model.attempts)
                resume.set()
                self.assertTrue(finished.wait(2))
                self.assert_failed(old, "disconnected")
            self.assertEqual(self.model.attempts[attempts:], [])
            self.assertEqual(self.manager.status(), [])
            self.assertEqual(self.model.owners(), {})
            self.assertEqual(set(self.model.targets), {bridge.target_id for bridge in self.manager.bridges.values()})
        finally:
            resume.set()

    def test_control_scheduled_after_invalidation_cannot_send_native_calls(self):
        self.delayed_start("control", True)

    def test_control_worker_started_after_invalidation_cannot_send_native_calls(self):
        self.delayed_start("control", False)

    def test_bridge_scheduled_after_invalidation_cannot_create_an_orphan(self):
        self.delayed_start("bridge", True)

    def test_bridge_worker_started_after_invalidation_cannot_create_an_orphan(self):
        self.delayed_start("bridge", False)

    def test_an_old_disconnect_notification_does_not_close_the_new_generation(self):
        self.daemon.prepare_page("a-work", "javascript", 1)
        self.model.disconnect(self.model.a)
        self.daemon.cdp_sock = self.model.b
        self.daemon.prepare_page("b-work", "javascript", 1)
        self.daemon.notify_preparations({"method": "daemon.disconnected", "connection": self.model.a})
        self.assertEqual(self.manager.status()[0]["clients"], 1)
        self.assertTrue(self.model.owners())
        self.daemon.release_page("b-work", 1)
        self.assertEqual(self.manager.status(), [])
        self.assertEqual(self.model.owners(), {})

    def test_session_detach_closes_its_owned_helper_target_before_retry(self):
        self.model.detach_first_helper = True
        with self.assertRaises(self.daemon.PagePreparationError) as caught:
            self.daemon.prepare_page("work", "javascript", 1)
        self.assertEqual(caught.exception.kind, "detached")
        bridge = next(iter(self.manager.bridges.values()))
        self.assertTrue(bridge.ready.wait(2))
        self.assertEqual(self.model.targets, {})
        self.assertTrue(bridge.session_detached)
        self.assertTrue(bridge.target_closed)
        self.daemon.prepare_page("work", "javascript", 1)
        self.assertEqual(set(self.model.targets), {reader.target_id for reader in self.manager.bridges.values()})
        self.assertEqual(len(self.model.targets), 1)
        self.daemon.release_page("work", 1)

    def test_release_keeps_an_already_sent_attach_response_accounted_for(self):
        entered, deliver = threading.Event(), threading.Event()

        def hold(target, sid):
            if target == "page":
                entered.set()
                self.assertTrue(deliver.wait(2))

        self.model.after_attach = hold
        try:
            preparation = self.start(self.daemon.prepare_page, "work", "javascript", None)
            self.assertTrue(entered.wait(2))
            release = self.start(self.daemon.release_page, "work", None)
            self.assert_failed(preparation, "released")
            self.assertTrue(self.model.owners())
            self.assertFalse(release.done())
            deliver.set()
            self.assertTrue(release.result(2)["released"])
            self.assertEqual(self.model.owners(), {})
        finally:
            deliver.set()


class ReleaseBeforeAcquisitionTests(unittest.TestCase):
    def test_release_ends_the_real_transport_wait_before_a_control_exists(self):
        daemon = load_daemon()
        daemon.cdp_sock = object()
        sent = threading.Event()
        messages = []
        future = concurrent.futures.Future()

        def send(text, **kwargs):
            messages.append(json.loads(text))
            sent.set()

        def prepare():
            try:
                future.set_result(daemon.prepare_page("work", "javascript", None))
            except BaseException as error:
                future.set_exception(error)

        thread = threading.Thread(target=prepare, daemon=True)
        try:
            with mock.patch.object(daemon, "ws_send", side_effect=send):
                thread.start()
                self.assertTrue(sent.wait(2))
                self.assertEqual(daemon.page_preparation.status(), [])
                self.assertTrue(daemon.release_page("work")["released"])
                with self.assertRaises(daemon.PagePreparationError) as caught:
                    future.result(2)
                self.assertEqual(caught.exception.kind, "released")
            self.assertEqual([message["method"] for message in messages], ["Target.getTargetInfo"])
            self.assertEqual(daemon.pending, {})
            self.assertEqual(daemon.page_preparation.active_states, set())
        finally:
            daemon.notify_preparations({"method": "daemon.disconnected"})
            thread.join(2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
