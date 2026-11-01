"""Unit tests for page preparation and response-wait ownership."""
import concurrent.futures
import copy
import importlib.util
import inspect
import itertools
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request


SOURCE = Path(os.environ.get(
    "CDP_DAEMON_SOURCE", Path(__file__).resolve().parents[1] / "cdp_daemon.py"))
sys.path.insert(0, str(SOURCE.parent))
DOCUMENT = {
    "url": "https://example.test/",
    "readyState": "complete",
    "visibilityState": "visible",
    "hasFocus": True,
    "window": {"width": 800, "height": 600},
    "wasDiscarded": False,
    "animationCallbacks": 0,
}


def load_daemon():
    spec = importlib.util.spec_from_file_location("preparation_test_daemon", SOURCE)
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)
    return daemon


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.daemon.cdp_sock = object()
        self.calls = []
        self.transport = mock.patch.object(self.daemon, "cdp_call", side_effect=self.respond)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def respond(self, method, params=None, **kwargs):
        self.calls.append((method, params, kwargs))
        if method == "Target.getTargetInfo":
            return {"result": {"targetInfo": {"targetId": "target", "type": "page"}}}
        if method == "Target.createTarget":
            return {"result": {"targetId": "bridge-target"}}
        if method == "Target.closeTarget":
            return {"result": {"success": True}}
        if method == "Target.attachToTarget":
            session = "bridge-session" if params["targetId"] == "bridge-target" else "control-session"
            return {"result": {"sessionId": session}}
        if method == "Runtime.evaluate":
            if kwargs.get("sessionId") == "bridge-session":
                value = {"available": True, "found": True, "targetId": "target", "tabId": 123,
                         "status": "complete", "discarded": False, "frozen": False, "active": False}
                if "initialize-view" in params["expression"]:
                    value = {"ok": True, "result": {"changed": True, "restored": True}}
            else:
                value = copy.deepcopy(DOCUMENT)
                if 'const mode = "animation"' in params["expression"]:
                    value["animationCallbacks"] = 2
            return {"result": {"result": {"type": "object", "value": value}}}
        if method == "Page.getFrameTree":
            return {"result": {"frameTree": {"frame": {"id": "main"}}}}
        if method == "Page.getLayoutMetrics":
            return {"result": {"cssVisualViewport": {"clientWidth": 800, "clientHeight": 600}}}
        return {"result": {}}

    def prepare(self, **kwargs):
        if kwargs.pop("allow_tab_selection", False):
            return self.daemon.page_preparation._prototype_prepare_with_selection("session", **kwargs)
        return self.daemon.prepare_page("session", **kwargs)

    def assert_released_observation(self):
        self.assertEqual(self.daemon.page_preparation.active_states, set())

    def start_call(self, function, *args):
        future = concurrent.futures.Future()

        def run():
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        return future

    def test_focus_precedes_renderer_commands_and_never_navigates(self):
        result = self.prepare()
        client = [call for call in self.calls if call[2].get("sessionId") == "session"]
        control = [call for call in self.calls if call[2].get("sessionId") == "control-session"]
        self.assertEqual([call[0] for call in client], [
            "Target.getTargetInfo", "Page.enable", "Page.getFrameTree", "Runtime.enable",
            "Runtime.evaluate",
        ])
        self.assertEqual([call[0] for call in control], [
            "Emulation.setFocusEmulationEnabled", "Page.setWebLifecycleState",
        ])
        self.assertEqual(control[0][1], {"enabled": True})
        self.assertEqual(control[1][1], {"state": "active"})
        self.assertLess(self.calls.index(control[0]), self.calls.index(client[1]))
        self.assertFalse(any(call[0] in ("Page.reload", "Page.navigate", "Page.navigateToHistoryEntry",
                                        "Target.activateTarget") for call in self.calls))
        self.assertEqual(result["sessionId"], "session")
        self.assertEqual(result["targetId"], "target")
        self.assertEqual(result["readiness"], "dom")
        self.assertEqual(result["document"], DOCUMENT)
        self.assertEqual(result["initialTabState"]["status"], "complete")
        self.assert_released_observation()

    def test_all_readiness_modes_are_passed_to_the_expression(self):
        for readiness in ("javascript", "dom", "load", "animation"):
            with self.subTest(readiness=readiness):
                self.prepare(readiness=readiness)
                params = [call[1] for call in self.calls if call[0] == "Runtime.evaluate" and
                          call[2].get("sessionId") == "session"][-1]
                self.assertIn("const mode = " + json.dumps(readiness), params["expression"])
                self.assertTrue(params["awaitPromise"])
                self.assertTrue(params["returnByValue"])

    def test_non_rendering_readiness_does_not_request_layout_metrics(self):
        for readiness in ("javascript", "dom", "load"):
            self.calls.clear()
            self.prepare(readiness=readiness)
            self.assertFalse(any(call[0] == "Page.getLayoutMetrics" for call in self.calls))

    def test_animation_revalidates_final_dimensions_after_callbacks(self):
        for changed in ("window", "viewport"):
            with self.subTest(changed=changed):
                original = self.respond
                callbacks = False

                def resize(method, params=None, **kwargs):
                    nonlocal callbacks
                    response = original(method, params, **kwargs)
                    if (method == "Runtime.evaluate" and kwargs.get("sessionId") == "session" and
                            'const mode = "animation"' in params["expression"]):
                        callbacks = True
                        if changed == "window":
                            response["result"]["result"]["value"]["window"] = {"width": 0, "height": 0}
                    if method == "Page.getLayoutMetrics" and callbacks and changed == "viewport":
                        response["result"]["cssVisualViewport"] = {"clientWidth": 0, "clientHeight": 0}
                    return response

                with mock.patch.object(self.daemon, "cdp_call", side_effect=resize):
                    with self.assertRaises(self.daemon.PagePreparationError) as caught:
                        self.prepare(readiness="animation")
                self.assertTrue(callbacks)
                self.assertEqual(caught.exception.kind, "view_uninitialized")
                self.assertEqual(caught.exception.stage, "animation")
                self.assertEqual(caught.exception.details["document"]["animationCallbacks"], 2)

    def test_animation_revalidates_final_dom_state_and_callback_count(self):
        for changed in ("readyState", "animationCallbacks"):
            with self.subTest(changed=changed):
                original = self.respond

                def invalidate(method, params=None, **kwargs):
                    response = original(method, params, **kwargs)
                    if (method == "Runtime.evaluate" and kwargs.get("sessionId") == "session" and
                            'const mode = "animation"' in params["expression"]):
                        response["result"]["result"]["value"][changed] = "loading" if changed == "readyState" else 0
                    return response

                with mock.patch.object(self.daemon, "cdp_call", side_effect=invalidate):
                    with self.assertRaises(self.daemon.PagePreparationError) as caught:
                        self.prepare(readiness="animation")
                self.assertEqual(caught.exception.kind, "readiness")
                self.assertEqual(caught.exception.stage, "animation")

    def test_null_timeout_waits_without_a_deadline(self):
        self.prepare(timeout_seconds=None)
        self.assertTrue(all(call[2]["timeout_seconds"] is None for call in self.calls))

    def test_one_deadline_covers_all_commands(self):
        with mock.patch.object(self.daemon.time, "monotonic", side_effect=itertools.count(100)):
            self.prepare(timeout_seconds=100)
        waits = [call[2]["timeout_seconds"] for call in self.calls if call[2]["timeout_seconds"] is not None]
        self.assertTrue(all(0 < wait < 100 for wait in waits))
        self.assertEqual(waits, sorted(set(waits), reverse=True))
        self.assertLess(waits[-1], waits[0])

    def test_expired_budget_stops_before_another_command(self):
        with mock.patch.object(self.daemon.time, "monotonic", side_effect=[100, 101, 110]):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare(timeout_seconds=5)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(caught.exception.stage, "control")
        self.assertEqual([call[0] for call in self.calls], ["Target.getTargetInfo"])
        self.assert_released_observation()

    def test_invalid_requests_do_not_send_commands(self):
        invalid = [
            {"session_id": None}, {"session_id": ""}, {"session_id": []},
            {"readiness": "unknown"}, {"readiness": []},
            {"timeout_seconds": 0}, {"timeout_seconds": -1},
            {"timeout_seconds": True}, {"timeout_seconds": "15"},
            {"timeout_seconds": float("inf")}, {"timeout_seconds": float("nan")},
        ]
        for options in invalid:
            with self.subTest(options=options):
                arguments = {"session_id": "session", **options}
                with self.assertRaises(self.daemon.PagePreparationError) as caught:
                    self.daemon.prepare_page(**arguments)
                self.assertEqual(caught.exception.kind, "invalid_request")
        self.assertEqual(self.calls, [])
        self.assert_released_observation()

    def test_workers_and_prerenders_are_not_modified(self):
        for target in ({"type": "service_worker"}, {"type": "page", "subtype": "prerender"}):
            with self.subTest(target=target):
                self.calls.clear()
                with mock.patch.object(self.daemon, "cdp_call", return_value={
                    "result": {"targetInfo": {"targetId": "target", **target}},
                }) as transport:
                    with self.assertRaises(self.daemon.PagePreparationError) as caught:
                        self.prepare()
                self.assertEqual(caught.exception.kind, "unsupported_target")
                self.assertEqual(transport.call_count, 1)
        self.assert_released_observation()

    def test_timeout_keeps_the_callers_session_and_already_sent_commands(self):
        original = self.respond

        def focus_timeout(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Emulation.setFocusEmulationEnabled" and kwargs.get("sessionId") == "control-session":
                raise TimeoutError("waiting for focus response")
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=focus_timeout):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(caught.exception.stage, "focus")
        self.assertEqual(caught.exception.session_id, "session")
        self.assertEqual([call[0] for call in self.calls if call[2].get("sessionId") == "control-session"],
                         ["Emulation.setFocusEmulationEnabled"])
        self.assertFalse(any(call[0] == "Target.detachFromTarget" for call in self.calls))
        self.assertEqual(self.daemon.page_preparation.status()[0]["clients"], 1)
        self.calls.clear()
        self.prepare()
        self.assertEqual(self.calls[-1][0], "Runtime.evaluate")
        self.assert_released_observation()

    def test_observed_navigation_allows_repeating_only_the_readiness_probe(self):
        original = self.respond
        reads = 0

        def navigate(method, params=None, **kwargs):
            nonlocal reads
            response = original(method, params, **kwargs)
            if method == "Runtime.evaluate" and kwargs.get("sessionId") == "session":
                reads += 1
                if reads == 1:
                    self.daemon.notify_preparations({
                        "method": "Runtime.executionContextsCleared", "sessionId": "session",
                    })
                    raise self.daemon.CDPError({"code": -32000, "message": "arbitrary wording"})
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=navigate):
            self.prepare()
        self.assertEqual(reads, 2)
        self.assertEqual([call[0] for call in self.calls].count("Page.enable"), 1)
        self.assert_released_observation()

    def test_navigation_invalidates_an_otherwise_successful_snapshot(self):
        original = self.respond
        reads = 0

        def navigate(method, params=None, **kwargs):
            nonlocal reads
            response = original(method, params, **kwargs)
            if method == "Runtime.evaluate" and kwargs.get("sessionId") == "session":
                reads += 1
                if reads == 1:
                    self.daemon.notify_preparations({
                        "method": "Page.frameNavigated", "sessionId": "session",
                        "params": {"frame": {"id": "main", "url": "https://new.test/"}},
                    })
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=navigate):
            self.prepare()
        self.assertEqual(reads, 2)

    def test_protocol_errors_without_navigation_are_preserved(self):
        error = {"code": -32000, "message": "page is unavailable", "data": "diagnostic"}
        original = self.respond

        def reject(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Runtime.evaluate" and kwargs.get("sessionId") == "session":
                raise self.daemon.CDPError(error)
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=reject):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.kind, "cdp")
        self.assertEqual(caught.exception.details, error)
        self.assertEqual(sum(call[0] == "Runtime.evaluate" and call[2].get("sessionId") == "session" for call in self.calls), 1)

    def test_javascript_exceptions_are_not_empty_successes(self):
        original = self.respond
        details = {"text": "Uncaught", "exceptionId": 3}

        def exception(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Runtime.evaluate" and kwargs.get("sessionId") == "session":
                return {"result": {"exceptionDetails": details}}
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=exception):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.kind, "javascript")
        self.assertEqual(caught.exception.details, details)
        self.assert_released_observation()

    def test_missing_snapshot_is_an_error(self):
        original = self.respond

        def missing(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Runtime.evaluate" and kwargs.get("sessionId") == "session":
                return {"result": {"result": {"type": "undefined"}}}
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=missing):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.kind, "invalid_response")

    def test_visibility_failure_is_reported(self):
        original = self.respond

        def hidden(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Runtime.evaluate" and kwargs.get("sessionId") == "session":
                response["result"]["result"]["value"]["visibilityState"] = "hidden"
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=hidden):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.kind, "visibility")

    def test_other_sessions_do_not_invalidate_or_abort_preparation(self):
        state = self.daemon.PagePreparationState("session")
        for method in ("Runtime.executionContextsCleared", "Inspector.targetCrashed"):
            state.observe({"method": method, "sessionId": "other"})
        state.observe({"method": "Target.detachedFromTarget", "params": {"sessionId": "other"}})
        self.assertEqual(state.navigation_revision(), 0)
        state.check()

    def test_browser_detach_event_uses_its_parameter_session_id(self):
        state = self.daemon.PagePreparationState("session")
        waiter = threading.Event()
        state.register(waiter)
        state.observe({"method": "Target.detachedFromTarget", "params": {"sessionId": "session"}})
        self.assertTrue(waiter.is_set())
        with self.assertRaises(self.daemon.PagePreparationError) as caught:
            state.check()
        self.assertEqual(caught.exception.kind, "detached")

    def test_iframe_context_changes_do_not_invalidate_the_main_document(self):
        state = self.daemon.PagePreparationState("session")
        state.bind_frame("main")
        state.observe({"method": "Runtime.executionContextCreated", "sessionId": "session",
                       "params": {"context": {"id": 7, "auxData": {"isDefault": True, "frameId": "child"}}}})
        state.observe({"method": "Runtime.executionContextDestroyed", "sessionId": "session",
                       "params": {"executionContextId": 7}})
        state.observe({"method": "Page.frameStartedNavigating", "sessionId": "session",
                       "params": {"frameId": "child"}})
        state.observe({"method": "Page.frameNavigated", "sessionId": "session",
                       "params": {"frame": {"id": "child", "parentId": "main"}}})
        self.assertEqual(state.navigation_revision(), 0)
        state.observe({"method": "Runtime.executionContextCreated", "sessionId": "session",
                       "params": {"context": {"id": 8, "auxData": {"isDefault": True, "frameId": "main"}}}})
        state.observe({"method": "Runtime.executionContextDestroyed", "sessionId": "session",
                       "params": {"executionContextId": 8}})
        self.assertEqual(state.navigation_revision(), 2)

    def test_the_first_terminal_event_is_retained(self):
        state = self.daemon.PagePreparationState("session")
        state.observe({"method": "Inspector.targetCrashed", "sessionId": "session"})
        state.observe({"method": "Target.detachedFromTarget", "params": {"sessionId": "session"}})
        with self.assertRaises(self.daemon.PagePreparationError) as caught:
            state.check()
        self.assertEqual(caught.exception.kind, "crashed")

    def test_a_detach_before_the_attach_reply_is_accounted_for(self):
        original = self.respond

        def detach_before_reply(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Target.attachToTarget" and params["targetId"] == "target":
                self.daemon.notify_preparations({"method": "Target.detachedFromTarget",
                                                "params": {"sessionId": "control-session"}})
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=detach_before_reply):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare()
            self.assertEqual(caught.exception.kind, "detached")
            self.daemon.release_page("session")
        self.assertFalse(any(call[0] == "Target.detachFromTarget" for call in self.calls))
        self.assertEqual(self.daemon.page_preparation.status(), [])
        self.assert_released_observation()

    def test_clients_share_one_physical_emulation_owner(self):
        self.prepare()
        self.daemon.prepare_page("other")
        attaches = [call for call in self.calls if call[0] == "Target.attachToTarget" and
                    call[1]["targetId"] == "target"]
        self.assertEqual(len(attaches), 1)
        self.assertEqual(self.daemon.page_preparation.status()[0]["clients"], 2)
        self.daemon.release_page("session")
        self.assertFalse(any(call[0] == "Target.detachFromTarget" for call in self.calls))
        self.assertEqual(self.daemon.page_preparation.status()[0]["clients"], 1)
        self.daemon.release_page("other")
        detaches = [call[1] for call in self.calls if call[0] == "Target.detachFromTarget"]
        self.assertEqual(detaches, [{"sessionId": "control-session"}])
        self.assertEqual(self.daemon.page_preparation.status(), [])

    def test_raw_client_detach_releases_the_last_owner(self):
        self.prepare()
        control = self.daemon.page_preparation.controls["target"]
        self.daemon.notify_preparations({"method": "Target.detachedFromTarget",
                                        "params": {"sessionId": "session"}})
        self.assertTrue(control.completed.wait(2))
        self.assertEqual(self.daemon.page_preparation.status(), [])
        self.assertEqual([call[1] for call in self.calls if call[0] == "Target.detachFromTarget"],
                         [{"sessionId": "control-session"}])

    def test_a_late_attach_reply_is_released_after_the_client_leaves(self):
        entered, finish_attach = threading.Event(), threading.Event()
        original = self.respond

        def delayed_attach(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Target.attachToTarget" and params["targetId"] == "target":
                entered.set()
                finish_attach.wait()
            return response

        manager = self.daemon.page_preparation
        original_wait = manager._wait

        def expired_wait(control, event, state, remaining):
            if not event.is_set():
                raise TimeoutError("test response wait expired")
            return original_wait(control, event, state, remaining)

        try:
            with mock.patch.object(self.daemon, "cdp_call", side_effect=delayed_attach):
                with mock.patch.object(manager, "_wait", side_effect=expired_wait):
                    with self.assertRaises(self.daemon.PagePreparationError) as caught:
                        self.prepare()
                    self.assertEqual(caught.exception.kind, "timeout")
                    self.assertTrue(entered.wait(2))
                    control = manager.controls["target"]
                    with self.assertRaises(self.daemon.PagePreparationError):
                        self.daemon.release_page("session")
                finish_attach.set()
                self.assertTrue(control.completed.wait(2))
                self.assertEqual(manager.status(), [])
            self.assertEqual([call[1] for call in self.calls if call[0] == "Target.detachFromTarget"],
                             [{"sessionId": "control-session"}])
        finally:
            finish_attach.set()

    def test_a_replacement_owner_waits_for_the_previous_detach(self):
        self.prepare()
        manager = self.daemon.page_preparation
        entered, finish_detach, next_waiting = threading.Event(), threading.Event(), threading.Event()
        original = self.respond
        original_wait = manager._wait

        def delayed_detach(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Target.detachFromTarget" and params["sessionId"] == "control-session":
                entered.set()
                finish_detach.wait()
            return response

        def observe_wait(control, event, state, remaining):
            if state.session_id == "next" and getattr(control, "phase", None) == "releasing":
                next_waiting.set()
            return original_wait(control, event, state, remaining)

        try:
            with mock.patch.object(self.daemon, "cdp_call", side_effect=delayed_detach):
                with mock.patch.object(manager, "_wait", side_effect=observe_wait):
                    release = self.start_call(self.daemon.release_page, "session")
                    self.assertTrue(entered.wait(2))
                    next_request = self.start_call(self.daemon.prepare_page, "next")
                    self.assertTrue(next_waiting.wait(2))
                    self.assertEqual(sum(call[0] == "Target.attachToTarget" and
                        call[1]["targetId"] == "target" for call in self.calls), 1)
                    finish_detach.set()
                    release.result(2)
                    next_request.result(2)
                self.assertEqual(sum(call[0] == "Target.attachToTarget" and
                    call[1]["targetId"] == "target" for call in self.calls), 2)
                self.daemon.release_page("next")
        finally:
            finish_detach.set()

    def test_missing_extension_is_reported_and_can_be_retried_after_installation(self):
        original = self.respond
        installed = False

        def availability(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if (method == "Runtime.evaluate" and kwargs.get("sessionId") == "bridge-session" and
                    "const targetId = null" in params["expression"] and not installed):
                response["result"]["result"]["value"] = {"available": False}
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=availability):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare()
            self.assertEqual(caught.exception.kind, "browser_state_unavailable")
            self.assertEqual(sum(call[0] == "Target.closeTarget" for call in self.calls), 1)
            self.assertFalse(any(call[0] == "Emulation.setFocusEmulationEnabled" and
                call[2].get("sessionId") == "control-session" for call in self.calls))
            installed = True
            self.prepare()
            self.daemon.release_page("session")
        self.assertEqual(sum(call[0] == "Target.createTarget" for call in self.calls), 2)

    def test_failed_extension_cleanup_is_retained_and_retried_before_replacement(self):
        original = self.respond
        installed = False
        cleanup_fails = True

        def availability(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if (method == "Runtime.evaluate" and kwargs.get("sessionId") == "bridge-session" and
                    "const targetId = null" in params["expression"] and not installed):
                response["result"]["result"]["value"] = {"available": False}
            if method == "Target.closeTarget" and cleanup_fails:
                raise self.daemon.CDPError({"code": -32000, "message": "close rejected"})
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=availability):
            with self.assertRaises(self.daemon.PagePreparationError):
                self.prepare()
            self.assertEqual(self.daemon.page_preparation.bridge_status()[0]["state"], "release_failed")
            self.assertEqual(sum(call[0] == "Target.createTarget" for call in self.calls), 1)
            installed, cleanup_fails = True, False
            self.prepare()
            self.daemon.release_page("session")
        self.assertEqual(sum(call[0] == "Target.closeTarget" for call in self.calls), 2)
        self.assertEqual(sum(call[0] == "Target.createTarget" for call in self.calls), 2)

    def test_concurrent_discarded_preparations_share_one_restoration(self):
        manager = self.daemon.page_preparation
        original_response = self.respond
        original_tab_state = manager._tab_state
        original_wait = manager._wait
        held, finish, second_entered = threading.Event(), threading.Event(), threading.Event()
        counts = {}
        restored = 0
        discarded = True

        def transport(method, params=None, **kwargs):
            nonlocal restored, discarded
            response = original_response(method, params, **kwargs)
            if (method == "Runtime.evaluate" and kwargs.get("sessionId") == "bridge-session" and
                    "const targetId = null" not in params["expression"]):
                response["result"]["result"]["value"].update(
                    status="unloaded" if discarded else "loading", discarded=discarded)
            if method == "Page.getNavigationHistory":
                return {"result": {"currentIndex": 0, "entries": [{"id": 7}]}}
            if method == "Page.navigateToHistoryEntry":
                restored += 1
                discarded = False
            return response

        def tab_state(target, state, remaining):
            result = original_tab_state(target, state, remaining)
            count = counts.get(state.session_id, 0) + 1
            counts[state.session_id] = count
            if count == 2:
                if state.session_id == "other":
                    second_entered.set()
                elif not held.is_set():
                    held.set()
                    finish.wait()
            return result

        def wait(control, event, state, remaining):
            if state.session_id == "other" and not event.is_set():
                second_entered.set()
            return original_wait(control, event, state, remaining)

        try:
            with mock.patch.object(self.daemon, "cdp_call", side_effect=transport):
                with mock.patch.object(manager, "_tab_state", side_effect=tab_state):
                    with mock.patch.object(manager, "_wait", side_effect=wait):
                        first = self.start_call(self.daemon.prepare_page, "session")
                        self.assertTrue(held.wait(2))
                        other = self.start_call(self.daemon.prepare_page, "other")
                        self.assertTrue(second_entered.wait(2))
                        finish.set()
                        first.result(2)
                        other.result(2)
                self.assertEqual(restored, 1)
                self.daemon.release_page("session")
                self.daemon.release_page("other")
        finally:
            finish.set()

    def test_attach_thread_failure_completes_its_wait_and_can_be_released(self):
        with mock.patch.object(self.daemon.threading.Thread, "start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare(timeout_seconds=None)
        self.assertEqual(caught.exception.kind, "transport")
        control = self.daemon.page_preparation.controls["target"]
        self.assertTrue(control.ready.is_set())
        self.daemon.release_page("session")
        self.assertEqual(self.daemon.page_preparation.status(), [])

    def test_initialization_thread_failure_completes_its_result(self):
        self.prepare()
        with mock.patch.object(self.daemon.threading.Thread, "start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare(timeout_seconds=None)
        self.assertEqual(caught.exception.kind, "transport")
        self.assertTrue(self.daemon.page_preparation.controls["target"].initialization.ready.is_set())
        self.daemon.release_page("session")
        self.assertEqual(self.daemon.page_preparation.status(), [])

    def test_cleanup_thread_failure_is_visible_and_retryable(self):
        self.prepare()
        manager = self.daemon.page_preparation
        with mock.patch.object(self.daemon.threading.Thread, "start", side_effect=RuntimeError("thread unavailable")):
            manager.observe({"method": "Target.detachedFromTarget", "params": {"sessionId": "session"}})
        self.assertEqual(manager.status()[0]["state"], "release_failed")
        self.daemon.release_page("session")
        self.assertEqual(manager.status(), [])

    def test_an_already_closed_reader_is_confirmed_from_browser_state(self):
        self.prepare()
        manager = self.daemon.page_preparation
        bridge = next(iter(manager.bridges.values()))
        original = self.respond

        def gone(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Target.closeTarget":
                raise self.daemon.CDPError({"code": -32602, "message": "arbitrary close rejection"})
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [{"targetId": "target"}]}}
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=gone):
            manager._retire_bridge(bridge, RuntimeError("reader stopped"))
            self.assertTrue(bridge.completed.wait(2))
        self.assertTrue(bridge.target_closed)
        self.assertTrue(bridge.session_detached)
        self.assertEqual(bridge.phase, "failed")
        self.prepare()
        self.assertEqual(sum(call[0] == "Target.createTarget" for call in self.calls), 2)

    def test_discarded_tabs_restore_the_current_history_entry(self):
        original = self.respond

        def discarded(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if (method == "Runtime.evaluate" and kwargs.get("sessionId") == "bridge-session" and
                    "const targetId = null" not in params["expression"]):
                response["result"]["result"]["value"].update(status="unloaded", discarded=True)
            if method == "Page.getNavigationHistory":
                return {"result": {"currentIndex": 1, "entries": [{"id": 3}, {"id": 7}]}}
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=discarded):
            result = self.prepare()
        navigation = [call[1] for call in self.calls if call[0] == "Page.navigateToHistoryEntry"]
        self.assertEqual(navigation, [{"entryId": 7}])
        self.assertTrue(result["initialTabState"]["discarded"])

    def test_a_load_started_during_history_inspection_is_not_replaced(self):
        original = self.respond
        reads = 0

        def loading(method, params=None, **kwargs):
            nonlocal reads
            response = original(method, params, **kwargs)
            if (method == "Runtime.evaluate" and kwargs.get("sessionId") == "bridge-session" and
                    "const targetId = null" not in params["expression"]):
                reads += 1
                response["result"]["result"]["value"].update(
                    status="unloaded" if reads == 1 else "loading", discarded=reads == 1)
            if method == "Page.getNavigationHistory":
                return {"result": {"currentIndex": 0, "entries": [{"id": 7}]}}
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=loading):
            self.prepare()
        self.assertEqual(reads, 2)
        self.assertFalse(any(call[0] == "Page.navigateToHistoryEntry" for call in self.calls))

    def test_missing_browser_lifecycle_fields_are_an_error(self):
        original = self.respond

        def incomplete(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if (method == "Runtime.evaluate" and kwargs.get("sessionId") == "bridge-session" and
                    "const targetId = null" not in params["expression"]):
                response["result"]["result"]["value"].pop("discarded")
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=incomplete):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.kind, "invalid_response")
        self.assertEqual(caught.exception.stage, "browser_state")

    def test_strict_animation_reports_zero_view_and_private_prototype_can_initialize(self):
        original = self.respond
        initialized = False

        def dimensions(method, params=None, **kwargs):
            nonlocal initialized
            response = original(method, params, **kwargs)
            if method == "Runtime.evaluate" and "initialize-view" in params["expression"]:
                initialized = True
            if method == "Runtime.evaluate" and kwargs.get("sessionId") == "session" and not initialized:
                response["result"]["result"]["value"]["window"] = {"width": 0, "height": 0}
            if method == "Page.getLayoutMetrics" and not initialized:
                response["result"]["cssVisualViewport"] = {"clientWidth": 0, "clientHeight": 0}
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=dimensions):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare(readiness="animation")
            self.assertEqual(caught.exception.kind, "view_uninitialized")
            self.assertFalse(initialized)
            result = self.prepare(readiness="animation", allow_tab_selection=True)
        self.assertTrue(initialized)
        self.assertTrue(result["viewInitialization"]["restored"])
        self.assertEqual(result["document"]["animationCallbacks"], 2)

    def test_failed_selection_restoration_is_reported(self):
        original = self.respond

        def moved(method, params=None, **kwargs):
            response = original(method, params, **kwargs)
            if method == "Runtime.evaluate" and kwargs.get("sessionId") == "session":
                response["result"]["result"]["value"]["window"] = {"width": 0, "height": 0}
            if method == "Runtime.evaluate" and "initialize-view" in params["expression"]:
                response["result"]["result"]["value"]["result"] = {
                    "changed": True, "restored": False, "restoreSkipped": "previous_tab_moved"}
            return response

        with mock.patch.object(self.daemon, "cdp_call", side_effect=moved):
            with self.assertRaises(self.daemon.PagePreparationError) as caught:
                self.prepare(readiness="animation", allow_tab_selection=True)
        self.assertEqual(caught.exception.kind, "selection_restore")


class CDPTransportTests(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.daemon.cdp_sock = mock.Mock()

    def test_send_failure_removes_response_waiter(self):
        with mock.patch.object(self.daemon, "ws_send", side_effect=OSError("closed")):
            with self.assertRaises(OSError):
                self.daemon.cdp_call("Runtime.evaluate", {"expression": "1"})
        self.assertEqual(self.daemon.pending, {})

    def test_raw_timeout_does_not_send_cancellation_or_detach(self):
        with mock.patch.object(self.daemon, "ws_send") as send:
            with self.assertRaises(TimeoutError):
                self.daemon.cdp_call("Runtime.evaluate", {"expression": "later()"}, timeout_seconds=0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(json.loads(send.call_args.args[0])["method"], "Runtime.evaluate")
        self.assertEqual(self.daemon.pending, {})

    def test_terminal_events_end_a_guarded_unbounded_wait(self):
        for method, params, kind in (
            ("Inspector.targetCrashed", {}, "crashed"),
            ("Inspector.detached", {"reason": "closed"}, "detached"),
            ("Target.detachedFromTarget", {"sessionId": "session"}, "detached"),
            ("daemon.disconnected", {}, "disconnected"),
        ):
            with self.subTest(method=method):
                state = self.daemon.PagePreparationState("session")
                self.daemon.page_preparation.active_states.add(state)

                def terminal(_):
                    self.daemon.notify_preparations({
                        "method": method, "sessionId": "session", "params": params,
                    })

                with mock.patch.object(self.daemon, "ws_send", side_effect=terminal):
                    with self.assertRaises(self.daemon.PagePreparationError) as caught:
                        self.daemon.cdp_call("Runtime.evaluate", sessionId="session",
                                             timeout_seconds=None, _page_state=state)
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(state.waiters, set())
                self.assertEqual(self.daemon.pending, {})
                self.daemon.page_preparation.active_states.clear()

    def test_already_terminal_state_does_not_send_a_command(self):
        state = self.daemon.PagePreparationState("session")
        state.observe({"method": "daemon.disconnected"})
        with mock.patch.object(self.daemon, "ws_send") as send:
            with self.assertRaises(self.daemon.PagePreparationError):
                self.daemon.cdp_call("Page.enable", _page_state=state)
        send.assert_not_called()
        self.assertEqual(self.daemon.pending, {})

    def test_reconnect_closes_and_joins_the_old_reader_before_connecting_again(self):
        client, browser = socket.socketpair()
        started, finished = threading.Event(), threading.Event()
        pending_reply = threading.Event()
        self.daemon.pending[99] = pending_reply
        self.daemon.cdp_sock = client
        self.daemon.cdp_leftover = b""
        original_reader = self.daemon.reader_thread
        connections = 0
        observed = {}
        future = concurrent.futures.Future()

        def reader():
            started.set()
            try:
                original_reader()
            finally:
                finished.set()

        def connect():
            nonlocal connections
            connections += 1
            if connections == 1:
                return True
            observed.update(closed=client.fileno() == -1, reader_finished=finished.is_set(),
                            pending_ended=pending_reply.is_set())
            raise StopIteration("test completed")

        def manage():
            try:
                self.daemon.connection_manager()
            except BaseException as error:
                future.set_exception(error)

        manager = threading.Thread(target=manage, daemon=True)
        try:
            with mock.patch.object(self.daemon, "connect_once", side_effect=connect):
                with mock.patch.object(self.daemon, "reader_thread", side_effect=reader):
                    manager.start()
                    self.assertTrue(started.wait(2))
                    self.daemon.reconnect_now.set()
                    wake = getattr(self.daemon, "connection_wake", None)
                    if wake is not None:
                        wake.set()
                    with self.assertRaises(StopIteration):
                        future.result(3)
            self.assertEqual(observed, {"closed": True, "reader_finished": True, "pending_ended": True})
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
            browser.close()
            finished.wait(2)
            manager.join(2)
            self.daemon.pending.clear()

    def test_send_uses_the_socket_that_passed_its_identity_check(self):
        original = self.daemon.cdp_sock
        replacement = mock.Mock()
        lines, first = inspect.getsourcelines(self.daemon.ws_send)
        send_line = first + next(index for index, line in enumerate(lines) if ".sendall(" in line)
        previous_trace = sys.gettrace()

        def replace_at_send(frame, event, argument):
            if frame.f_code is self.daemon.ws_send.__code__ and event == "line" and frame.f_lineno == send_line:
                self.daemon.cdp_sock = replacement
            return replace_at_send

        try:
            sys.settrace(replace_at_send)
            self.daemon.ws_send('{"method":"Target.getTargets"}', _expected_socket=original)
        finally:
            sys.settrace(previous_trace)
        original.sendall.assert_called_once()
        replacement.sendall.assert_not_called()

    def test_a_control_command_cannot_be_sent_on_a_replacement_connection(self):
        previous = mock.Mock()
        current = self.daemon.cdp_sock
        with self.assertRaises(RuntimeError):
            self.daemon.cdp_call("Target.detachFromTarget", {"sessionId": "old-session"},
                                 _expected_socket=previous)
        previous.sendall.assert_not_called()
        current.sendall.assert_not_called()
        self.assertEqual(self.daemon.pending, {})

    def test_late_reply_is_ignored_and_subsequent_events_are_read(self):
        messages = iter([
            json.dumps({"id": 99, "result": {"result": {"value": "late"}}}),
            json.dumps({"method": "Runtime.consoleAPICalled", "sessionId": "session", "params": {}}),
            None,
        ])
        with mock.patch.object(self.daemon, "ws_recv_raw", side_effect=lambda: next(messages)):
            self.daemon.reader_thread()
        self.assertEqual(len(self.daemon.events), 1)
        self.assertEqual(self.daemon.events[0]["method"], "Runtime.consoleAPICalled")


class PreparationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.server = self.daemon.ThreadingHTTPServer(("127.0.0.1", 0), self.daemon.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def post(self, route, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}{route}",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_prepare_route_and_defaults(self):
        with mock.patch.object(self.daemon, "prepare_page", return_value={"sessionId": "session"}) as prepare:
            status, result = self.post("/prepare", {"sessionId": "session"})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"sessionId": "session"})
        prepare.assert_called_once_with("session", "dom", 15)

    def test_prepare_route_preserves_null_timeout(self):
        with mock.patch.object(self.daemon, "prepare_page", return_value={}) as prepare:
            self.post("/prepare", {"sessionId": "session", "readiness": "animation", "timeout_seconds": None})
        prepare.assert_called_once_with("session", "animation", None)

    def test_invalid_prepare_body_is_a_client_error(self):
        status, result = self.post("/prepare", [])
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["kind"], "invalid_request")

    def test_structured_preparation_error(self):
        error = self.daemon.PagePreparationError("timeout", "focus", "still waiting")
        error.session_id, error.readiness = "session", "dom"
        with mock.patch.object(self.daemon, "prepare_page", side_effect=error):
            status, result = self.post("/prepare", {"sessionId": "session"})
        self.assertEqual(status, 500)
        self.assertEqual(result["error"], {
            "kind": "timeout", "stage": "focus", "message": "still waiting",
            "sessionId": "session", "readiness": "dom",
        })

    def test_prepare_rejects_selection_and_unknown_fields(self):
        for field in ("allowTabSelection", "other"):
            with self.subTest(field=field):
                with mock.patch.object(self.daemon, "prepare_page") as prepare:
                    status, result = self.post("/prepare", {"sessionId": "session", field: True})
                self.assertEqual(status, 400)
                self.assertEqual(result["error"]["kind"], "invalid_request")
                prepare.assert_not_called()

    def test_release_route_and_defaults(self):
        with mock.patch.object(self.daemon, "release_page", return_value={"released": True}) as release:
            status, result = self.post("/release", {"sessionId": "session"})
        self.assertEqual(status, 200)
        self.assertTrue(result["released"])
        release.assert_called_once_with("session", 15)

    def test_release_rejects_unknown_fields(self):
        with mock.patch.object(self.daemon, "release_page") as release:
            status, result = self.post("/release", {"sessionId": "session", "detach": True})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["kind"], "invalid_request")
        release.assert_not_called()

    def test_reconnect_notifies_the_connection_manager(self):
        status, _ = self.post("/reconnect", {})
        self.assertEqual(status, 200)
        self.assertTrue(self.daemon.reconnect_now.is_set())
        self.assertTrue(self.daemon.connection_wake.is_set())

    def test_raw_eval_route_keeps_its_existing_response(self):
        with mock.patch.object(self.daemon, "cdp_call", return_value={
            "result": {"result": {"type": "number", "value": 4}},
        }) as call:
            status, result = self.post("/eval", {"sessionId": "session", "expression": "2+2"})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"result": {"type": "number", "value": 4}})
        self.assertEqual(call.call_args.kwargs, {"sessionId": "session", "timeout_seconds": 15})


if __name__ == "__main__":
    unittest.main()
