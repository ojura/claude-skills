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
  clear_modals.py --wait    poll until at least one button is pressed
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


def find_all(node, role_match, names, depth=0, max_depth=30, out=None):
    if out is None: out = []
    if node is None or depth > max_depth: return out
    try:
        if node.get_role_name() in role_match and node.get_name() in names:
            out.append(node)
        for i in range(node.get_child_count()):
            try:
                find_all(node.get_child_at_index(i), role_match, names, depth+1, max_depth, out)
            except Exception:
                pass
    except Exception:
        pass
    return out


def press_button(btn):
    """Invoke 'press' action on an AT-SPI button. Returns True on success."""
    try:
        act = btn.get_action_iface()
        for j in range(act.get_n_actions()):
            if act.get_action_name(j) == 'press':
                act.do_action(j)
                return True
    except Exception:
        pass
    return False


def scan_and_press():
    """Scan AT-SPI desktop for Chrome's Allow/Restore buttons; press all found."""
    _pump_glib()
    desk = Atspi.get_desktop(0)
    pressed = 0
    for i in range(desk.get_child_count()):
        try:
            app = desk.get_child_at_index(i)
            if app and app.get_name() == 'Google Chrome':
                buttons = find_all(app, ('push button', 'button'), BUTTON_NAMES)
                for btn in buttons:
                    if press_button(btn):
                        pressed += 1
        except Exception:
            pass
    return pressed


def chrome_accessibility_health():
    """Returns (found_chrome_app, has_renderer_a11y).

    Two failure modes to distinguish:
    - No Chrome app at AT-SPI desktop at all: Chrome isn't running, or AT-SPI
      bridge is broken.
    - Chrome app present but `Native accessibility API support` is OFF in
      chrome://accessibility: the browser-shell tree (toolbar, tabs, omnibox,
      extension buttons) IS exposed (~hundreds of nodes), but the rendered
      page content and the renderer-owned Allow dialog are NOT. The dialog
      this script is supposed to press lives in that hidden tree, so we must
      reject this state explicitly instead of spinning to deadline.

    Discriminator: renderer-content roles. With toggle ON, the tree exposes
    'document web', 'document frame', 'heading', 'link', etc. With toggle
    OFF, only browser-shell roles ('push button', 'panel', 'notification',
    'page tab', 'tool bar', 'frame') appear."""
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
        print(f'pressed {n}')
        return

    # Wait mode: keep scanning until a modal appears AND we press at least
    # one button (Allow or Restore). Chrome adds the Allow modal asynchronously
    # after the WS upgrade, so a single startup scan misses it; the crash
    # "Restore pages?" prompt is up at relaunch and caught by the same scan.
    # Health check: if after ~2s Chrome's accessibility tree is empty,
    # bail fast with a clear diagnostic. The most common cause is the
    # global Accessibility flag being off in chrome://accessibility;
    # without it, AT-SPI sees a Chrome app node but no descendants, so
    # this scanner would spin to its 60s deadline producing nothing.
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
            print(f'clean (round {rounds}, total {total} pressed)')
            return
        if not health_checked and time.time() > health_deadline:
            health_checked = True
            found, has_renderer = chrome_accessibility_health()
            if not found:
                print('ABORT: no Chrome app found via AT-SPI. Is Chrome running?')
                sys.exit(2)
            if not has_renderer:
                print('ABORT: Chrome browser-shell is in AT-SPI but renderer-side '
                      'accessibility is OFF. Enable "Native accessibility API support" '
                      'at chrome://accessibility (or restart Chrome with '
                      '--force-renderer-accessibility), then retry. The Allow dialog '
                      'lives in the renderer-side tree and is invisible without it.')
                sys.exit(3)
        time.sleep(0.3)
    print(f'timeout (no Allow/Restore button found in {rounds} rounds)')
    sys.exit(1)


if __name__ == '__main__':
    main()
