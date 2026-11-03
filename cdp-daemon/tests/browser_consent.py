#!/usr/bin/env python3
"""Exercise actual Chrome prompts on an owned display and accessibility bus.

Run explicitly with system Python on AC power. This starts private Chrome and
xfwm4, enables debugging through its UI, and deliberately crashes that owned
browser to exercise recovery. The user's Chrome and daemon are not contacted.
"""
import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
ROOT = None
REPORT = {}
API = None
LAUNCHER = None
STOP_POWER = threading.Event()
POWER_LOST = threading.Event()


def ac_online():
    return any(
        (entry / "type").is_file()
        and (entry / "type").read_text().strip() == "Mains"
        and (entry / "online").read_text().strip() == "1"
        for entry in Path("/sys/class/power_supply").iterdir()
    )


def require_ac():
    if POWER_LOST.is_set() or not ac_online():
        raise RuntimeError("AC was unavailable; this owned run requires re-execution")


def watch_power():
    while not STOP_POWER.wait(0.5):
        if not ac_online():
            POWER_LOST.set()


def record(step, **metadata):
    REPORT["steps"].append({
        "step": step,
        "time_zagreb": datetime.datetime.now(ZoneInfo("Europe/Zagreb")).isoformat(),
        **metadata,
    })
    (ROOT / "result.json").write_text(json.dumps(REPORT, indent=2))


def wait_for(function, description, seconds=20):
    deadline = time.monotonic() + seconds
    while True:
        require_ac()
        value = function()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(0.05)


def request(route, body=None, timeout=40):
    if route not in ("/fixture/stop", "/fixture/close"):
        require_ac()
    assert API is not None and not API.endswith(":7799")
    operation = urllib.request.Request(
        API + route,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urllib.request.urlopen(operation, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        value = json.load(response)
        if response.status != 200:
            raise RuntimeError(json.dumps(value))
        return value


def cdp(method, params=None, sid=None, timeout=15):
    body = {"method": method, "params": params or {}, "timeout_seconds": timeout}
    if sid is not None:
        body["sessionId"] = sid
    return request("/cdp", body, timeout + 5)["result"]


def evaluate(sid, expression):
    value = cdp("Runtime.evaluate", {
        "expression": expression, "returnByValue": True, "awaitPromise": True,
    }, sid)
    if value.get("exceptionDetails"):
        raise RuntimeError(json.dumps(value["exceptionDetails"]))
    return value.get("result", {}).get("value")


def attach(target):
    return cdp("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]


def native(label, mode="inspect"):
    result = request("/fixture/presser", {"mode": mode, "label": label})
    assert result["exit_code"] == 0
    value = result["data"]
    assert value["private_bus_and_display_verified"]
    assert value["observed_chrome_application_pids"] == [value["chrome_pid"]]
    return value


def selected(value, when="before"):
    tabs = value["accessibility_" + when]["page_tabs"]
    result = [(index, tab["object_path"]) for index, tab in enumerate(tabs) if tab["selected"]]
    assert result and all(identity for index, identity in result)
    return result


def maximized(value, when="before"):
    return any(window["maximized_horz"] and window["maximized_vert"]
               for window in value["native_" + when]["chrome_windows"])


def acquire(label):
    before = native(label + "-before")
    request("/fixture/connect", {})
    scans = []
    deadline = time.monotonic() + 40
    while True:
        require_ac()
        info = request("/fixture/info")
        if info["connected"]:
            break
        if info["connection_result"] is not None:
            raise RuntimeError(json.dumps(info["connection_result"]))
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned native consent did not complete")
        scans.append(native(f"{label}-scan-{len(scans) + 1}", "press"))
        time.sleep(0.5)
    after = native(label + "-after")
    assert before["native_before"] == after["native_after"]
    assert selected(before) == selected(after, "after")
    actions = [event for scan in scans for event in scan["events"]
               if event["event"] == "native_action_started"]
    assert actions and all(event["prompt_kind"] == "consent" and event["button"] == "Allow"
                           for event in actions)
    assert maximized(after, "after")
    record(label, scan_count=len(scans), only_intended_allow_actions=True,
           maximization_selection_focus_preserved=True, connected=True)


def managed_acquisition():
    """Use the production presser thread for two native consent acquisitions."""
    for cycle in range(2):
        label = f"managed-presser-{cycle + 1}"
        before = native(label + "-before")
        cursor = request("/status")["consent_diagnostics"]["last_seq"]
        request("/fixture/disconnect", {})
        request("/fixture/connect", {"managed": True})

        def connected():
            info = request("/fixture/info")
            if info["connection_result"] is not None and not info["connected"]:
                raise RuntimeError("Owned managed presser failed to acquire Chrome")
            return info["connected"]

        wait_for(connected, "Owned managed presser did not finish native consent", 40)
        after = native(label + "-after")
        assert before["native_before"] == after["native_after"]
        assert selected(before) == selected(after, "after")
        events = request("/status")["consent_diagnostics"]["events"]
        actions = [e for e in events if e["seq"] > cursor and e["event"] == "native_action_started"]
        assert actions and all(e["button"] == "Allow" and e["prompt_kind"] == "consent" for e in actions)
        scan_count = sum(e["event"] == "scan_started" for e in events)
        time.sleep(0.6)
        idle_events = request("/status")["consent_diagnostics"]["events"]
        assert sum(e["event"] == "scan_started" for e in idle_events) == scan_count
        assert not any(e["event"] == "presser_dispatch_failed" for e in idle_events)
        record(label, production_thread_acquired_native_consent=True,
               idle_scanning_stopped=True, maximization_selection_focus_preserved=True)


def first_page():
    return next(target for target in cdp("Target.getTargets")["targetInfos"]
                if target["type"] == "page" and target["url"] == "about:blank")


def verify_profile():
    target = cdp("Target.createTarget", {
        "url": "chrome://version/", "background": True, "newWindow": False,
    })["targetId"]
    try:
        sid = attach(target)
        cdp("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        value = evaluate(sid, """(async()=>{
          const deadline=performance.now()+10000;
          while(!document.querySelector('#profile_path')?.textContent?.trim()){
            if(performance.now()>deadline)throw new Error('Owned profile path unavailable');
            await new Promise(resolve=>setTimeout(resolve,25));
          }
          return {profilePath:document.querySelector('#profile_path').textContent.trim()};
        })()""")
        assert value["profilePath"] == str(ROOT / "profile/Default")
        (ROOT / "actual-profile-proof.json").write_text(json.dumps(value, indent=2))
        version = cdp("Browser.getVersion")
        record("actual_owned_profile_verified", **value,
               browser={key: version[key] for key in ("product", "revision", "protocolVersion")})
    finally:
        cdp("Target.closeTarget", {"targetId": target})


def calibrate_observers():
    target = first_page()["targetId"]
    window = cdp("Browser.getWindowForTarget", {"targetId": target})["windowId"]
    focused = native("focus-observer-calibration", "calibrate")
    assert focused["focus_observer_calibrated"]
    assert focused["native_before"] == focused["native_after"]
    cdp("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "normal"}})
    normal = native("window-observer-normal")
    assert not maximized(normal)
    cdp("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "maximized"}})
    restored = native("window-observer-maximized")
    assert maximized(restored)
    record("native_observers_calibrated", focus_change_detected=True,
           normal_and_maximized_detected=True)


def window_control():
    inspections = []

    def actionable():
        value = native(f"native-window-before-{len(inspections) + 1}")
        inspections.append(value)
        matching = any(button['name'] == 'Restore' and not button['prompt_kinds']
                       and not button['under_document'] and button['showing'] and button['press_action']
                       for button in value['accessibility_before']['buttons'])
        return value if matching and maximized(value) else None

    wait_for(actionable, 'The owned maximized-window Restore control was not actionable')
    value = native('native-window-control', 'press')
    assert any(button['name'] == 'Restore' and not button['prompt_kinds']
               and not button['under_document'] and button['showing'] and button['press_action']
               for button in value['accessibility_before']['buttons'])
    assert value['completed_calls'] == 0
    assert value['native_before'] == value['native_after'] and maximized(value, 'after')
    assert selected(value) == selected(value, 'after')
    record('actionable_native_window_restore_rejected', native_state_preserved=True)


def web_document_control():
    sid = attach(first_page()["targetId"])
    html = """<!doctype html><title>Owned web prompt imitation</title>
      <script>window.calls={allow:0,restore:0}</script>
      <div role="dialog" aria-label="Allow remote debugging?">
        <button onclick="calls.allow++">Allow</button></div>
      <div role="alertdialog" aria-label="Restore pages?">
        <button onclick="calls.restore++">Restore</button></div>"""
    cdp("Page.navigate", {"url": "data:text/html," + quote(html)}, sid)
    evaluate(sid, """(async()=>{
      while(document.title!=='Owned web prompt imitation')await new Promise(r=>setTimeout(r,25));
      document.querySelector('button').click();
      if(window.calls.allow!==1)throw new Error('Owned click counter did not observe its control');
      window.calls.allow=0;return true;
    })()""")
    value = native("maximized-window-web-imitation", "press")
    assert value["completed_calls"] == 0
    assert value["native_before"] == value["native_after"] and maximized(value, "after")
    assert selected(value) == selected(value, "after")
    assert evaluate(sid, "window.calls") == {"allow": 0, "restore": 0}
    assert all(any(button['name'] == name and button['under_document']
                   and button['prompt_kinds'] and button['showing'] and button['press_action']
                   for button in value['accessibility_before']['buttons'])
               for name in ('Allow', 'Restore'))
    record("web_prompt_imitations_rejected", native_state_preserved=True,
           calibrated_web_handlers_unchanged=True)


def enable_owned_debugging():
    target = cdp("Target.createTarget", {
        "url": "chrome://inspect/#remote-debugging", "background": True, "newWindow": False,
    })["targetId"]
    try:
        sid = attach(target)
        cdp("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        enabled = evaluate(sid, """(async()=>{
          const deadline=performance.now()+15000;
          while(!document.querySelector('#remote-debugging-enabled')||
                document.querySelector('#remote-debugging-enabled').disabled){
            if(performance.now()>deadline)throw new Error('Owned debugging toggle unavailable');
            await new Promise(r=>setTimeout(r,25));
          }
          const box=document.querySelector('#remote-debugging-enabled');
          if(!box.checked)box.click();return box.checked;
        })()""")
        assert enabled
        record("owned_remote_debugging_enabled_through_ui")
    finally:
        cdp("Target.closeTarget", {"targetId": target})


def actual_recovery():
    sid = attach(first_page()["targetId"])
    url = "data:text/html," + quote(
        "<!doctype html><title>Owned previous recovery session</title><p>Owned recovery state</p>")
    cdp("Page.navigate", {"url": url}, sid)
    evaluate(sid, """(async()=>{
      while(document.title!=='Owned previous recovery session')await new Promise(r=>setTimeout(r,25));
      return true;
    })()""")
    def saved():
        return any(url.encode() in file.read_bytes() or url.encode("utf-16-le") in file.read_bytes()
                   for file in (ROOT / "profile/Default/Sessions").glob("Session_*"))
    wait_for(saved, "Owned recovery navigation was not saved")
    def crash_marker_written():
        prefs = ROOT / "profile/Default/Preferences"
        return prefs.exists() and json.loads(prefs.read_text()).get("profile", {}).get("exit_type") == "Crashed"
    wait_for(crash_marker_written, "Owned running-session crash marker was not written", 30)
    record("owned_navigation_and_crash_marker_saved_before_crash")
    stopped = request("/fixture/close", {"crash": True})
    assert stopped["chrome_exit_code"] < 0 and not stopped["used_sigterm_fallback"]
    record("owned_browser_crashed", exit_code=stopped["chrome_exit_code"], sigterm_fallback=False)
    request("/fixture/start", {"bootstrap": False})
    inspections = []
    def recovery_prompt_visible():
        value = native(f"actual-crash-recovery-before-{len(inspections) + 1}")
        inspections.append(value)
        return value if any(prompt["kind"] == "crash_recovery"
                            for prompt in value["accessibility_before"]["prompts"]) else None
    before = wait_for(recovery_prompt_visible, "Owned crash-recovery prompt did not appear")
    scans = []
    deadline = time.monotonic() + 40
    while True:
        value = native(f"actual-crash-recovery-scan-{len(scans) + 1}", "press")
        scans.append(value)
        assert before["native_before"] == value["native_after"]
        assert selected(before) == selected(value, "after")
        if not any(prompt["kind"] == "crash_recovery"
                   for prompt in value["accessibility_after"]["prompts"]):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned native recovery prompt did not close")
        time.sleep(0.5)
    assert len(scans[-1]["accessibility_after"]["page_tabs"]) > len(before["accessibility_before"]["page_tabs"])
    actions = [event for scan in scans for event in scan["events"]
               if event["event"] == "native_action_started"]
    assert actions and all(event["prompt_kind"] == "crash_recovery" and event["button"] == "Restore"
                           for event in actions)
    acquire("post-recovery-consent")
    restored = wait_for(lambda: [target for target in cdp("Target.getTargets")["targetInfos"]
                                if target.get("url") == url], "Saved navigation was not restored")
    record("actual_crash_recovery_verified", scan_count=len(scans),
           saved_navigation_restored=bool(restored), existing_selection_focus_maximization_preserved=True,
           restored_tabs_added_in_background=True)


def cleanup_launcher():
    if LAUNCHER is None:
        return
    if API is not None:
        try:
            request("/fixture/stop", {})
        except Exception as error:
            REPORT["cleanup_request_error_type"] = type(error).__name__
    try:
        LAUNCHER.wait(60)
    except subprocess.TimeoutExpired:
        # The launcher has a recorded private process group. Chrome starts its
        # own group, so verify its unique owned profile before terminating it.
        lock = ROOT / "profile/SingletonLock"
        try:
            pid = int(os.readlink(lock).rsplit("-", 1)[1])
            proc = Path("/proc") / str(pid)
            entries = dict(item.split(b"=", 1) for item in (proc / "environ").read_bytes().split(b"\0") if b"=" in item)
            if pid > 1 and (proc / "exe").resolve().name == "chrome" and entries.get(b"HOME") == str(ROOT / "home").encode():
                os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        if LAUNCHER.poll() is None:
            os.killpg(LAUNCHER.pid, signal.SIGTERM)
        LAUNCHER.wait(15)
        REPORT["cleanup_sigterm_fallback"] = True
    REPORT["owned_launcher_exit_code"] = LAUNCHER.returncode


def main():
    global ROOT, REPORT, API, LAUNCHER
    ROOT = Path(tempfile.mkdtemp(prefix="cdp-native-prompts.", dir="/tmp"))
    REPORT = {"directory": str(ROOT), "steps": []}
    try:
        require_ac()
        for command in ("google-chrome", "xvfb-run", "dbus-run-session", "xfwm4", "xdotool", "xprop", "xwininfo", "gdbus", "ss"):
            if shutil.which(command) is None:
                raise RuntimeError("Required native test command is unavailable: " + command)
        power_thread = threading.Thread(target=watch_power, daemon=True)
        power_thread.start()
        environment = {
            "PATH": os.environ["PATH"], "LANG": "C.UTF-8",
            "XDG_DATA_DIRS": "/usr/local/share:/usr/share", "XDG_SESSION_TYPE": "x11",
            "XDG_CURRENT_DESKTOP": "XFCE", "GTK_USE_PORTAL": "0", "GSETTINGS_BACKEND": "memory",
            "CDP_NATIVE_FIXTURE_ROOT": str(ROOT),
        }
        for key, name in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                          ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                          ("XDG_RUNTIME_DIR", "runtime")):
            directory = ROOT / name
            directory.mkdir(mode=0o700)
            environment[key] = str(directory)
        with (ROOT / "controller.log").open("ab") as log:
            LAUNCHER = subprocess.Popen([
                "xvfb-run", "-a", "-s", "-screen 0 1280x900x24", "dbus-run-session", "--",
                sys.executable, str(HERE / "native_consent_controller.py"),
            ], env=environment, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        def ready():
            if LAUNCHER.poll() is not None:
                raise RuntimeError("Owned native controller exited; inspect " + str(ROOT / "controller.log"))
            file = ROOT / "controller.json"
            return json.loads(file.read_text()) if file.exists() else None
        configuration = wait_for(ready, "Owned native controller did not start", 40)
        API = configuration["api"]
        assert configuration["connected"]
        verify_profile()
        window_control()
        calibrate_observers()
        web_document_control()
        enable_owned_debugging()
        closed = request("/fixture/close", {})
        assert closed["chrome_exit_code"] == 0 and not closed["used_sigterm_fallback"]
        request("/fixture/start", {"bootstrap": False})
        acquire("actual-consent")
        actual_recovery()
        managed_acquisition()
        require_ac()
        REPORT["outcome"] = "passed"
    except BaseException as error:
        REPORT["outcome"] = "failed"
        REPORT["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        try:
            cleanup_launcher()
            if LAUNCHER is not None and LAUNCHER.returncode != 0:
                raise RuntimeError('Owned launcher cleanup did not exit normally')
        except BaseException as error:
            REPORT['outcome'] = 'failed'
            REPORT['cleanup_failure'] = {'type': type(error).__name__, 'message': str(error)}
            raise
        finally:
            STOP_POWER.set()
            REPORT["ac_loss_observed"] = POWER_LOST.is_set()
            failed_after_work = POWER_LOST.is_set() and REPORT.get("outcome") == "passed"
            if failed_after_work:
                REPORT["outcome"] = "failed"
                REPORT["failure"] = {"type": "ACLoss", "message": "Native run requires re-execution"}
            (ROOT / "result.json").write_text(json.dumps(REPORT, indent=2))
            print(json.dumps(REPORT, indent=2))
            if failed_after_work:
                raise RuntimeError("AC was lost during the owned native run")


if __name__ == "__main__":
    main()
