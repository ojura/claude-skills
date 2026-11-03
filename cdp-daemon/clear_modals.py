#!/usr/bin/env python3
"""Press Chrome's "Allow remote debugging?" and crash-recovery "Restore"
button(s) via AT-SPI.

Uses Chrome's native accessibility tree to find recognized alert/dialog
containers and invoke their intended buttons. It skips renderer documents.
  - "Allow remote debugging?" -> "Allow" on consent-controlled CDP connections.
  - "Restore pages?" -> "Restore" after a Chrome crash.

Chrome exposes distinct Allow nodes for the same prompt; retain their traversal
order and attempt each intended control. A window's Restore button and web
content imitating these prompt titles do not qualify. Unsupported structures
are reported without falling back to a browser-wide button search.

Chrome requests window activation when it creates the consent dialog. This
presser invokes native actions without issuing a window-activation command.

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

    The standalone scanner does not run a main loop between calls. Dispatch
    before reading the desktop so application and tree changes reach its cache.
    The daemon also dispatches this context between scans and while idle.
    Bound this pass by `seconds` so continuous signals cannot prevent a scan."""
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end and ctx.pending():
        ctx.iteration(False)

# These are native Chrome prompt titles, not titles from rendered web pages.
PROMPTS = {
    'Allow remote debugging?': ('consent', 'Allow'),
    'Restore pages?': ('crash_recovery', 'Restore'),
}
BUTTON_NAMES = tuple(button for kind, button in PROMPTS.values())
PROMPT_ROLES = {'dialog', 'alert'}
DOCUMENT_ROLES = {
    'document', 'document web', 'document frame', 'document text',
    'document email', 'document presentation', 'document spreadsheet', 'web view',
}


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


def press_button(btn, reporter=None, button='unknown', prompt_kind=None):
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
                _report(reporter, 'native_action_started', button=button, prompt_kind=prompt_kind)
                action_started = True
                result = act.do_action(j)
                _report(reporter, 'native_action_finished', button=button, prompt_kind=prompt_kind,
                        native_result=result if isinstance(result, bool) else None)
                return True
        _report(reporter, 'press_action_unavailable', button=button, prompt_kind=prompt_kind)
    except Exception as error:
        event = 'native_action_error' if action_started else 'accessibility_query_error'
        _report(reporter, event, button=button, prompt_kind=prompt_kind,
                error_type=type(error).__name__)
    return False


def find_prompt_buttons(node, report, counts, prompt=None, depth=0, max_depth=30):
    """Find intended native buttons without entering renderer documents.

    Traverse once so distinct Allow nodes retain their order. Buttons in
    unrecognized or conflicting dialog contexts are excluded. A nested native
    window starts a separate context.
    """
    found = []
    if node is None:
        return found
    if depth > max_depth:
        counts['depth_limited'] += 1
        report('prompt_structure_unavailable', reason='depth_limit')
        return found
    try:
        role = node.get_role_name()
        if role in DOCUMENT_ROLES:
            counts['document_subtrees_skipped'] += 1
            return found
        if role in ('frame', 'window') and prompt is not None:
            counts['unsupported_prompts'] += 1
            report('prompt_structure_unavailable', reason='nested_window')
            prompt = None
        if role in PROMPT_ROLES:
            selected = PROMPTS.get(node.get_name())
            if selected is None:
                counts['unsupported_prompts'] += 1
                report('prompt_structure_unavailable', reason='unrecognized_prompt')
                prompt = ('blocked', None)
            elif prompt is not None and prompt[0] == 'blocked':
                counts['unsupported_prompts'] += 1
                report('prompt_structure_unavailable', reason='unsupported_ancestor')
            elif prompt is not None and selected != prompt:
                counts['unsupported_prompts'] += 1
                report('prompt_structure_unavailable', reason='conflicting_prompts')
                prompt = ('ambiguous', None)
            else:
                prompt = selected
                counts['matched_prompts'] += 1
        if role in ('push button', 'button'):
            name = node.get_name()
            if name in BUTTON_NAMES:
                if prompt is not None and prompt[1] == name:
                    found.append((node, name, prompt[0]))
                else:
                    counts['excluded_buttons'] += 1
                    report('button_rejected', button=name, reason='outside_intended_prompt')
        for index in range(node.get_child_count()):
            try:
                found.extend(find_prompt_buttons(node.get_child_at_index(index), report,
                    counts, prompt, depth + 1, max_depth))
            except Exception as error:
                report('accessibility_query_error', error_type=type(error).__name__)
    except Exception as error:
        report('accessibility_query_error', error_type=type(error).__name__)
    return found


def scan_and_press(reporter=None):
    """Invoke buttons from recognized native Chrome prompts; count completed calls."""
    _pump_glib()
    desk = Atspi.get_desktop(0)
    completed = 0
    applications = 0
    matched = 0
    counts = dict.fromkeys(('query_errors', 'document_subtrees_skipped',
                           'unsupported_prompts', 'matched_prompts',
                           'excluded_buttons', 'depth_limited'), 0)

    def report(event, **metadata):
        if event == 'accessibility_query_error':
            counts['query_errors'] += 1
        _report(reporter, event, **metadata)

    for index in range(desk.get_child_count()):
        try:
            app = desk.get_child_at_index(index)
            if app and app.get_name() == 'Google Chrome':
                applications += 1
                buttons = find_prompt_buttons(app, report, counts)
                matched += len(buttons)
                for btn, button, prompt_kind in buttons:
                    report('button_matched', button=button, prompt_kind=prompt_kind)
                    if press_button(btn, report, button, prompt_kind):
                        completed += 1
        except Exception as error:
            report('accessibility_query_error', error_type=type(error).__name__)
    report('scan_finished', chrome_applications=applications,
           matched_buttons=matched, completed_calls=completed, **counts)
    return completed


def chrome_accessibility_health():
    """Return whether Chrome and renderer-content roles are visible to AT-SPI.

    Browser controls and renderer content are observed separately. Chrome's
    consent and recovery dialogs are native controls; their availability is
    established by prompt scanning, not by renderer-content roles. Missing
    renderer roles can indicate disabled accessibility or an incomplete tree.
    The daemon records this observation and continues scanning.
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
