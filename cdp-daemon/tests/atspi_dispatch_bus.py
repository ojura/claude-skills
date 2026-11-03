#!/usr/bin/env python3
"""Reproduce idle AT-SPI backpressure and test dispatch on owned D-Bus buses.

Run explicitly with system Python on AC power. The fixture starts its own bus
and accessibility registry. It does not connect to a desktop bus or Chrome.
"""
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent


def worker(mode):
    assert os.environ['AT_SPI_BUS_ADDRESS'] == os.environ['DBUS_SESSION_BUS_ADDRESS']
    assert '/run/user/' not in os.environ['AT_SPI_BUS_ADDRESS']
    sys.path.insert(0, str(HERE.parent))
    import clear_modals
    from gi.repository import GLib
    import cdp_daemon as daemon
    daemon.log = lambda message: None
    scanned = threading.Event()
    count = 0
    owner = None
    native_owner = None
    listener = None
    scans = 0
    block_scan = threading.Event()
    entered = threading.Event()
    resume_scan = threading.Event()
    fail_scan = threading.Event()

    def observed(event):
        nonlocal count
        assert threading.get_ident() == owner
        count += 1

    def scan(reporter=None):
        nonlocal owner, native_owner, listener, scans
        owner = threading.get_ident()
        native_owner = threading.get_native_id()
        if listener is None:
            clear_modals.Atspi.get_desktop(0)
            listener = clear_modals.Atspi.EventListener.new(observed)
            assert listener.register('object:state-changed:focused')
        scans += 1
        scanned.set()
        if block_scan.is_set():
            entered.set()
            assert resume_scan.wait(10), 'Owned blocked scan did not receive its release'
            block_scan.clear()
        if fail_scan.is_set():
            fail_scan.clear()
            raise RuntimeError('Owned synthetic scan failure')
        return 0

    clear_modals.scan_and_press = scan
    clear_modals.chrome_accessibility_health = lambda: (False, False)
    thread = None
    if mode == 'dispatch':
        daemon.press_wanted.set()
        thread = threading.Thread(target=daemon.presser_main, daemon=True)
        thread.start()
        assert scanned.wait(5)
        daemon.press_wanted.clear()
    else:
        scan()
        # Reproduce the old presser: pump at scan entry, then park in Python.
        clear_modals._pump_glib()
    print(json.dumps({'ready': True, 'pid': os.getpid(), 'owner_tid': native_owner}), flush=True)
    for line in sys.stdin:
        command = json.loads(line)['command']
        if command == 'arm':
            assert mode == 'dispatch'
            scanned.clear()
            daemon.press_wanted.set()
            assert scanned.wait(5)
        elif command == 'disarm':
            daemon.press_wanted.clear()
        elif command == 'block':
            block_scan.set()
            daemon.press_wanted.set()
            assert entered.wait(5)
        elif command == 'resume':
            daemon.press_wanted.clear()
            resume_scan.set()
        elif command == 'fail':
            fail_scan.set()
            scanned.clear()
            daemon.press_wanted.set()
            assert scanned.wait(5)
            daemon.press_wanted.clear()
        elif command == 'stop':
            if thread is not None:
                daemon.press_wanted.stop()
                thread.join(5)
                assert not thread.is_alive()
                assert daemon.press_wanted.context is None
            else:
                clear_modals.Atspi.exit()
            print(json.dumps({'stopped': True, 'received': count, 'scans': scans,
                              'scan_errors': sum(e['event'] == 'scan_failed' for e in daemon.consent_snapshot()['events'])}), flush=True)
            assert json.loads(sys.stdin.readline())['command'] == 'exit'
            return
        print(json.dumps({'received': count, 'scans': scans}), flush=True)


def ac_online():
    return any((p / 'type').is_file() and (p / 'type').read_text().strip() == 'Mains'
               and (p / 'online').read_text().strip() == '1'
               for p in Path('/sys/class/power_supply').iterdir())


def run_case(mode, root):
    from gi.repository import Gio, GLib
    case = root / mode
    case.mkdir()
    config = case / 'bus.conf'
    config.write_text(f'''<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:tmpdir={case}</listen>
<auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*"/><allow own="*"/><allow eavesdrop="true"/></policy>
<limit name="max_outgoing_bytes">1048576</limit></busconfig>''')
    processes = []
    stopped = threading.Event()
    lost_power = threading.Event()

    def watch_power():
        while not stopped.wait(0.25):
            if not ac_online():
                lost_power.set()

    monitor = threading.Thread(target=watch_power)
    monitor.start()
    with (case / 'bus.log').open('w+') as bus_log, (case / 'worker.log').open('w+') as worker_log:
        try:
            bus = subprocess.Popen(['dbus-daemon', '--nofork', '--print-address=1', '--config-file=' + str(config)],
                                   stdout=subprocess.PIPE, stderr=bus_log, text=True, start_new_session=True)
            processes.append(bus)
            address = bus.stdout.readline().strip()
            assert address.startswith('unix:') and str(case) in address
            flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
            connection = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
            environment = {'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8', 'HOME': str(case),
                           'AT_SPI_BUS_ADDRESS': address, 'DBUS_SESSION_BUS_ADDRESS': address,
                           'GSETTINGS_BACKEND': 'memory'}
            registry = subprocess.Popen(['/usr/libexec/at-spi2-registryd'], env=environment,
                                        stdout=bus_log, stderr=bus_log, start_new_session=True)
            processes.append(registry)
            deadline = time.monotonic() + 5
            while not connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                    'org.freedesktop.DBus', 'NameHasOwner', GLib.Variant('(s)', ('org.a11y.atspi.Registry',)),
                    None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]:
                assert registry.poll() is None, 'Owned registry exited before registration'
                assert time.monotonic() < deadline, 'Owned registry did not register'
                time.sleep(0.01)
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', mode],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=worker_log,
                                     env=environment, text=True, start_new_session=True)
            processes.append(child)
            def response():
                assert select.select([child.stdout], [], [], 30)[0], 'Owned worker did not reply within 30 seconds'
                line = child.stdout.readline()
                assert line, 'Owned worker exited before replying'
                return json.loads(line)

            ready = response()
            assert ready['ready']
            owner_tid = ready['owner_tid']

            def call(method, parameters=None, interface='org.freedesktop.DBus'):
                return connection.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', interface,
                                            method, parameters, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]

            names = call('ListNames')
            owned = [name for name in names if name.startswith(':') and
                     call('GetConnectionUnixProcessID', GLib.Variant('(s)', (name,))) == child.pid]
            assert len(owned) == 1
            name = owned[0]

            def stats():
                return call('GetConnectionStats', GLib.Variant('(s)', (name,)),
                            'org.freedesktop.DBus.Debug.Stats')

            def command(value):
                child.stdin.write(json.dumps({'command': value}) + '\n')
                child.stdin.flush()
                return response()

            def sample_cpu():
                ticks = Path(f'/proc/{child.pid}/stat').read_text().split()
                status = Path(f'/proc/{child.pid}/task/{owner_tid}/status').read_text().splitlines()
                switches = {s.split(':')[0]: int(s.split(':')[1]) for s in status
                            if 'ctxt_switches:' in s}
                return sum(int(ticks[i]) for i in (13, 14)), sum(switches.values())

            first_cpu = sample_cpu()
            time.sleep(1)
            next_cpu = sample_cpu()
            idle = {'cpu_seconds': (next_cpu[0] - first_cpu[0]) / os.sysconf('SC_CLK_TCK'),
                    'context_switches': next_cpu[1] - first_cpu[1]}
            phases = []
            peak = stats()['OutgoingBytes']
            for phase in ('idle', 'acquisition', 'rearmed_idle'):
                if phase == 'rearmed_idle' and mode == 'dispatch':
                    command('disarm')
                before = command('status')
                if phase == 'acquisition' and mode == 'dispatch':
                    assert command('arm')['scans'] > before['scans']
                phase_started = time.monotonic()
                for batch in range(100):
                    assert ac_online() and not lost_power.is_set()
                    for index in range(100):
                        connection.emit_signal(None, '/org/a11y/atspi/accessible/fixture',
                            'org.a11y.atspi.Event.Object', 'StateChanged',
                            GLib.Variant('(siiva{sv})', ('focused', 1, 0, GLib.Variant('s', 'x' * 1024), {})))
                    connection.flush_sync(None)
                    time.sleep(0.01)
                    peak = max(peak, stats()['OutgoingBytes'])
                deadline = time.monotonic() + 10
                while mode == 'dispatch':
                    value = command('status')
                    if value['received'] >= before['received'] + 10000:
                        break
                    assert time.monotonic() < deadline, 'Owned signals did not finish dispatching'
                    time.sleep(0.01)
                phases.append({'phase': phase, 'duration_seconds': time.monotonic() - phase_started,
                               'stats': stats(), 'worker': command('status')})
            backpressure = None
            if mode == 'dispatch':
                assert 'full message queue' not in (case / 'bus.log').read_text()
                before = command('status')['received']
                command('block')
                for index in range(2000):
                    connection.emit_signal(None, '/org/a11y/atspi/accessible/fixture',
                        'org.a11y.atspi.Event.Object', 'StateChanged',
                        GLib.Variant('(siiva{sv})', ('focused', 1, 0, GLib.Variant('s', 'x' * 1024), {})))
                connection.flush_sync(None)
                full = stats()
                assert full['OutgoingBytes'] >= 1048576
                command('resume')
                deadline = time.monotonic() + 10
                while stats()['OutgoingBytes']:
                    assert time.monotonic() < deadline, 'Owned full queue did not drain after scan release'
                    time.sleep(0.01)
                rejected_blocked = (case / 'bus.log').read_text().count('full message queue')
                assert rejected_blocked > 0
                deadline = time.monotonic() + 10
                while command('status')['received'] < before + 2000 - rejected_blocked:
                    assert time.monotonic() < deadline, 'Accepted signals did not finish dispatching'
                    time.sleep(0.01)
                command('arm')
                command('disarm')
                command('fail')
                command('arm')
                command('disarm')
                recovered = command('status')['received']
                for index in range(1000):
                    connection.emit_signal(None, '/org/a11y/atspi/accessible/fixture',
                        'org.a11y.atspi.Event.Object', 'StateChanged',
                        GLib.Variant('(siiva{sv})', ('focused', 1, 0, GLib.Variant('s', 'x' * 1024), {})))
                connection.flush_sync(None)
                deadline = time.monotonic() + 10
                while command('status')['received'] < recovered + 1000:
                    assert time.monotonic() < deadline, 'Signals after recovery did not dispatch'
                    time.sleep(0.01)
                backpressure = {'blocked_queue': full, 'rejected_while_blocked': rejected_blocked,
                                'received_before': before,
                                'received_after_drain': recovered, 'after_rearm': command('status'),
                                'remaining_outgoing_bytes': stats()['OutgoingBytes']}
                assert backpressure['remaining_outgoing_bytes'] == 0
            final = command('stop')
            assert child.poll() is None
            assert name not in call('ListNames'), 'Atspi.exit did not close the live process connection'
            child.stdin.write(json.dumps({'command': 'exit'}) + '\n')
            child.stdin.flush()
            child.wait(5)
            assert child.returncode == 0
            connection.close_sync(None)
            rejected = (case / 'bus.log').read_text().count('full message queue')
            report = {'mode': mode, 'idle': idle, 'peak_sampled_outgoing_bytes': peak,
                      'phases': phases, 'backpressure': backpressure, 'final': final,
                      'queue_rejections': rejected, 'atspi_exit_disconnected_before_process_exit': True}
            if mode == 'dispatch':
                assert final['received'] == 33000 - rejected
                assert final['scan_errors'] == 1
                assert all(p['stats']['OutgoingBytes'] == 0 for p in phases)
                assert rejected == backpressure['rejected_while_blocked']
                assert idle['cpu_seconds'] < 0.05
            else:
                assert final['received'] < 30000 and rejected > 0
                assert phases[-1]['stats']['OutgoingBytes'] >= 1048576
            return report
        finally:
            stopped.set()
            monitor.join()
            for process in reversed(processes):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(10)
            assert not lost_power.is_set(), 'AC was lost; this run requires re-execution'


def main():
    assert ac_online(), 'This explicit bus load test requires AC power'
    with tempfile.TemporaryDirectory(prefix='cdp-atspi-bus.', dir='/tmp') as directory:
        root = Path(directory)
        report = [run_case(mode, root) for mode in ('parked', 'dispatch')]
        print(json.dumps({'outcome': 'passed', 'cases': report}, indent=2))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        worker(sys.argv[2])
    else:
        main()
