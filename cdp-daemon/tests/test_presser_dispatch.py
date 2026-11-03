"""Test GLib dispatch and presser ownership without using the desktop bus."""
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    from gi.repository import GLib
except ImportError:
    GLib = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_page_preparation import load_daemon


@unittest.skipIf(GLib is None, 'GLib dispatch tests require system Python with python3-gi')
class PresserDispatchTests(unittest.TestCase):
    def setUp(self):
        self.daemon = load_daemon()
        self.daemon.log = mock.Mock()
        self.scanned = threading.Event()
        self.threads = []
        self.sources = []
        self.fake = types.SimpleNamespace(
            GLib=GLib, Atspi=types.SimpleNamespace(exit=self.exited),
            scan_and_press=self.scan, chrome_accessibility_health=self.health)
        self.exit_threads = []
        self.scan_threads = []
        self.health_threads = []
        self.raise_scan = False
        self.result = 0
        self.patch = mock.patch.dict(sys.modules, {'clear_modals': self.fake})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.cleanup)

    def scan(self, reporter):
        self.scan_threads.append(threading.get_ident())
        self.scanned.set()
        if self.raise_scan:
            raise RuntimeError('private query error')
        reporter('scan_finished', completed_calls=self.result)
        return self.result

    def health(self):
        self.health_threads.append(threading.get_ident())
        return True, False

    def exited(self):
        self.exit_threads.append(threading.get_ident())

    def start(self):
        thread = threading.Thread(target=self.daemon.presser_main)
        self.threads.append(thread)
        thread.start()
        ready = threading.Event()
        self.source(lambda: ready.set())
        self.assertTrue(ready.wait(3), 'Presser did not begin dispatching GLib')
        return thread

    def source(self, callback):
        def once(*unused):
            callback()
            return False
        source = GLib.idle_source_new()
        source.set_callback(once)
        source.attach(GLib.MainContext.default())
        self.sources.append(source)
        return source

    def cleanup(self):
        self.daemon.press_wanted.stop()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'Presser did not stop')
        for source in self.sources:
            source.destroy()

    def test_idle_dispatches_events_without_scanning_then_wakes_for_acquisition(self):
        thread = self.start()
        self.assertEqual(self.scan_threads, [])
        self.daemon.begin_consent_attempt(150)
        self.daemon.press_wanted.set()
        self.assertTrue(self.scanned.wait(3))
        self.daemon.press_wanted.clear()
        self.daemon.press_wanted.stop()
        thread.join(3)
        self.assertEqual(set(self.scan_threads + self.health_threads + self.exit_threads), {thread.ident})
        self.assertEqual(self.exit_threads, [thread.ident])
        self.assertIsNone(self.daemon.press_wanted.context)

    def test_request_before_context_binding_is_not_lost(self):
        self.daemon.press_wanted.set()
        self.start()
        self.assertTrue(self.scanned.wait(3))

    def test_stop_before_start_does_not_scan_and_releases_context(self):
        self.daemon.press_wanted.stop()
        self.daemon.presser_main()
        self.assertEqual(self.scan_threads, [])
        self.assertEqual(self.exit_threads, [threading.get_ident()])
        self.assertTrue(GLib.MainContext.default().acquire())
        GLib.MainContext.default().release()

    def test_stop_wakes_idle_blocking_poll(self):
        thread = self.start()
        self.daemon.press_wanted.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.scan_threads, [])
        self.assertEqual(self.exit_threads, [thread.ident])

    def test_disarm_cancels_scan_timer_and_rearm_scans_immediately(self):
        self.start()
        self.daemon.press_wanted.set()
        self.assertTrue(self.scanned.wait(3))
        self.daemon.press_wanted.clear()
        settled = threading.Event()
        self.source(settled.set)
        self.assertTrue(settled.wait(3))
        count = len(self.scan_threads)
        self.scanned.clear()
        time.sleep(0.6)
        self.assertEqual(len(self.scan_threads), count)
        self.daemon.press_wanted.set()
        self.assertTrue(self.scanned.wait(3))

    def test_scan_error_keeps_dispatch_and_next_acquisition_available(self):
        self.raise_scan = True
        self.start()
        self.daemon.press_wanted.set()
        self.assertTrue(self.scanned.wait(3))
        self.daemon.press_wanted.clear()
        settled = threading.Event()
        self.source(settled.set)
        self.assertTrue(settled.wait(3))
        self.raise_scan = False
        self.scanned.clear()
        self.daemon.press_wanted.set()
        self.assertTrue(self.scanned.wait(3))
        events = self.daemon.consent_snapshot()['events']
        self.assertTrue(any(e['event'] == 'scan_failed' for e in events))
        self.assertNotIn('private query error', str(events))

    def test_signal_burst_does_not_prevent_active_scan_or_shutdown(self):
        self.start()
        dispatched = []
        source = GLib.idle_source_new()
        source.set_priority(GLib.PRIORITY_DEFAULT)

        def burst(*unused):
            dispatched.append(threading.get_ident())
            return True

        source.set_callback(burst)
        source.attach(GLib.MainContext.default())
        self.sources.append(source)
        self.daemon.press_wanted.set()
        self.assertTrue(self.scanned.wait(3))
        self.daemon.press_wanted.stop()
        self.threads[0].join(3)
        self.assertFalse(self.threads[0].is_alive())
        self.assertTrue(dispatched)
        self.assertEqual(set(dispatched), {self.threads[0].ident})

    def test_active_interval_source_is_destroyed_on_disarm_and_stop(self):
        timers = []
        created = threading.Event()
        original = GLib.timeout_source_new

        def timer(milliseconds):
            self.assertEqual(milliseconds, 500)
            source = original(milliseconds)
            timers.append(source)
            created.set()
            return source

        with mock.patch.object(GLib, 'timeout_source_new', side_effect=timer):
            self.start()
            self.assertEqual(timers, [])
            self.daemon.press_wanted.set()
            self.assertTrue(created.wait(3))
            self.daemon.press_wanted.clear()
            idle = threading.Event()
            self.source(idle.set)
            self.assertTrue(idle.wait(3))
            self.assertTrue(all(source.is_destroyed() for source in timers))
            created.clear()
            self.daemon.press_wanted.set()
            self.assertTrue(created.wait(3))
            self.daemon.press_wanted.stop()
            self.threads[0].join(3)
            self.assertTrue(all(source.is_destroyed() for source in timers))

    def test_context_owned_by_another_thread_prevents_atspi_calls(self):
        fake_context = mock.Mock()
        fake_context.acquire.return_value = False
        self.fake.GLib = types.SimpleNamespace(MainContext=types.SimpleNamespace(default=lambda: fake_context))
        self.daemon.presser_main()
        self.assertEqual(self.scan_threads, [])
        self.assertEqual(self.exit_threads, [])
        self.assertEqual(self.daemon.consent_snapshot()['events'][-1]['event'], 'presser_dispatch_failed')
        fake_context.release.assert_not_called()

    def test_dispatch_error_releases_atspi_and_context(self):
        self.daemon.press_wanted.dispatch = mock.Mock(side_effect=RuntimeError('private dispatch error'))
        self.daemon.presser_main()
        events = self.daemon.consent_snapshot()['events']
        self.assertEqual(events[-1]['event'], 'presser_dispatch_failed')
        self.assertEqual(events[-1]['error_type'], 'RuntimeError')
        self.assertEqual(self.exit_threads, [threading.get_ident()])
        self.assertIsNone(self.daemon.press_wanted.context)


if __name__ == '__main__':
    unittest.main()
