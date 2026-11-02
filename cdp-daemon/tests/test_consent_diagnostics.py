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
        prompts = [Node('Restore pages?' if node.name == 'Restore' else 'Allow remote debugging?',
                        'alert' if node.name == 'Restore' else 'dialog', children=[node])
                   for node in nodes]
        self.modals.Atspi.get_desktop.return_value = Node(children=[Node('Google Chrome', children=prompts)])
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

    def test_writer_failure_keeps_valid_upgrade_open_and_retains_diagnostics(self):
        sock = mock.Mock()
        sock.recv.return_value = b'HTTP/1.1 101 Switching Protocols\r\n\r\n'
        with mock.patch.object(self.daemon.socket, 'create_connection', return_value=sock):
            with mock.patch.object(self.daemon, 'log', side_effect=OSError('private writer detail')):
                result = self.daemon.ws_upgrade(150)
        self.assertEqual(result[0], 'ok')
        sock.recv.assert_called_once()
        sock.close.assert_not_called()
        snapshot = self.daemon.consent_snapshot()
        self.assertEqual(snapshot['text_log_failures'], 1)
        self.assertEqual(snapshot['last_text_log_error_type'], 'OSError')
        self.assertEqual(snapshot['events'][0]['event'], 'upgrade_request_sent')
        self.assertNotIn('private writer detail', json.dumps(snapshot))

    def test_writer_failure_does_not_interrupt_full_successful_acquisition(self):
        sock = mock.Mock()
        sock.recv.return_value = b'HTTP/1.1 101 Switching Protocols\r\n\r\n'
        with mock.patch.object(self.daemon.socket, 'create_connection', return_value=sock):
            with mock.patch.object(self.daemon, 'log', side_effect=OSError('owned writer failure')):
                self.assertTrue(self.daemon.connect_once(150))
        self.assertIs(self.daemon.cdp_sock, sock)
        self.assertEqual(self.daemon.cdp_state, 'connected')
        sock.close.assert_not_called()
        self.assertEqual(self.daemon.consent_snapshot()['text_log_failures'], 5)

    def test_application_queries_increment_scan_error_count(self):
        for operation in ('get_name', 'get_child_at_index', 'get_child_count'):
            with self.subTest(operation=operation):
                application = Node('Google Chrome')
                desktop = Node(children=[application])
                owner = application if operation == 'get_name' else desktop
                if operation == 'get_child_count':
                    owner = application
                setattr(owner, operation, mock.Mock(side_effect=RuntimeError('private query text')))
                self.modals.Atspi.get_desktop.return_value = desktop
                self.modals.scan_and_press(self.daemon.begin_consent_scan())
                self.assertEqual(self.events('scan_finished')[-1]['query_errors'], 1)
        self.assertNotIn('private query text', json.dumps(self.events()))

    def test_button_label_query_error_is_counted_without_guessing_its_action(self):
        button = Node('Allow', 'push button')
        button.get_name = mock.Mock(side_effect=RuntimeError('private label'))
        self.assertEqual(self.scan(button), 0)
        self.assertEqual(button.calls, 0)
        self.assertEqual(self.events('scan_finished')[0]['query_errors'], 1)
        self.assertEqual(self.events('button_matched'), [])
        self.assertNotIn('private label', json.dumps(self.events()))

    def test_action_interface_queries_are_counted_separately_from_native_errors(self):
        for operation in ('get_action_iface', 'get_n_actions', 'get_action_name'):
            with self.subTest(operation=operation):
                button = Node('Allow', 'push button')
                setattr(button, operation, mock.Mock(side_effect=RuntimeError('private action query')))
                self.assertEqual(self.scan(button), 0)
                self.assertEqual(button.calls, 0)
                self.assertEqual(self.events('scan_finished')[-1]['query_errors'], 1)
        self.assertEqual(self.events('native_action_error'), [])
        self.assertNotIn('private action query', json.dumps(self.events()))

    def test_health_start_is_visible_while_blocked_and_completion_keeps_old_identity(self):
        old_attempt = self.daemon.begin_consent_attempt(150)
        self.daemon.press_wanted.set()
        entered, finish = threading.Event(), threading.Event()
        clock = [1_000_000_000]
        failures = []

        def scan(reporter):
            reporter('scan_finished', completed_calls=0, matched_buttons=0, query_errors=0)
            return 0

        def health():
            entered.set()
            if not finish.wait(5):
                raise AssertionError('Owned health call did not get its completion signal')
            clock[0] += 5_000_000_000
            return True, False

        def run():
            try:
                self.daemon.presser_main()
            except StopPresser:
                pass
            except BaseException as error:
                failures.append(error)

        fake = types.SimpleNamespace(scan_and_press=scan, chrome_accessibility_health=health)
        with mock.patch.dict(sys.modules, {'clear_modals': fake}):
            with mock.patch.object(self.daemon.time, 'sleep', side_effect=StopPresser):
                with mock.patch.object(self.daemon.time, 'monotonic_ns', side_effect=lambda: clock[0]):
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        during = self.events()
                        self.assertEqual(during[-1]['event'], 'accessibility_health_started')
                        self.assertEqual(self.events('accessibility_health_finished'), [])
                        self.daemon.begin_consent_attempt(150)
                    finally:
                        finish.set()
                        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        start = self.events('accessibility_health_started')[0]
        end = self.events('accessibility_health_finished')[0]
        self.assertEqual(start['attempt_id'], old_attempt)
        self.assertEqual(end['attempt_id'], old_attempt)
        self.assertEqual(start['scan_id'], end['scan_id'])
        self.assertEqual(end['duration_ms'], 5000)

    def test_every_unchanged_health_observation_is_recorded_without_cadence_change(self):
        self.daemon.begin_consent_attempt(150)
        self.daemon.press_wanted.set()
        scans = []

        def scan(reporter):
            scans.append(True)
            reporter('scan_finished', completed_calls=0, matched_buttons=0, query_errors=0)
            return 0

        def sleep(seconds):
            self.assertEqual(seconds, 0.5)
            if len(scans) == 11:
                raise StopPresser

        health = mock.Mock(return_value=(True, False))
        fake = types.SimpleNamespace(scan_and_press=scan, chrome_accessibility_health=health)
        with mock.patch.dict(sys.modules, {'clear_modals': fake}):
            with mock.patch.object(self.daemon.time, 'sleep', side_effect=sleep):
                with self.assertRaises(StopPresser):
                    self.daemon.presser_main()
        self.assertEqual(len(scans), 11)
        self.assertEqual(health.call_count, 2)
        self.assertEqual(len(self.events('accessibility_health_started')), 2)
        self.assertEqual(len(self.events('accessibility_health_finished')), 2)
        self.assertTrue(all(entry['duration_ms'] >= 0 for entry in self.events('accessibility_health_finished')))

    def test_health_failure_is_timed_and_distinguished_from_scan_failure(self):
        self.daemon.begin_consent_attempt(150)
        self.daemon.press_wanted.set()

        def scan(reporter):
            reporter('scan_finished', completed_calls=0, matched_buttons=0, query_errors=0)
            return 0

        fake = types.SimpleNamespace(scan_and_press=scan,
            chrome_accessibility_health=mock.Mock(side_effect=RuntimeError('private health failure')))
        with mock.patch.dict(sys.modules, {'clear_modals': fake}):
            with mock.patch.object(self.daemon.time, 'sleep', side_effect=StopPresser) as sleep:
                with self.assertRaises(StopPresser):
                    self.daemon.presser_main()
        sleep.assert_called_once_with(0.5)
        failure = self.events('accessibility_health_failed')[0]
        self.assertEqual(failure['error_type'], 'RuntimeError')
        self.assertGreaterEqual(failure['duration_ms'], 0)
        self.assertEqual(self.events('scan_failed'), [])
        self.assertNotIn('private health failure', json.dumps(self.events()))

    def scan_native_tree(self, *children):
        self.modals.Atspi.get_desktop.return_value = Node(children=[Node('Google Chrome', children=children)])
        self.daemon.begin_consent_attempt(150)
        return self.modals.scan_and_press(self.daemon.begin_consent_scan())

    def test_maximized_window_restore_is_excluded_but_both_intended_prompts_work(self):
        window_restore = Node('Restore', 'push button')
        consent_buttons = [Node('Allow', 'push button'), Node('Allow', 'push button')]
        recovery_restore = Node('Restore', 'push button')
        frame = Node('Owned maximized window', 'frame', children=[
            Node('Window controls', children=[window_restore]),
            Node('Allow remote debugging?', 'dialog', children=consent_buttons),
            Node('Restore pages?', 'alert', children=[recovery_restore]),
        ])
        self.assertEqual(self.scan_native_tree(frame), 3)
        self.assertEqual(window_restore.calls, 0)
        self.assertEqual([button.calls for button in consent_buttons], [1, 1])
        self.assertEqual(recovery_restore.calls, 1)
        actions = self.events('native_action_finished')
        self.assertEqual([(event['button'], event['prompt_kind']) for event in actions],
                         [('Allow', 'consent'), ('Allow', 'consent'), ('Restore', 'crash_recovery')])
        self.assertEqual(self.events('scan_finished')[0]['excluded_buttons'], 1)

    def test_web_document_cannot_imitate_native_prompt_titles_and_buttons(self):
        allow, restore = Node('Allow', 'button'), Node('Restore', 'button')
        imitations = Node('Owned web page', 'document web', children=[
            Node('Allow remote debugging?', 'dialog', children=[allow]),
            Node('Restore pages?', 'alert', children=[restore]),
        ])
        self.assertEqual(self.scan_native_tree(Node(role='frame', children=[imitations])), 0)
        self.assertEqual((allow.calls, restore.calls), (0, 0))
        self.assertEqual(self.events('scan_finished')[0]['document_subtrees_skipped'], 1)
        self.assertEqual(self.events('button_matched'), [])

    def test_document_subtree_is_skipped_before_reading_its_content(self):
        content = Node('private page content')
        content.get_name = mock.Mock(side_effect=AssertionError('Web content was inspected'))
        for role in sorted(self.modals.DOCUMENT_ROLES):
            with self.subTest(role=role):
                document = Node(role=role, children=[content])
                document.get_child_count = mock.Mock(side_effect=AssertionError('Document traversal occurred'))
                self.assertEqual(self.scan_native_tree(document), 0)
                content.get_name.assert_not_called()
                document.get_child_count.assert_not_called()

    def test_unknown_native_prompt_is_observable_without_global_fallback(self):
        button = Node('Allow', 'button')
        self.assertEqual(self.scan_native_tree(Node('private dialog title', 'dialog', children=[button])), 0)
        self.assertEqual(button.calls, 0)
        self.assertEqual(self.events('scan_finished')[0]['unsupported_prompts'], 1)
        self.assertEqual(self.events('prompt_structure_unavailable')[0]['reason'], 'unrecognized_prompt')
        self.assertNotIn('private dialog title', json.dumps(self.events()))

    def test_known_prompt_nested_under_unrecognized_dialog_stays_excluded(self):
        button = Node('Allow', 'button')
        nested = Node('private unrelated dialog', 'dialog', children=[
            Node('Allow remote debugging?', 'alert', children=[button])])
        self.assertEqual(self.scan_native_tree(nested), 0)
        self.assertEqual(button.calls, 0)
        reasons = [event['reason'] for event in self.events('prompt_structure_unavailable')]
        self.assertEqual(reasons, ['unrecognized_prompt', 'unsupported_ancestor'])
        self.assertNotIn('private unrelated dialog', json.dumps(self.events()))

    def test_title_without_native_dialog_role_does_not_authorize_button(self):
        button = Node('Allow', 'button')
        self.assertEqual(self.scan_native_tree(Node('Allow remote debugging?', 'panel', children=[button])), 0)
        self.assertEqual(button.calls, 0)
        self.assertEqual(self.events('button_rejected')[0]['reason'], 'outside_intended_prompt')

    def test_conflicting_nested_prompt_context_is_observable_and_not_invoked(self):
        button = Node('Restore', 'button')
        nested = Node('Allow remote debugging?', 'dialog', children=[
            Node('Restore pages?', 'alert', children=[button])])
        self.assertEqual(self.scan_native_tree(nested), 0)
        self.assertEqual(button.calls, 0)
        self.assertEqual(self.events('prompt_structure_unavailable')[0]['reason'], 'conflicting_prompts')

    def test_native_window_nested_under_prompt_does_not_inherit_button_permission(self):
        button = Node('Restore', 'button')
        prompt = Node('Restore pages?', 'alert', children=[
            Node('Owned window', 'frame', children=[button])])
        self.assertEqual(self.scan_native_tree(prompt), 0)
        self.assertEqual(button.calls, 0)
        self.assertEqual(self.events('prompt_structure_unavailable')[0]['reason'], 'nested_window')

    def test_nested_same_prompt_keeps_distinct_buttons_and_visits_each_once(self):
        buttons = [Node('Allow', 'button'), Node('Allow', 'push button')]
        prompt = Node('Allow remote debugging?', 'dialog', children=[
            Node('Allow remote debugging?', 'alert', children=buttons)])
        self.assertEqual(self.scan_native_tree(prompt), 2)
        self.assertEqual([button.calls for button in buttons], [1, 1])

    def test_depth_limit_is_reported_without_scanning_unbounded_structure(self):
        node = Node('Allow', 'button')
        for unused in range(33):
            node = Node(children=[node])
        self.assertEqual(self.scan_native_tree(node), 0)
        self.assertGreater(self.events('scan_finished')[0]['depth_limited'], 0)
        self.assertEqual(self.events('prompt_structure_unavailable')[0]['reason'], 'depth_limit')


if __name__ == '__main__':
    unittest.main()
