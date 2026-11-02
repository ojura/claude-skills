"""Exercise consent diagnostics with owned sockets and accessibility objects."""
import builtins
import importlib.util
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_page_preparation import load_daemon


class StopPresser(BaseException):
    pass


class Node:
    def __init__(self, name='', role='panel', children=(), result=True, action='press'):
        self.name, self.role, self.children = name, role, children
        self.result, self.action, self.calls = result, action, 0

    def get_name(self):
        return self.name

    def get_role_name(self):
        return self.role

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_action_iface(self):
        return self

    def get_n_actions(self):
        return 1

    def get_action_name(self, index):
        return self.action

    def do_action(self, index):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return self.result()
        return self.result


def load_modals():
    gi = types.ModuleType('gi')
    gi.require_version = mock.Mock()
    repository = types.ModuleType('gi.repository')
    repository.Atspi = mock.Mock()
    repository.GLib = mock.Mock()
    repository.GLib.MainContext.default.return_value.pending.return_value = False
    filename = Path(__file__).resolve().parents[1] / 'clear_modals.py'
    spec = importlib.util.spec_from_file_location('owned_test_clear_modals', filename)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {'gi': gi, 'gi.repository': repository}):
        spec.loader.exec_module(module)
    return module


class ConsentDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.logs = []
        patch = mock.patch.object(self.daemon, 'log', side_effect=self.logs.append)
        patch.start()
        self.addCleanup(patch.stop)
        self.modals = load_modals()

    def scan(self, *nodes):
        self.modals.Atspi.get_desktop.return_value = Node(children=[Node('Google Chrome', children=nodes)])
        self.daemon.begin_consent_attempt(150)
        return self.modals.scan_and_press(self.daemon.begin_consent_scan())

    def events(self, kind=None):
        values = self.daemon.consent_snapshot()['events']
        return [entry for entry in values if kind is None or entry['event'] == kind]

    def test_false_native_result_is_recorded_without_changing_completed_call_count(self):
        button = Node('Allow', 'push button', result=False)
        self.assertEqual(self.scan(button), 1)
        self.assertEqual(button.calls, 1)
        self.assertIs(self.events('native_action_finished')[0]['native_result'], False)
        self.assertEqual(self.events('scan_finished')[0]['completed_calls'], 1)
        self.assertNotIn('pressed', '\n'.join(self.logs))

    def test_true_native_result_is_recorded(self):
        self.assertEqual(self.scan(Node('Allow', 'button')), 1)
        self.assertIs(self.events('native_action_finished')[0]['native_result'], True)

    def test_every_duplicate_and_restore_action_keeps_its_order(self):
        nodes = [Node(name, 'push button') for name in ('Allow', 'Allow', 'Restore')]
        self.assertEqual(self.scan(*nodes), 3)
        self.assertEqual([entry['button'] for entry in self.events('native_action_started')], ['Allow', 'Allow', 'Restore'])
        self.assertEqual([node.calls for node in nodes], [1, 1, 1])

    def test_missing_press_action_does_not_invoke_another_action(self):
        button = Node('Restore', 'push button', action='click')
        self.assertEqual(self.scan(button), 0)
        self.assertEqual(button.calls, 0)
        self.assertEqual(self.events('press_action_unavailable')[0]['button'], 'Restore')

    def test_action_error_does_not_record_exception_message(self):
        self.assertEqual(self.scan(Node('Allow', 'push button', result=RuntimeError('cookie=private-value'))), 0)
        self.assertEqual(self.events('native_action_error')[0]['error_type'], 'RuntimeError')
        self.assertNotIn('private-value', json.dumps(self.events()))

    def test_unrecognized_labels_are_not_recorded(self):
        private = Node('private page title and token', 'push button')
        self.scan(private, Node('Allow', 'push button'))
        self.assertEqual(private.calls, 0)
        self.assertNotIn('private page', json.dumps(self.events()))

    def test_tree_query_failures_are_counted(self):
        broken = Node()
        broken.get_role_name = mock.Mock(side_effect=RuntimeError('private page title'))
        self.scan(broken)
        self.assertEqual(self.events('scan_finished')[0]['query_errors'], 1)
        self.assertNotIn('private page', json.dumps(self.events()))

    def test_diagnostic_callback_failure_does_not_interrupt_native_calls(self):
        button = Node('Allow', 'push button')
        reporter = mock.Mock(side_effect=RuntimeError('reporter failure'))
        self.assertTrue(self.modals.press_button(button, reporter, 'Allow'))
        self.assertEqual(button.calls, 1)

    def test_completed_old_action_keeps_old_attempt_after_rearm(self):
        old_attempt = self.daemon.begin_consent_attempt(150)
        report = self.daemon.begin_consent_scan()
        entered, finish = threading.Event(), threading.Event()
        failures = []

        def native_action():
            entered.set()
            if not finish.wait(5):
                raise AssertionError('Owned action did not get its completion signal')
            return False

        def run():
            try:
                self.modals.press_button(Node(result=native_action), report, 'Allow')
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            newer = self.daemon.begin_consent_attempt(150)
            self.daemon.consent_event('connection_ready', newer)
        finally:
            finish.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        started = self.events('native_action_started')[0]
        ended = self.events('native_action_finished')[0]
        connected = self.events('connection_ready')[0]
        self.assertEqual(started['attempt_id'], old_attempt)
        self.assertEqual(ended['attempt_id'], old_attempt)
        self.assertEqual(connected['attempt_id'], newer)
        self.assertLess(started['seq'], connected['seq'])
        self.assertLess(connected['seq'], ended['seq'])

    def test_scan_summary_keeps_original_attempt_and_scan_identity(self):
        first = self.daemon.begin_consent_attempt(150)
        old = self.daemon.begin_consent_scan()
        second = self.daemon.begin_consent_attempt(150)
        new = self.daemon.begin_consent_scan()
        new('scan_finished', completed_calls=0)
        old('scan_finished', completed_calls=1)
        summaries = self.events('scan_finished')
        self.assertEqual([entry['attempt_id'] for entry in summaries], [second, first])
        self.assertNotEqual(summaries[0]['scan_id'], summaries[1]['scan_id'])
        self.assertTrue(all(entry['duration_ms'] >= 0 for entry in summaries))

    def test_events_have_wall_and_monotonic_timestamps_and_one_log_source(self):
        self.daemon.consent_event('native_action_finished', 7, button='Allow', native_result=False)
        event = self.events()[0]
        self.assertIn('T', event['time_zagreb'])
        self.assertTrue(event['time_zagreb'].endswith('+01:00') or event['time_zagreb'].endswith('+02:00'))
        self.assertGreater(event['monotonic_ns'], 0)
        self.assertEqual(json.loads(self.logs[0].removeprefix('consent: ')), event)

    def test_metadata_allowlist_drops_unselected_values(self):
        self.daemon.consent_event('button_matched', 1, button='private-title',
                                  cookie='private-cookie', endpoint='private-capability')
        data = json.dumps(self.events())
        self.assertNotIn('private', data)
        self.assertEqual(self.events()[0]['button'], 'unknown')

    def test_retention_exposes_evicted_sequence_range(self):
        capacity = self.daemon.consent_events.maxlen
        for unused in range(capacity + 3):
            self.daemon.consent_event('scan_started', 1)
        snapshot = self.daemon.consent_snapshot()
        self.assertEqual(len(snapshot['events']), capacity)
        self.assertEqual(snapshot['dropped_events'], 3)
        self.assertEqual(snapshot['first_seq'], 4)
        self.assertEqual(snapshot['last_seq'], capacity + 3)

    def test_fake_socket_records_send_and_upgrade_order_without_chrome(self):
        sock = mock.Mock()
        sock.recv.return_value = b'HTTP/1.1 101 Switching Protocols\r\n\r\n'
        with mock.patch.object(self.daemon.socket, 'create_connection', return_value=sock):
            with mock.patch.object(self.daemon, '_discover_endpoint', return_value=(12345, '/owned-test')):
                self.assertTrue(self.daemon.connect_once(150))
        kinds = [entry['event'] for entry in self.events()]
        self.assertEqual(kinds, ['attempt_started', 'presser_requested', 'upgrade_request_sent', 'upgrade_finished', 'connection_ready'])
        self.assertEqual(len({entry['attempt_id'] for entry in self.events()}), 1)
        self.assertFalse(self.daemon.press_wanted.is_set())
        self.assertEqual(self.daemon.cdp_state, 'connected')
        self.assertNotIn('/owned-test', json.dumps(self.events()))

    def test_presser_import_failure_records_class_not_exception_text(self):
        original = builtins.__import__

        def importing(name, *args, **kwargs):
            if name == 'clear_modals':
                raise ModuleNotFoundError('private import path')
            return original(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=importing):
            self.daemon.presser_main()
        self.assertEqual(self.events('presser_import_failed')[0]['error_type'], 'ModuleNotFoundError')
        self.assertNotIn('private import path', json.dumps(self.events()))

    def test_presser_keeps_completed_call_sleep_and_skips_health_as_before(self):
        self.daemon.begin_consent_attempt(150)
        self.daemon.press_wanted.set()
        fake = types.SimpleNamespace(scan_and_press=mock.Mock(return_value=1),
                                     chrome_accessibility_health=mock.Mock())
        with mock.patch.dict(sys.modules, {'clear_modals': fake}):
            with mock.patch.object(self.daemon.time, 'sleep', side_effect=StopPresser) as sleep:
                with self.assertRaises(StopPresser):
                    self.daemon.presser_main()
        sleep.assert_called_once_with(0.5)
        fake.chrome_accessibility_health.assert_not_called()
        self.assertEqual(len(self.events('scan_started')), 1)


if __name__ == '__main__':
    unittest.main()
