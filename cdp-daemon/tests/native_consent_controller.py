#!/usr/bin/env python3
"""Own Chrome, its window manager, accessibility bus and CDP test endpoint."""
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(os.environ["CDP_NATIVE_FIXTURE_ROOT"]).resolve()
SOURCE = Path(__file__).resolve().parents[1]
assert ROOT.parent == Path("/tmp") and ROOT.name.startswith("cdp-native-prompts.")
sys.path.insert(0, str(SOURCE))
import cdp_daemon as CDP
from browser_consent import wait_for_window_manager

assert os.environ.get("DISPLAY") and not re.match(r"^:(0|1)(\.|$)", os.environ["DISPLAY"])
assert os.environ.get("WAYLAND_DISPLAY") is None
assert f"/run/user/{os.getuid()}/bus" not in os.environ["DBUS_SESSION_BUS_ADDRESS"]
assert Path(os.environ["HOME"]).resolve().is_relative_to(ROOT)
assert Path(os.environ["XDG_RUNTIME_DIR"]).resolve().is_relative_to(ROOT)
assert any(
    (entry / "type").is_file() and (entry / "type").read_text().strip() == "Mains"
    and (entry / "online").read_text().strip() == "1"
    for entry in Path("/sys/class/power_supply").iterdir()
)


def bus_call(*arguments):
    return subprocess.check_output([
        "gdbus", "call", "--session", "--dest", "org.a11y.Bus", *arguments,
    ], text=True)


address = ast.literal_eval(bus_call(
    "--object-path", "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress").strip())[0]
os.environ["AT_SPI_BUS_ADDRESS"] = address
bus_call("--object-path", "/org/a11y/bus", "--method", "org.freedesktop.DBus.Properties.Set",
         "org.a11y.Status", "IsEnabled", "<true>")
wm_log = (ROOT / "window-manager.log").open("ab")
wm = subprocess.Popen(["xfwm4", "--replace", "--compositor=off"],
                      stdout=wm_log, stderr=subprocess.STDOUT)
try:
    wait_for_window_manager(wm, ROOT / "window-manager-readiness.json")
except BaseException:
    if wm.poll() is None:
        wm.terminate()
        wm.wait(10)
    wm_log.close()
    raise
profile = ROOT / "profile"
chrome_log = (ROOT / "chrome.log").open("ab")
chrome = None
reader = None
connection_thread = None
presser_thread = None
connection_result = None
used_sigterm_fallback = False
STOP = threading.Event()
INFO_LOCK = threading.Lock()


def endpoint():
    lines = (profile / "DevToolsActivePort").read_text().splitlines()
    return int(lines[0]), lines[1]


CDP._discover_endpoint = endpoint


def call(method, params=None, sid=None, timeout=10):
    return CDP.cdp_call(method, params or {}, sessionId=sid, timeout_seconds=timeout)["result"]


def connect(managed=False):
    global reader, connection_result
    try:
        assert chrome is not None and chrome.poll() is None
        if managed:
            if not CDP.connect_once(150):
                raise RuntimeError("Owned managed acquisition failed")
        else:
            CDP.PORT, CDP.PATH = endpoint()
            attempt = CDP.begin_consent_attempt(150)
            response = CDP.ws_upgrade(150)
            if response[0] != "ok":
                raise RuntimeError("Owned upgrade outcome " + response[0])
            CDP.cdp_sock, CDP.cdp_leftover = response[1:]
            CDP.cdp_sock.settimeout(None)
            CDP.consent_event("connection_ready", attempt, state="connected")
        CDP.disconnected.clear()
        reader = threading.Thread(target=CDP.reader_thread, daemon=True)
        reader.start()
        connection_result = {"connected": True}
    except Exception as error:
        connection_result = {
            "connected": False, "error_type": type(error).__name__, "message": str(error),
        }
    write_info()


def start(bootstrap):
    global chrome, connection_result
    assert chrome is None or chrome.poll() is not None
    endpoint_file = profile / "DevToolsActivePort"
    if endpoint_file.exists():
        endpoint_file.unlink()
    arguments = [
        shutil.which("google-chrome"), "--ozone-platform=x11",
        "--user-data-dir=" + str(profile), "--no-first-run", "--no-default-browser-check",
        "--password-store=basic", "--disable-background-networking",
        "--force-renderer-accessibility", "--start-maximized",
    ]
    if bootstrap:
        arguments.append("--remote-debugging-port=0")
    arguments.append("about:blank")
    chrome = subprocess.Popen(arguments, stdout=chrome_log, stderr=subprocess.STDOUT,
                              start_new_session=True)
    connection_result = None
    deadline = time.monotonic() + 30
    while not endpoint_file.exists():
        if chrome.poll() is not None:
            raise RuntimeError("Owned Chrome exited during startup")
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned Chrome did not publish its endpoint")
        time.sleep(0.05)
    if bootstrap:
        connect()
        assert connection_result["connected"]
    write_info()
    return info()


def stop_browser(crash=False):
    global used_sigterm_fallback
    used_sigterm_fallback = False
    if chrome is None or chrome.poll() is not None:
        return
    connected_before = CDP.cdp_sock is not None
    if connected_before:
        try:
            call("Browser.crash" if crash else "Browser.close", timeout=5)
        except Exception:
            # Closing or crashing can end the transport before a response.
            # Wait for process exit before using the recorded owned PID.
            pass
    elif not crash:
        used_sigterm_fallback = True
        chrome.terminate()
    try:
        chrome.wait(20)
    except subprocess.TimeoutExpired:
        used_sigterm_fallback = True
        chrome.terminate()
        chrome.wait(10)
    if reader is not None:
        reader.join(5)
        assert not reader.is_alive()
    write_info()


def info():
    return {
        "api": f"http://127.0.0.1:{server.server_port}", "directory": str(ROOT),
        "profile": str(profile), "chrome_pid": chrome.pid if chrome else None,
        "chrome_exit_code": chrome.poll() if chrome else None,
        "connected": CDP.cdp_sock is not None and connection_result == {"connected": True},
        "connection_result": connection_result,
        "used_sigterm_fallback": used_sigterm_fallback,
        "private_display": os.environ["DISPLAY"], "private_session_bus": True,
        "private_accessibility_bus": True, "candidate_source": str(SOURCE),
    }


def write_info():
    if "server" in globals():
        with INFO_LOCK:
            temporary = ROOT / "controller.json.tmp"
            temporary.write_text(json.dumps(info(), indent=2))
            temporary.replace(ROOT / "controller.json")


def native_probe(body):
    assert chrome is not None and chrome.poll() is None
    mode = body.get("mode", "inspect" if body.get("inspect", False) else "press")
    assert mode in ("inspect", "press", "calibrate")
    label = body.get("label", "probe")
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", label)
    arguments = [
        sys.executable, str(Path(__file__).with_name("native_consent_presser.py")),
        str(chrome.pid), str(profile), mode, label,
    ]
    response = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = response.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        response.terminate()
        output, errors = response.communicate(timeout=10)
        raise TimeoutError("Owned native presser exceeded its wait and received SIGTERM")
    error_file = ROOT / (label + ".stderr")
    error_file.write_bytes(errors)
    if response.returncode:
        raise RuntimeError("Owned native probe failed; inspect " + str(error_file))
    return {"exit_code": response.returncode, "stderr_bytes": len(errors), "data": json.loads(output)}


class Handler(CDP.Handler):
    def do_GET(self):
        if self.path == "/fixture/info":
            self._send(200, info())
        else:
            super().do_GET()

    def do_POST(self):
        global connection_thread, presser_thread, connection_result
        if self.path.startswith("/fixture/"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/fixture/start":
                    result = start(body.get("bootstrap", False))
                elif self.path == "/fixture/close":
                    stop_browser(body.get("crash", False))
                    result = info()
                elif self.path == "/fixture/disconnect":
                    assert CDP.cdp_sock is not None and reader is not None
                    CDP.cdp_sock.shutdown(CDP.socket.SHUT_RDWR)
                    reader.join(5)
                    assert not reader.is_alive() and CDP.cdp_sock is None
                    result = {"disconnected": True}
                elif self.path == "/fixture/connect":
                    assert CDP.cdp_sock is None
                    managed = body.get("managed", False)
                    connection_result = None
                    if managed and presser_thread is None:
                        presser_thread = threading.Thread(target=CDP.presser_main, daemon=True)
                        presser_thread.start()
                    connection_thread = threading.Thread(target=connect, args=(managed,), daemon=True)
                    connection_thread.start()
                    result = {"started": True}
                elif self.path == "/fixture/presser":
                    result = native_probe(body)
                elif self.path == "/fixture/stop":
                    STOP.set()
                    result = {"stopping": True}
                else:
                    raise ValueError("Unknown owned fixture route")
                self._send(200, result)
            except Exception as error:
                self._send(500, {"error_type": type(error).__name__, "error": str(error)})
        elif self.path in ("/shutdown", "/reconnect"):
            self._send(403, {"error": "Use the owned fixture lifecycle routes"})
        else:
            super().do_POST()


server = CDP.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    start(True)
    STOP.wait()
finally:
    try:
        CDP.press_wanted.stop()
        try:
            if presser_thread is not None:
                presser_thread.join(5)
        finally:
            stop_browser()
        if presser_thread is not None:
            assert not presser_thread.is_alive()
    finally:
        wm.terminate()
        wm.wait(10)
        server.shutdown()
        server.server_close()
        thread.join(5)
        chrome_log.close()
        wm_log.close()
