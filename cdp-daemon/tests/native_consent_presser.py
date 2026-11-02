#!/usr/bin/env python3
"""Inspect or press owned native Chrome prompts on a private accessibility bus."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(os.environ["CDP_NATIVE_FIXTURE_ROOT"]).resolve()
assert ROOT.parent == Path("/tmp") and ROOT.name.startswith("cdp-native-prompts.")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clear_modals as modals

pid = int(sys.argv[1])
profile = Path(sys.argv[2]).resolve()
mode, label = sys.argv[3:5]
assert profile.is_relative_to(ROOT)
assert os.environ.get("DISPLAY") and not re.match(r"^:(0|1)(\.|$)", os.environ["DISPLAY"])
assert os.environ.get("WAYLAND_DISPLAY") is None
assert f"/run/user/{os.getuid()}/bus" not in os.environ["DBUS_SESSION_BUS_ADDRESS"]
assert os.environ.get("AT_SPI_BUS_ADDRESS")
configuration = json.loads((ROOT / "controller.json").read_text())
assert configuration["chrome_pid"] == pid
assert Path(configuration["profile"]).resolve() == profile
actual_profile = json.loads((ROOT / "actual-profile-proof.json").read_text())["profilePath"]
assert Path(actual_profile).resolve() == profile / "Default"
port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
listeners = subprocess.check_output(["ss", "-ltnp", f"sport = :{port}"], text=True)
assert {int(value) for value in re.findall(r"pid=(\d+)", listeners)} == {pid}
assert (Path("/proc") / str(pid) / "exe").resolve().name == "chrome"
registration_started = time.monotonic()
deadline = registration_started + 10
while True:
    modals._pump_glib()
    desktop = modals.Atspi.get_desktop(0)
    applications = []
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is not None and app.get_name() == "Google Chrome":
            assert app.get_process_id() == pid, "Unexpected Chrome PID on the private accessibility bus"
            applications.append(app)
    if applications:
        break
    if time.monotonic() >= deadline:
        raise TimeoutError("Owned Chrome is not registered on the private accessibility bus")
    time.sleep(0.05)


def native_window():
    focus = int(subprocess.check_output(["xdotool", "getwindowfocus"], text=True).strip())
    active = subprocess.check_output(
        ["xprop", "-root", "_NET_ACTIVE_WINDOW"], text=True).strip().split()[-1]
    found = subprocess.run([
        "xdotool", "search", "--all", "--onlyvisible", "--pid", str(pid), "--name", ".*",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    windows = []
    for raw in found.stdout.splitlines():
        xid = int(raw)
        data = subprocess.check_output(["xprop", "-id", str(xid), "_NET_WM_STATE"], text=True)
        windows.append({
            "id": xid,
            "maximized_horz": "_NET_WM_STATE_MAXIMIZED_HORZ" in data,
            "maximized_vert": "_NET_WM_STATE_MAXIMIZED_VERT" in data,
        })
    return {"focus_window": focus, "active_window": active, "chrome_windows": windows}


def inspect():
    buttons, prompts, tabs = [], [], []
    errors = 0

    def walk(node, ancestors=(), depth=0):
        nonlocal errors
        if node is None or depth > 40:
            return
        try:
            role = node.get_role_name()
            named_roles = ("push button", "button", "dialog", "alert", "page tab")
            name = node.get_name() if role in named_roles else None
            if role in ("dialog", "alert"):
                known = modals.PROMPTS.get(name)
                kind = known[0] if known else "unrecognized"
                prompts.append({"role": role, "kind": kind})
                context = (*ancestors, (role, kind))
            elif role in modals.DOCUMENT_ROLES:
                context = (*ancestors, (role, "document"))
            else:
                context = (*ancestors, (role, None))
            if role in ("push button", "button") and name in modals.BUTTON_NAMES:
                states, actions = node.get_state_set(), node.get_action_iface()
                buttons.append({
                    "name": name,
                    "ancestor_roles": [item[0] for item in ancestors],
                    "prompt_kinds": [item[1] for item in ancestors
                                     if item[1] in ("consent", "crash_recovery")],
                    "under_document": any(item[1] == "document" for item in ancestors),
                    "showing": states.contains(modals.Atspi.StateType.SHOWING),
                    "press_action": any(actions.get_action_name(index) == "press"
                                        for index in range(actions.get_n_actions())),
                })
            if role == "page tab":
                states = node.get_state_set()
                tabs.append({
                    "selected": states.contains(modals.Atspi.StateType.SELECTED),
                    "owned_control": name.startswith("Owned") if isinstance(name, str) else False,
                    "object_path": getattr(node, "path", None),
                })
            for index in range(node.get_child_count()):
                walk(node.get_child_at_index(index), context, depth + 1)
        except Exception:
            errors += 1

    for app in applications:
        walk(app)
    return {"buttons": buttons, "prompts": prompts, "page_tabs": tabs,
            "inspection_query_errors": errors}


report = {
    "label": label, "mode": mode, "chrome_pid": pid,
    "observed_chrome_application_pids": [app.get_process_id() for app in applications],
    "private_bus_and_display_verified": True,
    "application_registration_wait_ms": (time.monotonic() - registration_started) * 1000,
    "native_before": native_window(), "accessibility_before": inspect(), "events": [],
}


def reporter(event, **metadata):
    report["events"].append({
        "event": event,
        "time_zagreb": datetime.datetime.now(ZoneInfo("Europe/Zagreb")).isoformat(),
        "monotonic_ns": time.monotonic_ns(), **metadata,
    })


if mode == "calibrate":
    original = report["native_before"]["focus_window"]
    root_text = subprocess.check_output(["xwininfo", "-root"], text=True)
    root_window = int(re.search(r"Window id:\s*(0x[0-9a-fA-F]+)", root_text)[1], 16)
    try:
        subprocess.run(["xdotool", "windowfocus", "--sync", str(root_window)], check=True)
        assert native_window()["focus_window"] != original
    finally:
        subprocess.run(["xdotool", "windowfocus", "--sync", str(original)], check=True)
    report["focus_observer_calibrated"] = True
if mode == "press":
    # Check the complete desktop again immediately before the production scanner.
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is not None and app.get_name() == "Google Chrome":
            assert app.get_process_id() == pid
    report["completed_calls"] = modals.scan_and_press(reporter)
report["native_after"] = native_window()
report["accessibility_after"] = inspect()
(ROOT / (label + ".json")).write_text(json.dumps(report, indent=2))
print(json.dumps(report))
