#!/usr/bin/env python3
"""Press Chrome's "Allow remote debugging?" and crash-recovery "Restore"
button(s) via AT-SPI.

Uses Chrome's accessibility tree (enabled by --force-renderer-accessibility)
to find the buttons without focus stealing or keyboard injection. Two prompts
are handled (see BUTTON_NAMES):
  - "Allow remote debugging?" -> "Allow"   (pops on every fresh CDP WebSocket)
  - "Restore pages?"          -> "Restore" (pops after a Chrome crash; pressing
                                 it recovers the prior session/tabs on relaunch)

Bug to avoid: Chrome exposes the Allow button as TWO push-button nodes under
the same `alert: 'Allow remote debugging?'` parent. find_all() and press
EVERY match. Pressing only the first is unreliable.

Usage:
  clear_modals.py           single-shot scan (exits 0 immediately)
  clear_modals.py --wait    poll until a native button action call completes
                            (or 60s deadline)
The daemon no longer subprocesses this; it imports scan_and_press /
chrome_accessibility_health directly (in-process presser thread). The CLI
remains for manual/debug use.
"""
import sys, time
import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, GLib


def _pump_glib(seconds=0.2):
    """Drain pending GLib main-context events so libatspi processes its
    D-Bus cache/registry signals (app register/deregister, children-changed).

    The daemon's presser thread only polls scan_and_press() on a sleep loop
    and never runs a GLib main loop, so without this the thread's AT-SPI cache
    goes stale: after Chrome relaunches, the cached desktop still points at the
    dead Chrome app object and no live Allow dialog is ever found. Pumping the
    thread's default context (Atspi is implicitly init'd on this thread by the
    first get_desktop call) keeps the desktop view current. Bounded by
    `seconds` so a signal storm can't wedge the scan."""
    ctx = GLib.MainContext.default()
    end = time.time() + seconds
    while time.time() < end and ctx.pending():
        ctx.iteration(False)

# Modal buttons we press. "Allow" clears the remote-debugging consent dialog
# that pops on every fresh CDP WebSocket; "Restore" clears Chrome's crash
# "Restore pages?" prompt so a relaunch recovers the prior session/tabs.
BUTTON_NAMES = ('Allow', 'Restore')


def find_all(node, role_match, names, depth=0, max_depth=30, out=None, query_errors=None):
    if out is None: out = []
    if node is None or depth > max_depth: return out
    try:
        if node.get_role_name() in role_match and node.get_name() in names:
            out.append(node)
        for i in range(node.get_child_count()):
            try:
                find_all(node.get_child_at_index(i), role_match, names,
                         depth+1, max_depth, out, query_errors)
            except Exception:
                if query_errors is not None:
                    query_errors[0] += 1
    except Exception:
        if query_errors is not None:
            query_errors[0] += 1
    return out


def _report(reporter, event, **metadata):
    """Keep optional diagnostic failures from interrupting native actions."""
    if reporter is not None:
        try:
            reporter(event, **metadata)
        except Exception:
            pass


def press_button(btn, reporter=None, button='unknown'):
    """Invoke 'press'; return whether a native action call completed.

    Preserve the existing completed-call count. The diagnostic separately records
    Chrome's boolean result, which can be false even when the call completes.
    A completed call or accepted action does not establish that consent was granted.
    """
    action_started = False
    try:
        act = btn.get_action_iface()
        for j in range(act.get_n_actions()):
            if act.get_action_name(j) == 'press':
                _report(reporter, 'native_action_started', button=button)
                action_started = True
                result = act.do_action(j)
                _report(reporter, 'native_action_finished', button=button,
                        native_result=result if isinstance(result, bool) else None)
                return True
        _report(reporter, 'press_action_unavailable', button=button)
    except Exception as error:
        event = 'native_action_error' if action_started else 'accessibility_query_error'
        _report(reporter, event, button=button, error_type=type(error).__name__)
    return False


def scan_and_press(reporter=None):
    """Invoke Chrome's matching Allow/Restore actions; count completed calls.

    Optional reports contain recognized button names and query/action outcomes,
    not accessibility labels from other controls or page content.
    """
    _pump_glib()
    desk = Atspi.get_desktop(0)
    completed = 0
    applications = 0
    matched = 0
    query_errors = [0]

    def report(event, **metadata):
        if event == 'accessibility_query_error':
            query_errors[0] += 1
        _report(reporter, event, **metadata)

    for i in range(desk.get_child_count()):
        try:
            app = desk.get_child_at_index(i)
            if app and app.get_name() == 'Google Chrome':
                applications += 1
                buttons = find_all(app, ('push button', 'button'), BUTTON_NAMES,
                                   query_errors=query_errors)
                matched += len(buttons)
                for btn in buttons:
                    try:
                        name = btn.get_name()
                    except Exception as error:
                        report('accessibility_query_error', error_type=type(error).__name__)
                        name = None
                    button = name if name in BUTTON_NAMES else 'unknown'
                    report('button_matched', button=button)
                    if press_button(btn, report, button):
                        completed += 1
        except Exception as error:
            report('accessibility_query_error', error_type=type(error).__name__)
    report('scan_finished', chrome_applications=applications,
            matched_buttons=matched, completed_calls=completed,
            query_errors=query_errors[0])
    return completed


def chrome_accessibility_health():
    """Return whether Chrome and renderer-content roles are visible to AT-SPI.

    Native accessibility can expose browser controls while renderer content,
    including the consent dialog, remains unavailable. Missing renderer roles
    can indicate disabled native accessibility or an incomplete current tree;
    this observation does not establish the setting's value. The daemon logs
    the observation and continues scanning.
    """
    RENDERER_ROLES = {'document web', 'document frame', 'document',
                      'web view', 'heading', 'paragraph', 'link',
                      'text container'}
    _pump_glib()
    desk = Atspi.get_desktop(0)
    found = False
    has_renderer = False
    def has_renderer_node(node, depth=0, max_depth=12):
        if node is None or depth > max_depth: return False
        try:
            if (node.get_role_name() or '') in RENDERER_ROLES: return True
            for i in range(node.get_child_count()):
                try:
                    if has_renderer_node(node.get_child_at_index(i), depth+1, max_depth): return True
                except Exception: pass
        except Exception: pass
        return False
    for i in range(desk.get_child_count()):
        try:
            app = desk.get_child_at_index(i)
            if app and app.get_name() == 'Google Chrome':
                found = True
                if has_renderer_node(app): has_renderer = True
        except Exception:
            pass
    return found, has_renderer


def main():
    wait_mode = '--wait' in sys.argv
    if not wait_mode:
        n = scan_and_press()
        print(f'completed {n} native action call(s)')
        return

    # Wait until a matching native action call completes. Chrome adds the Allow
    # dialog asynchronously after the upgrade request, so one startup scan can
    # miss it. Check accessibility after two seconds to report whether Chrome
    # and renderer-content roles are visible during this standalone invocation.
    deadline = time.time() + 60
    health_deadline = time.time() + 2
    health_checked = False
    total = 0
    rounds = 0
    while time.time() < deadline:
        rounds += 1
        n = scan_and_press()
        total += n
        if total > 0:
            # Press one more round to catch any straggler dialogs that
            # might appear after the first press completes.
            time.sleep(0.5)
            extra = scan_and_press()
            total += extra
            print(f'completed {total} native action call(s) in {rounds} round(s)')
            return
        if not health_checked and time.time() > health_deadline:
            health_checked = True
            found, has_renderer = chrome_accessibility_health()
            if not found:
                print('ABORT: AT-SPI did not report Chrome. Check that Chrome is running.')
                sys.exit(2)
            if not has_renderer:
                print('ABORT: AT-SPI exposes Chrome controls but did not expose '
                      'renderer content. Check "Native accessibility API support" '
                      'at chrome://accessibility, or approve the debugging request manually.')
                sys.exit(3)
        time.sleep(0.3)
    print(f'timeout (no Allow/Restore button found in {rounds} rounds)')
    sys.exit(1)


if __name__ == '__main__':
    main()
