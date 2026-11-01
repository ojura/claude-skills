#!/usr/bin/env python3
"""Exercise preparation in an owned Chrome profile on a private X display.

Run with python3 -B -I tests/browser_page_preparation.py -v. Chrome, Xvfb and
xvfb-run are required. The test extension observes browser-maintained tab state;
production preparation does not depend on an extension.
"""
import base64
import collections
import concurrent.futures
import ctypes
import ctypes.util
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("browser_test_cdp", ROOT / "cdp_daemon.py")
CDP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CDP)


class Fixtures(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.counts = collections.Counter()
        self.gates = {}
        self.started = {}
        self.lock = threading.Lock()

    def gate(self, token):
        with self.lock:
            self.gates.setdefault(token, threading.Event())
            self.started.setdefault(token, threading.Event())
            return self.gates[token], self.started[token]


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request = urllib.parse.urlsplit(self.path)
        token = urllib.parse.parse_qs(request.query).get("token", [""])[0]
        with self.server.lock:
            self.server.counts[request.path] += 1
        if request.path == "/worker.js":
            data = b"""self.onmessage = event => {
              self.postMessage('entered');
              Atomics.wait(new Int32Array(event.data), 0, 0);
              self.postMessage('released');
            };"""
            content_type = "text/javascript"
        elif request.path in ("/held-image", "/held-fetch"):
            release, started = self.server.gate(token)
            started.set()
            if not release.wait(45):
                self.send_error(500, "The fixture was not released")
                return
            if request.path == "/held-image":
                data = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1kAAAAASUVORK5CYII=")
                content_type = "image/png"
            else:
                data = b'{"released":true}'
                content_type = "application/json"
        else:
            image = (f'<img src="/held-image?token={token}">' if request.path == "/load-wait" else "")
            data = ("""<!doctype html><html><head><title>CDP method fixture</title>
              <style>body{height:10000px;background:rgb(17,34,51)}</style></head>
              <body><input id="draft">""" + image + """<script>
              window.identity=crypto.randomUUID(); window.events=[]; window.rafCount=0;
              document.addEventListener('freeze',()=>window.events.push('freeze'));
              document.addEventListener('resume',()=>window.events.push('resume'));
              function frame(){window.rafCount++; requestAnimationFrame(frame)}
              requestAnimationFrame(frame);
              </script></body></html>""").encode()
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class OwnedBrowser:
    def __init__(self, directory):
        self.directory = directory
        self.profile = directory / "profile"
        self.extension = directory / "extension"
        self.extension.mkdir()
        (self.extension / "manifest.json").write_text(json.dumps({
            "manifest_version": 3, "name": "CDP Preparation Test Observer", "version": "1.0",
            "permissions": ["tabs"], "background": {"service_worker": "worker.js"},
        }))
        (self.extension / "worker.js").write_text("chrome.runtime.onInstalled.addListener(() => {});\n")
        self.prototype_extension = directory / "selection-prototype"
        shutil.copytree(ROOT / "browser-state-extension", self.prototype_extension)
        shutil.copyfile(ROOT / "research/view_initialization.js",
                        self.prototype_extension / "view_initialization.js")
        prototype_manifest = self.prototype_extension / "manifest.json"
        manifest = json.loads(prototype_manifest.read_text())
        manifest["background"] = {"service_worker": "view_initialization.js"}
        prototype_manifest.write_text(json.dumps(manifest))
        self.selection_prototype_installed = False
        self.log = (directory / "chrome.log").open("ab")
        self.process = None
        self.reader = None
        self.observer = None
        self.sessions = set()
        self.sent = []
        self.sent_lock = threading.Lock()
        self.original_send = CDP.ws_send
        CDP.ws_send = self.record_send
        self.on_send = None
        self.extension_id = None

    def record_send(self, message, **kwargs):
        command = json.loads(message)
        with self.sent_lock:
            self.sent.append(command)
        if self.on_send is not None:
            self.on_send(command)
        return self.original_send(message, **kwargs)

    def start(self, restore=False):
        endpoint = self.profile / "DevToolsActivePort"
        if endpoint.exists():
            endpoint.unlink()
        env = dict(os.environ)
        env.pop("WAYLAND_DISPLAY", None)
        env["XDG_SESSION_TYPE"] = "x11"
        guard = ('case "$DISPLAY" in :0|:0.*|:1|:1.*|"") '
                 'printf "Refusing the desktop display\\n" >&2; exit 64;; esac; '
                 'printf "%s\\n%s\\n" "$DISPLAY" "$XAUTHORITY" > "$1"; shift; exec "$@"')
        args = ["xvfb-run", "-a", "-s", "-screen 0 1280x900x24", "sh", "-c", guard,
                "cdp-browser-test", str(self.directory / "display"),
                shutil.which("google-chrome"), "--ozone-platform=x11",
                "--user-data-dir=" + str(self.profile), "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check", "--password-store=basic",
                "--enable-unsafe-extension-debugging", "--disable-background-networking",
                "--disable-session-crashed-bubble"]
        args += ["--restore-last-session"] if restore else ["about:blank"]
        self.process = subprocess.Popen(args, env=env, cwd=self.directory,
                                        stdout=self.log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        deadline = time.monotonic() + 30
        while not endpoint.exists():
            if self.process.poll() is not None:
                raise RuntimeError(f"Owned Chrome exited: {self.process.returncode}")
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned Chrome did not publish its endpoint")
            time.sleep(.05)
        self.display_name, self.xauthority = (self.directory / "display").read_bytes().splitlines()
        self.connect()
        helper_id = self.call("Extensions.loadUnpacked", {"path": str(ROOT / "browser-state-extension")})["id"]
        self.selection_prototype_installed = False
        if CDP.page_preparation.extension_origin != "chrome-extension://" + helper_id:
            raise AssertionError("The installed helper does not match its manifest identity")
        self.open_observer()

    def open_observer(self):
        loaded = False
        if self.extension_id is None:
            self.extension_id = self.call("Extensions.loadUnpacked", {"path": str(self.extension)})["id"]
            loaded = True
        deadline = time.monotonic() + 10
        while True:
            workers = [target for target in self.call("Target.getTargets")["targetInfos"]
                       if target["type"] == "service_worker" and
                       target["url"].startswith("chrome-extension://" + self.extension_id + "/")]
            for worker in workers:
                try:
                    self.observer = self.attach(worker["targetId"])
                    self.call("Runtime.enable", sid=self.observer)
                    self.eval(self.observer, "typeof chrome.tabs.query")
                    return
                except CDP.CDPError:
                    current = self.call("Target.getTargets")["targetInfos"]
                    if any(target["targetId"] == worker["targetId"] for target in current):
                        raise
            if not workers and not loaded:
                self.extension_id = self.call("Extensions.loadUnpacked", {"path": str(self.extension)})["id"]
                loaded = True
            if time.monotonic() >= deadline:
                raise TimeoutError("The owned observer extension did not start")
            time.sleep(.05)

    def install_selection_prototype(self):
        if self.selection_prototype_installed:
            return
        bridges = list(CDP.page_preparation.bridges.values())
        for bridge in bridges:
            if bridge.target_id is not None:
                self.call("Target.closeTarget", {"targetId": bridge.target_id})
        extension_id = CDP.page_preparation.extension_origin.rsplit("/", 1)[-1]
        self.call("Extensions.uninstall", {"id": extension_id})
        self.call("Extensions.loadUnpacked", {"path": str(self.prototype_extension)})
        for bridge in bridges:
            if not bridge.completed.wait(5):
                raise AssertionError("The owned state reader did not close before prototype installation")
        self.selection_prototype_installed = True

    def connect(self):
        lines = (self.profile / "DevToolsActivePort").read_text().splitlines()
        CDP.PORT, CDP.PATH = int(lines[0]), lines[1]
        reply = CDP.ws_upgrade(10)
        if reply[0] != "ok":
            raise RuntimeError(str(reply))
        CDP.cdp_sock, CDP.cdp_leftover = reply[1:]
        self.reader = threading.Thread(target=CDP.reader_thread, daemon=True)
        self.reader.start()

    def stop(self):
        if self.process is None:
            return
        try:
            if CDP.cdp_sock is not None:
                self.call("Browser.close", timeout=10)
        finally:
            try:
                self.process.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(10)
            self.process = None
            if self.reader is not None:
                self.reader.join(5)
                if self.reader.is_alive():
                    raise RuntimeError("The owned CDP reader did not stop")
            self.sessions.clear()
            self.observer = None

    def close(self):
        try:
            self.stop()
        finally:
            CDP.ws_send = self.original_send
            self.log.close()

    def call(self, method, params=None, sid=None, timeout=10):
        return CDP.cdp_call(method, params or {}, sessionId=sid, timeout_seconds=timeout)["result"]

    def eval(self, sid, expression, timeout=10):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        }, sid, timeout)
        if result.get("exceptionDetails"):
            raise AssertionError(json.dumps(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    def attach(self, target_id):
        sid = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        self.sessions.add(sid)
        return sid

    def detach(self, sid):
        if sid not in self.sessions:
            return
        try:
            CDP.release_page(sid)
            self.call("Target.detachFromTarget", {"sessionId": sid})
        finally:
            self.sessions.discard(sid)

    def tabs(self):
        return self.eval(self.observer, """chrome.tabs.query({}).then(tabs => tabs.map(
          ({id,url,status,discarded,frozen,active}) => ({id,url,status,discarded,frozen,active})))""")

    def tab(self, tab_id):
        return next(tab for tab in self.tabs() if tab["id"] == tab_id)

    def active_ids(self):
        return [tab["id"] for tab in self.tabs() if tab["active"]]

    def target_for(self, url):
        return next(target for target in self.call("Target.getTargets")["targetInfos"]
                    if target["type"] == "page" and target["url"] == url)

    def create(self, url, hidden=False):
        params = {"url": url, "hidden": True} if hidden else {"url": url, "background": True}
        target = self.call("Target.createTarget", params)["targetId"]
        return target, self.attach(target)

    def wait_tab_complete(self, tab_id):
        return self.eval(self.observer, """new Promise(resolve => {
          const id = %d;
          const done = (tabId, change) => {
            if (tabId === id && change.status === 'complete') {
              chrome.tabs.onUpdated.removeListener(done); resolve(true);
            }
          };
          chrome.tabs.onUpdated.addListener(done);
          chrome.tabs.get(id).then(tab => {if (tab.status === 'complete') done(id,{status:'complete'})});
        })""" % tab_id)


class InputFocusControl:
    """Put input focus in an owned window on the browser's private X display."""

    def __init__(self, browser):
        display_name = browser.display_name
        if display_name.split(b".", 1)[0] in (b":0", b":1"):
            raise AssertionError("Refusing to change input focus on the desktop display")
        self.x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_ulong, ctypes.c_ulong]
        self.x11.XCreateSimpleWindow.restype = ctypes.c_ulong
        for name in ("XMapWindow", "XDestroyWindow"):
            getattr(self.x11, name).argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.x11.XGetInputFocus.argtypes = [ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
        self.x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        previous_authority = os.environ.get("XAUTHORITY")
        try:
            os.environ["XAUTHORITY"] = os.fsdecode(browser.xauthority)
            self.display = self.x11.XOpenDisplay(display_name)
        finally:
            if previous_authority is None:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = previous_authority
        if not self.display:
            raise AssertionError("The private X display could not be opened")
        root = self.x11.XDefaultRootWindow(self.display)
        self.window = self.x11.XCreateSimpleWindow(self.display, root, 5, 5, 120, 80, 0, 0, 0)

    def __enter__(self):
        self.x11.XMapWindow(self.display, self.window)
        self.x11.XSync(self.display, 0)
        self.x11.XSetInputFocus(self.display, self.window, 1, 0)
        self.x11.XSync(self.display, 0)
        if self.focus() != self.window:
            raise AssertionError("The input-focus control window did not acquire focus")
        return self

    def focus(self):
        window, revert = ctypes.c_ulong(), ctypes.c_int()
        self.x11.XGetInputFocus(self.display, ctypes.byref(window), ctypes.byref(revert))
        return window.value

    def __exit__(self, *args):
        self.x11.XDestroyWindow(self.display, self.window)
        self.x11.XCloseDisplay(self.display)


@unittest.skipUnless(shutil.which("google-chrome") and shutil.which("xvfb-run"),
                     "Chrome and xvfb-run are required")
class BrowserPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="cdp-preparation-")
        cls.fixtures = Fixtures()
        cls.fixture_thread = threading.Thread(target=cls.fixtures.serve_forever, daemon=True)
        cls.fixture_thread.start()
        cls.base = f"http://127.0.0.1:{cls.fixtures.server_port}"
        cls.browser = OwnedBrowser(Path(cls.temporary.name))
        cls.api = CDP.ThreadingHTTPServer(("127.0.0.1", 0), CDP.Handler)
        cls.api_thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.api_thread.start()
        cls.measurements = []
        try:
            cls.browser.start()
            cls.version = cls.browser.call("Browser.getVersion")
            for i in range(26):
                url = cls.base + "/restore/" + str(i)
                tab_id = cls.browser.eval(cls.browser.observer,
                    "chrome.tabs.create({url:%s,active:false}).then(t=>t.id)" % json.dumps(url))
                cls.browser.wait_tab_complete(tab_id)
                target = cls.browser.target_for(url)
                sid = cls.browser.attach(target["targetId"])
                cls.browser.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
                cls.browser.call("Runtime.enable", sid=sid)
                cls.browser.eval(sid, """(async () => {
                  document.querySelector('#draft').value = 'restored draft';
                  document.querySelector('#draft').dispatchEvent(new Event('input',{bubbles:true}));
                  sessionStorage.setItem('saved', 'restored session storage');
                  scrollTo(0,321);
                  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
                  history.replaceState({saved:true}, '', location.href + '#saved');
                  return true;
                })()""")
                cls.browser.call("Emulation.setFocusEmulationEnabled", {"enabled": False}, sid)
                cls.browser.detach(sid)
            cls.browser.stop()
            cls.browser.start(restore=True)
            states = cls.browser.tabs()
            cls.restored = [tab for tab in states if tab["status"] == "unloaded" and not tab["discarded"]]
            if len(cls.restored) < 4:
                raise AssertionError("The real restoration fixture did not produce enough unloaded tabs")
            cls.measurements.append({"case": "restore_fixture", "unloaded": len(cls.restored),
                                     "browser": cls.version["product"], "revision": cls.version["revision"]})
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        finally:
            for gate in cls.fixtures.gates.values():
                gate.set()
            cls.api.shutdown()
            cls.api.server_close()
            cls.api_thread.join(5)
            cls.fixtures.shutdown()
            cls.fixtures.server_close()
            cls.fixture_thread.join(5)
            for measurement in cls.measurements:
                print("MEASURE " + json.dumps(measurement, sort_keys=True), flush=True)
            cls.temporary.cleanup()

    def setUp(self):
        self.owned_targets = []
        self.owned_sessions = []
        self.preparation_threads = []
        self.selected_before = self.browser.active_ids()

    def tearDown(self):
        self.browser.on_send = None
        for sid in reversed(self.owned_sessions):
            try:
                self.browser.detach(sid)
            except (CDP.CDPError, RuntimeError):
                pass
        for target in reversed(self.owned_targets):
            try:
                self.browser.call("Target.closeTarget", {"targetId": target})
            except (CDP.CDPError, RuntimeError):
                pass
        if self.browser.observer is not None and CDP.cdp_sock is not None:
            self.assertEqual(self.browser.active_ids(), self.selected_before)
        for thread in self.preparation_threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "An owned preparation thread did not stop")
        self.assertEqual(CDP.page_preparation.active_states, set())
        self.assertEqual(CDP.pending, {})

    def start_preparation(self, sid, readiness, timeout, allow_tab_selection=False):
        future = concurrent.futures.Future()

        def run():
            try:
                if allow_tab_selection:
                    self.browser.install_selection_prototype()
                    result = CDP.page_preparation._prototype_prepare_with_selection(sid, readiness, timeout)
                else:
                    result = CDP.prepare_page(sid, readiness, timeout)
                future.set_result(result)
            except BaseException as error:
                future.set_exception(error)

        thread = threading.Thread(target=run, daemon=True)
        self.preparation_threads.append(thread)
        thread.start()
        return future

    def create(self, suffix, hidden=False):
        target, sid = self.browser.create(self.base + suffix, hidden=hidden)
        self.owned_targets.append(target)
        self.owned_sessions.append(sid)
        return target, sid

    def attach_restored(self):
        tab = next(tab for tab in self.browser.tabs()
                   if tab["status"] == "unloaded" and not tab["discarded"] and
                   tab["url"].startswith(self.base + "/restore/"))
        target = self.browser.target_for(tab["url"])["targetId"]
        sid = self.browser.attach(target)
        self.owned_sessions.append(sid)
        return tab, target, sid

    def prepare(self, sid, readiness="dom", timeout=10, allow_tab_selection=False):
        start = time.monotonic()
        if allow_tab_selection:
            self.browser.install_selection_prototype()
            response = CDP.page_preparation._prototype_prepare_with_selection(sid, readiness, timeout)
        else:
            response = CDP.prepare_page(sid, readiness, timeout)
        self.measurements.append({"case": self._testMethodName, "readiness": readiness,
                                 "seconds": round(time.monotonic() - start, 4)})
        return response

    def http(self, route, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.api.server_port}{route}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def extension_worker(self, prepared_session=None):
        if prepared_session is not None:
            self.browser.install_selection_prototype()
            CDP.prepare_page(prepared_session, "javascript")
            bridge = next(bridge for bridge in CDP.page_preparation.bridges.values() if bridge.phase == "ready")
            active = self.browser.active_ids()[0]
            response = self.browser.eval(bridge.session_id, """(async()=>{
              const target=(await chrome.debugger.getTargets()).find(target=>target.tabId===%d);
              return chrome.runtime.sendMessage({method:'initialize-view',targetId:target.id,requesterTargetId:%s});
            })()""" % (active, json.dumps(bridge.target_id)))
            self.assertTrue(response["ok"])
            self.assertFalse(response["result"]["changed"])
        workers = [target for target in self.browser.call("Target.getTargets")["targetInfos"]
                   if target["type"] == "service_worker" and
                   target["url"].startswith(CDP.page_preparation.extension_origin + "/")]
        self.assertEqual(len(workers), 1)
        sid = self.browser.attach(workers[0]["targetId"])
        self.owned_sessions.append(sid)
        self.browser.call("Runtime.enable", sid=sid)
        return sid

    def test_01_focus_alone_loads_a_deferred_restored_tab(self):
        tab, _, sid = self.attach_restored()
        before = time.monotonic()
        self.browser.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        seconds = time.monotonic() - before
        self.browser.call("Runtime.enable", sid=sid)
        self.browser.eval(sid, "new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
        self.assertEqual(self.browser.tab(tab["id"])["status"], "complete")
        snapshot = self.browser.eval(sid, "({draft:document.querySelector('#draft').value,"
            "saved:sessionStorage.getItem('saved'),state:history.state,scrollY,"
            "visible:document.visibilityState,focus:document.hasFocus()})")
        self.assertEqual(snapshot["draft"], "restored draft")
        self.assertEqual(snapshot["saved"], "restored session storage")
        self.assertEqual(snapshot["state"], {"saved": True})
        self.assertEqual(snapshot["scrollY"], 321)
        self.assertEqual(snapshot["visible"], "visible")
        self.assertTrue(snapshot["focus"])
        self.assertFalse(self.browser.tab(tab["id"])["active"])
        self.measurements.append({"case": "focus_alone", "before": tab,
                                 "seconds": round(seconds, 4), "restored": snapshot})

    def test_02_unfreezing_alone_does_not_load_a_deferred_tab(self):
        tab, _, sid = self.attach_restored()
        self.browser.call("Page.setWebLifecycleState", {"state": "active"}, sid)
        self.assertEqual(self.browser.tab(tab["id"])["status"], "unloaded")
        with self.assertRaises(TimeoutError):
            self.browser.eval(sid, "globalThis.queuedProbe=(globalThis.queuedProbe||0)+1", timeout=.1)
        self.assertEqual(self.browser.tab(tab["id"])["status"], "unloaded")
        self.prepare(sid, "animation")
        self.assertEqual(self.browser.tab(tab["id"])["status"], "complete")
        self.assertFalse(self.browser.tab(tab["id"])["active"])

    def test_03_page_enable_before_focus_waits_on_an_unloaded_renderer(self):
        tab, _, sid = self.attach_restored()
        with self.assertRaises(TimeoutError):
            self.browser.call("Page.enable", sid=sid, timeout=.1)
        self.assertEqual(self.browser.tab(tab["id"])["status"], "unloaded")
        status, result = self.http("/prepare", {"sessionId": sid, "readiness": "animation"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["document"]["animationCallbacks"], 2)
        self.assertEqual(self.browser.tab(tab["id"])["status"], "complete")

    def test_04_loaded_background_state_is_preserved_and_detach_releases_emulation(self):
        _, sid = self.create("/loaded")
        self.browser.call("Runtime.enable", sid=sid)
        before = self.browser.eval(sid, "({identity:window.identity,visible:document.visibilityState,focus:document.hasFocus()})")
        self.assertEqual(before["visible"], "hidden")
        self.assertFalse(before["focus"])
        self.browser.eval(sid, "document.querySelector('#draft').value='do not lose me';history.replaceState({draft:true},'')")
        requests = self.fixtures.counts["/loaded"]
        self.prepare(sid, "animation")
        self.prepare(sid, "animation")
        after = self.browser.eval(sid, "({identity:window.identity,draft:document.querySelector('#draft').value,state:history.state})")
        self.assertEqual(after, {"identity": before["identity"], "draft": "do not lose me", "state": {"draft": True}})
        self.assertEqual(self.fixtures.counts["/loaded"], requests)
        target = self.owned_targets[-1]
        self.browser.detach(sid)
        check = self.browser.attach(target)
        self.owned_sessions.append(check)
        released = self.browser.eval(check, "({visible:document.visibilityState,focus:document.hasFocus(),identity:window.identity})")
        self.assertEqual(released, {"visible": "hidden", "focus": False, "identity": before["identity"]})

    def discarded_fixture(self, suffix):
        target, sid = self.create(suffix)
        self.prepare(sid, "animation")
        self.browser.eval(sid, """(async()=>{
          document.querySelector('#draft').value='discarded draft';
          sessionStorage.setItem('saved','discarded storage');
          scrollTo(0,321);
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          history.replaceState({saved:true},'',location.href);
          return true;
        })()""")
        self.browser.detach(sid)
        tab = next(tab for tab in self.browser.tabs() if tab["url"] == self.base + suffix)
        discarded_id = self.browser.eval(self.browser.observer,
            f"chrome.tabs.discard({tab['id']}).then(t=>t.id)")
        self.assertTrue(self.browser.tab(discarded_id)["discarded"])
        current_target = self.browser.target_for(self.base + suffix)["targetId"]
        if current_target != target:
            self.owned_targets.append(current_target)
        self.measurements.append({"case": "discard_identity", "tab_id_changed": discarded_id != tab["id"],
                                  "target_id_changed": current_target != target})
        return discarded_id, current_target

    def test_05_discarded_tab_loads_without_selecting_it(self):
        discarded_id, current_target = self.discarded_fixture("/discarded")
        sid = self.browser.attach(current_target)
        self.owned_sessions.append(sid)
        dom = self.prepare(sid, "dom")
        self.assertTrue(dom["initialTabState"]["discarded"])
        with self.assertRaises(CDP.PagePreparationError) as caught:
            CDP.prepare_page(sid, "animation", 1)
        self.assertEqual(caught.exception.kind, "view_uninitialized")
        self.assertEqual(caught.exception.details["document"]["window"], {"width": 0, "height": 0})
        with InputFocusControl(self.browser) as focus:
            self.assertFalse(self.browser.eval(self.browser.observer,
                f"chrome.tabs.get({discarded_id}).then(t=>chrome.windows.get(t.windowId)).then(w=>w.focused)"))
            rendered = self.prepare(sid, "animation", allow_tab_selection=True)
            self.assertEqual(focus.focus(), focus.window)
        self.assertTrue(rendered["viewInitialization"]["changed"])
        self.assertTrue(rendered["viewInitialization"]["restored"])
        self.assertEqual(self.fixtures.counts["/discarded"], 2)
        self.measurements.append({"case": "view_initialization", "native_focus_preserved": True,
                                  "chrome_selection_restored": True, "app_requests": 2})
        after = self.browser.tab(discarded_id)
        self.assertEqual(after["status"], "complete")
        self.assertFalse(after["discarded"])
        self.assertFalse(after["active"])
        self.assertTrue(self.browser.eval(sid, "document.wasDiscarded"))
        restored = self.browser.eval(sid, "({draft:document.querySelector('#draft').value,"
            "saved:sessionStorage.getItem('saved'),state:history.state,scrollY})")
        native_id, native_target = self.discarded_fixture("/discarded-native-control")
        selected = self.browser.active_ids()[0]
        try:
            self.browser.eval(self.browser.observer,
                f"chrome.tabs.update({native_id},{{active:true}}).then(t=>t.id)")
            self.browser.wait_tab_complete(native_id)
            native_sid = self.browser.attach(native_target)
            self.owned_sessions.append(native_sid)
            self.browser.call("Runtime.enable", sid=native_sid)
            native = self.browser.eval(native_sid, "({draft:document.querySelector('#draft').value,"
                "saved:sessionStorage.getItem('saved'),state:history.state,scrollY})")
        finally:
            self.browser.eval(self.browser.observer,
                f"chrome.tabs.update({selected},{{active:true}}).then(t=>t.id)")
        self.assertEqual(restored, native)
        self.measurements.append({"case": "discarded_state", "method": restored, "native": native})
        image = self.browser.call("Page.captureScreenshot", {"format": "png"}, sid)["data"]
        self.assertTrue(base64.b64decode(image).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_06_explicit_freeze_is_reversed_without_reloading(self):
        _, sid = self.create("/frozen")
        self.browser.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        self.browser.call("Runtime.enable", sid=sid)
        identity = self.browser.eval(sid, "window.identity")
        requests = self.fixtures.counts["/frozen"]
        self.browser.call("Emulation.setFocusEmulationEnabled", {"enabled": False}, sid)
        self.browser.call("Page.setWebLifecycleState", {"state": "frozen"}, sid)
        immediate = "responded"
        try:
            self.browser.eval(sid, "2+2", timeout=.1)
        except TimeoutError:
            immediate = "timed_out"
        with self.assertRaises(TimeoutError):
            self.browser.eval(sid, "new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))", timeout=.1)
        self.prepare(sid, "animation")
        self.assertEqual(self.browser.eval(sid, "window.identity"), identity)
        self.assertEqual(self.fixtures.counts["/frozen"], requests)
        events = self.browser.eval(sid, "window.events")
        self.assertIn("freeze", events)
        self.assertIn("resume", events)
        self.measurements.append({"case": "explicit_freeze", "immediate_eval": immediate, "events": events})

    def test_07_navigation_keeps_focus_emulation_on_the_same_session(self):
        _, sid = self.create("/before-navigation")
        self.prepare(sid)
        self.browser.call("Page.navigate", {"url": self.base + "/after-navigation"}, sid)
        result = self.prepare(sid, "animation")
        self.assertEqual(result["document"]["url"], self.base + "/after-navigation")
        self.assertTrue(result["document"]["hasFocus"])

    def test_08_two_sessions_keep_visible_capture_until_the_last_detaches(self):
        target, first = self.create("/two-sessions")
        second = self.browser.attach(target)
        self.owned_sessions.append(second)
        self.prepare(first)
        self.prepare(second)
        self.browser.detach(first)
        snapshot = self.browser.eval(second, "({visible:document.visibilityState,focus:document.hasFocus()})")
        self.assertEqual(snapshot["visible"], "visible")
        self.assertTrue(snapshot["focus"])

    def test_09_animation_readiness_produces_fresh_screenshots(self):
        _, sid = self.create("/screenshots")
        self.prepare(sid, "animation")
        first = self.browser.call("Page.captureScreenshot", {"format": "png"}, sid)["data"]
        self.browser.eval(sid, "document.body.style.background='rgb(12,130,44)'")
        self.prepare(sid, "animation")
        second = self.browser.call("Page.captureScreenshot", {"format": "png"}, sid)["data"]
        self.assertTrue(base64.b64decode(second).startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotEqual(first, second)

    def test_10_dom_and_load_readiness_have_different_conditions(self):
        token = "dom-versus-load"
        release, started = self.fixtures.gate(token)
        try:
            _, sid = self.create("/load-wait?token=" + token)
            self.assertTrue(started.wait(10))
            dom = self.prepare(sid, "dom")
            self.assertEqual(dom["document"]["readyState"], "interactive")
            with self.assertRaises(CDP.PagePreparationError) as caught:
                CDP.prepare_page(sid, "load", .15)
            self.assertEqual(caught.exception.kind, "timeout")
            self.assertEqual(caught.exception.stage, "readiness")
            self.assertEqual(self.browser.eval(sid, "document.visibilityState"), "visible")
            release.set()
            loaded = self.prepare(sid, "load")
            self.assertEqual(loaded["document"]["readyState"], "complete")
        finally:
            release.set()

    def test_11_an_unrelated_pending_fetch_does_not_block_dom_preparation(self):
        _, sid = self.create("/fetch-page")
        self.prepare(sid)
        identity = self.browser.eval(sid, "window.identity")
        release, started = self.fixtures.gate("fetch")
        try:
            self.browser.eval(sid, "window.fetchWait=fetch('/held-fetch?token=fetch').then(r=>r.json()); true")
            self.assertTrue(started.wait(10))
            self.prepare(sid)
            self.assertEqual(self.browser.eval(sid, "window.identity"), identity)
            self.assertFalse(release.is_set())
            release.set()
            self.assertEqual(self.browser.eval(sid, "window.fetchWait"), {"released": True})
        finally:
            release.set()

    def test_12_busy_worker_is_rejected_without_wake_commands(self):
        target, sid = self.create("/worker-page")
        self.prepare(sid)
        self.assertTrue(self.browser.eval(sid, "crossOriginIsolated"))
        self.browser.eval(sid, """new Promise(resolve => {
          window.releaseWorker = new Int32Array(new SharedArrayBuffer(4));
          window.testWorker = new Worker('/worker.js');
          window.testWorker.addEventListener('message', event => {if(event.data==='entered')resolve(true)});
          window.testWorker.postMessage(window.releaseWorker.buffer);
        })""")
        worker = next(item for item in self.browser.call("Target.getTargets")["targetInfos"]
                      if item["type"] == "worker" and item.get("parentId") == target)
        worker_sid = self.browser.attach(worker["targetId"])
        self.owned_sessions.append(worker_sid)
        try:
            start = len(self.browser.sent)
            with self.assertRaises(CDP.PagePreparationError) as caught:
                CDP.prepare_page(worker_sid)
            self.assertEqual(caught.exception.kind, "unsupported_target")
            commands = [command["method"] for command in self.browser.sent[start:]
                        if command.get("sessionId") == worker_sid]
            self.assertEqual(commands, ["Target.getTargetInfo"])
            self.prepare(sid)
            self.measurements.append({"case": "busy_worker", "url": worker["url"],
                                      "preparation_commands": commands})
        finally:
            self.browser.eval(sid, "Atomics.store(window.releaseWorker,0,1);Atomics.notify(window.releaseWorker,0)")
            self.browser.eval(sid, "window.testWorker.terminate();true")

    def test_12b_response_timeout_keeps_an_async_evaluation_running(self):
        _, sid = self.create("/late-evaluation")
        self.prepare(sid)
        with self.assertRaises(TimeoutError):
            self.browser.eval(sid, """(async()=>{
              await new Promise(resolve=>window.continueProbe=resolve);
              window.completedProbe=(window.completedProbe||0)+1;
              return window.completedProbe;
            })()""", timeout=.1)
        self.browser.eval(sid, "window.continueProbe();true")
        self.assertEqual(self.browser.eval(sid, "window.completedProbe"), 1)

    def test_13_hidden_targets_do_not_get_an_animation_success_without_callbacks(self):
        _, sid = self.create("/hidden-target", hidden=True)
        self.prepare(sid, "dom")
        with self.assertRaises(CDP.PagePreparationError) as caught:
            self.prepare(sid, "animation", timeout=.3)
        self.assertEqual(caught.exception.kind, "view_uninitialized")
        self.assertEqual(caught.exception.stage, "animation")
        self.assertEqual(caught.exception.details["document"]["window"], {"width": 0, "height": 0})
        self.measurements.append({"case": "hidden_target", "outcome": "view_uninitialized"})

    def test_14_navigation_during_readiness_repeats_the_read_only_probe(self):
        token = "navigate-while-loading"
        release, started = self.fixtures.gate(token)
        target, sid = self.create("/load-wait?token=" + token)
        control = self.browser.attach(target)
        self.owned_sessions.append(control)
        dispatched = threading.Event()
        self.browser.on_send = lambda command: dispatched.set() if (
            command.get("sessionId") == sid and command["method"] == "Runtime.evaluate" and
            "const mode" in command.get("params", {}).get("expression", "")) else None
        try:
            self.assertTrue(started.wait(10))
            preparation = self.start_preparation(sid, "load", 10)
            self.assertTrue(dispatched.wait(5))
            self.browser.call("Page.navigate", {"url": self.base + "/navigation-finished"}, control)
            result = preparation.result(5)
            self.assertEqual(result["document"]["url"], self.base + "/navigation-finished")
            self.assertEqual(result["document"]["readyState"], "complete")
        finally:
            release.set()

    def test_15_closing_a_target_ends_an_unbounded_readiness_wait(self):
        token = "close-while-loading"
        release, started = self.fixtures.gate(token)
        target, sid = self.create("/load-wait?token=" + token)
        dispatched = threading.Event()
        self.browser.on_send = lambda command: dispatched.set() if (
            command.get("sessionId") == sid and command["method"] == "Runtime.evaluate" and
            "const mode" in command.get("params", {}).get("expression", "")) else None
        try:
            self.assertTrue(started.wait(10))
            preparation = self.start_preparation(sid, "load", None)
            self.assertTrue(dispatched.wait(5))
            self.browser.call("Target.closeTarget", {"targetId": target})
            with self.assertRaises(CDP.PagePreparationError) as caught:
                preparation.result(5)
            self.assertEqual(caught.exception.kind, "detached")
            self.browser.sessions.discard(sid)
        finally:
            release.set()

    def test_16_active_tab_is_preserved(self):
        active = next(tab for tab in self.browser.tabs() if tab["active"])
        target = self.browser.target_for(active["url"])["targetId"]
        sid = self.browser.attach(target)
        self.owned_sessions.append(sid)
        self.prepare(sid, "animation")
        self.assertTrue(self.browser.tab(active["id"])["active"])

    def test_17_renderer_crash_ends_an_unbounded_readiness_wait(self):
        token = "crash-while-loading"
        release, started = self.fixtures.gate(token)
        other_origin = f"http://localhost:{self.fixtures.server_port}/load-wait?token={token}"
        target, sid = self.browser.create(other_origin)
        self.owned_targets.append(target)
        self.owned_sessions.append(sid)
        control = self.browser.attach(target)
        self.owned_sessions.append(control)
        dispatched = threading.Event()
        self.browser.on_send = lambda command: dispatched.set() if (
            command.get("sessionId") == sid and command["method"] == "Runtime.evaluate" and
            "const mode" in command.get("params", {}).get("expression", "")) else None
        try:
            self.assertTrue(started.wait(10))
            preparation = self.start_preparation(sid, "load", None)
            self.assertTrue(dispatched.wait(5))
            try:
                self.browser.call("Page.crash", sid=control, timeout=.2)
            except TimeoutError:
                pass
            with self.assertRaises(CDP.PagePreparationError) as caught:
                preparation.result(5)
            self.assertEqual(caught.exception.kind, "crashed")
        finally:
            release.set()

    def test_18_connection_loss_ends_an_unbounded_readiness_wait(self):
        token = "disconnect-while-loading"
        release, started = self.fixtures.gate(token)
        _, sid = self.create("/load-wait?token=" + token)
        dispatched = threading.Event()
        self.browser.on_send = lambda command: dispatched.set() if (
            command.get("sessionId") == sid and command["method"] == "Runtime.evaluate" and
            "const mode" in command.get("params", {}).get("expression", "")) else None
        try:
            self.assertTrue(started.wait(10))
            preparation = self.start_preparation(sid, "load", None)
            self.assertTrue(dispatched.wait(5))
            sock = CDP.cdp_sock
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            self.browser.reader.join(5)
            with self.assertRaises(CDP.PagePreparationError) as caught:
                preparation.result(5)
            self.assertEqual(caught.exception.kind, "disconnected")
        finally:
            release.set()
            self.browser.on_send = None
            self.browser.sessions.clear()
            self.owned_sessions.clear()
            self.browser.connect()
            self.browser.observer = None
            self.browser.open_observer()

    def test_19_release_ends_a_wait_and_keeps_the_client_session_attached(self):
        token = "release-while-loading"
        release, started = self.fixtures.gate(token)
        _, sid = self.create("/load-wait?token=" + token)
        dispatched = threading.Event()
        self.browser.on_send = lambda command: dispatched.set() if (
            command.get("sessionId") == sid and command["method"] == "Runtime.evaluate" and
            "const mode" in command.get("params", {}).get("expression", "")) else None
        try:
            self.assertTrue(started.wait(10))
            preparation = self.start_preparation(sid, "load", None)
            self.assertTrue(dispatched.wait(5))
            response = CDP.release_page(sid)
            self.assertTrue(response["released"])
            with self.assertRaises(CDP.PagePreparationError) as caught:
                preparation.result(5)
            self.assertEqual(caught.exception.kind, "released")
            self.assertEqual(self.browser.call("Target.getTargetInfo", sid=sid)["targetInfo"]["type"], "page")
            self.assertEqual(CDP.page_preparation.status(), [])
        finally:
            release.set()

    def test_20_missing_helper_is_reported_and_installation_recovers(self):
        _, sid = self.create("/missing-helper")
        extension_id = CDP.page_preparation.extension_origin.rsplit("/", 1)[-1]
        self.browser.call("Extensions.uninstall", {"id": extension_id})
        try:
            with self.assertRaises(CDP.PagePreparationError) as caught:
                self.prepare(sid)
            self.assertEqual(caught.exception.kind, "browser_state_unavailable")
            self.assertEqual(caught.exception.stage, "browser_state")
            self.assertIn("browser-state-extension", str(caught.exception))
        finally:
            installed = self.browser.call("Extensions.loadUnpacked", {"path": str(ROOT / "browser-state-extension")})["id"]
            self.browser.selection_prototype_installed = False
            self.assertEqual(installed, extension_id)
        result = self.prepare(sid, "dom")
        self.assertTrue(result["document"]["hasFocus"])

    def test_21_selection_prototype_has_a_query_update_counterexample(self):
        """Demonstrate why selection restoration is outside the supported API."""
        discarded_id, target = self.discarded_fixture("/selection-counterexample")
        sid = self.browser.attach(target)
        self.owned_sessions.append(sid)
        self.prepare(sid, "dom")
        _, third = self.create("/newer-selection")
        third_id = self.prepare(third, "javascript")["initialTabState"]["tabId"]
        selected = self.browser.active_ids()[0]
        worker = self.extension_worker(sid)
        self.browser.eval(worker, """(() => {
          const query = chrome.tabs.query;
          const update = chrome.tabs.update;
          globalThis.cdpInjectedSelection = false;
          chrome.tabs.query = async function(options) {
            const result = await query.call(chrome.tabs, options);
            if (options.active && result[0]?.id === %d && !globalThis.cdpInjectedSelection) {
              globalThis.cdpInjectedSelection = true;
              await update.call(chrome.tabs, %d, {active:true});
            }
            return result;
          };
          globalThis.cdpRestoreQuery = () => { chrome.tabs.query = query; };
          return true;
        })()""" % (discarded_id, third_id))
        try:
            response = self.prepare(sid, "animation", allow_tab_selection=True)
            self.assertTrue(self.browser.eval(worker, "globalThis.cdpInjectedSelection"))
            self.assertTrue(response["viewInitialization"]["restored"])
            self.assertEqual(self.browser.active_ids(), [selected])
            self.assertNotEqual(selected, third_id)
            self.measurements.append({"case": "selection_interleaving_counterexample",
                                      "newer_selection_overwritten": True})
        finally:
            self.browser.eval(worker, "globalThis.cdpRestoreQuery();true")

    def test_22_a_fresh_background_target_is_an_explicit_rendering_choice(self):
        _, old_target = self.discarded_fixture("/fresh-target-choice")
        old = self.browser.attach(old_target)
        self.owned_sessions.append(old)
        self.prepare(old, "javascript")
        self.browser.eval(old, "localStorage.setItem('cdp_shared','same-context');"
                              "document.cookie='cdp_shared=yes; path=/; SameSite=Lax';true")
        with self.assertRaises(CDP.PagePreparationError) as caught:
            CDP.prepare_page(old, "animation")
        self.assertEqual(caught.exception.kind, "view_uninitialized")
        context_id = self.browser.call("Target.getTargetInfo", sid=old)["targetInfo"]["browserContextId"]
        with InputFocusControl(self.browser) as focus:
            fresh_target = self.browser.call("Target.createTarget", {
                "url": self.base + "/fresh-target-choice", "background": True,
                "browserContextId": context_id,
            })["targetId"]
            self.owned_targets.append(fresh_target)
            fresh = self.browser.attach(fresh_target)
            self.owned_sessions.append(fresh)
            result = self.prepare(fresh, "animation")
            self.assertEqual(focus.focus(), focus.window)
        self.assertNotEqual(fresh_target, old_target)
        self.assertEqual(result["targetId"], fresh_target)
        state = self.browser.eval(fresh, "({shared:localStorage.getItem('cdp_shared'),"
            "session:sessionStorage.getItem('saved'),cookie:document.cookie})")
        self.assertEqual(state["shared"], "same-context")
        self.assertIsNone(state["session"])
        self.assertIn("cdp_shared=yes", state["cookie"])
        self.assertEqual(self.browser.eval(old, "({width:outerWidth,height:outerHeight})"),
                         {"width": 0, "height": 0})
        self.measurements.append({"case": "fresh_background_target", "native_focus_preserved": True,
                                  "cookies_and_local_storage_shared": True,
                                  "session_storage_is_fresh": True, "original_target_preserved": True})

    def test_23_applied_selection_cleanup_survives_timeout_and_connection_loss(self):
        """Exercise cleanup of the unsupported selection research transaction."""
        discarded_id, target = self.discarded_fixture("/selection-lifetime")
        sid = self.browser.attach(target)
        self.owned_sessions.append(sid)
        self.prepare(sid, "dom")
        worker = self.extension_worker(sid)
        selected = self.browser.active_ids()[0]
        requester = next(bridge.target_id for bridge in CDP.page_preparation.bridges.values()
                         if bridge.phase == "ready")
        self.assertTrue(self.browser.eval(worker, """(() => {
          const original = chrome.tabs.update;
          globalThis.cdpSelectionEntered = new Promise(resolve => {globalThis.cdpNotifySelection=resolve});
          let held = false;
          const wrapped = async function(id, options) {
            const result = await original.call(chrome.tabs,id,options);
            if (id === %d && options.active && !held) {
              held = true;
              globalThis.cdpNotifySelection(true);
              await new Promise(resolve => {globalThis.cdpReleaseSelection=resolve});
            }
            return result;
          };
          chrome.tabs.update = wrapped;
          globalThis.cdpRestoreUpdate=()=>{chrome.tabs.update=original};
          return chrome.tabs.update === wrapped;
        })()""" % discarded_id))
        try:
            with InputFocusControl(self.browser) as focus:
                preparation = self.start_preparation(sid, "animation", .2, True)
                self.browser.eval(worker, "globalThis.cdpSelectionEntered")
                with self.assertRaises(CDP.PagePreparationError) as caught:
                    preparation.result(5)
                self.assertEqual(caught.exception.kind, "timeout")
                self.assertEqual(caught.exception.stage, "view_initialization")
                self.assertEqual(self.browser.active_ids(), [discarded_id])
                self.assertEqual(focus.focus(), focus.window)
                sock = CDP.cdp_sock
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
                self.browser.reader.join(5)
                self.browser.sessions.clear()
                self.owned_sessions.clear()
                worker = None
                self.browser.connect()
                self.browser.open_observer()
                targets = self.browser.call("Target.getTargets")["targetInfos"]
                self.assertFalse(any(item["targetId"] == requester for item in targets))
                worker = self.extension_worker()
                self.assertEqual(self.browser.eval(worker, "typeof globalThis.cdpReleaseSelection"), "function")
                self.browser.eval(worker, "globalThis.cdpReleaseSelection();true")
                self.browser.eval(worker, "Promise.all([...windowOperations.values()]).then(()=>true)")
                self.assertEqual(self.browser.active_ids(), [selected])
                self.assertEqual(focus.focus(), focus.window)
                self.measurements.append({"case": "selection_cleanup_after_connection_loss",
                                          "requester_target_closed": True,
                                          "worker_finished_restoration": True,
                                          "native_focus_preserved": True})
        finally:
            if CDP.cdp_sock is not None and worker is not None:
                self.browser.eval(worker, "if(typeof cdpReleaseSelection==='function')cdpReleaseSelection();"
                                         "if(typeof cdpRestoreUpdate==='function')cdpRestoreUpdate();true")
            if CDP.cdp_sock is not None and self.browser.observer is not None:
                self.browser.eval(self.browser.observer,
                    f"chrome.tabs.update({selected},{{active:true}}).then(t=>t.id)")

    def test_24_javascript_readiness_does_not_wait_for_another_clients_load(self):
        token = "independent-readiness"
        release, started = self.fixtures.gate(token)
        target, first = self.create("/load-wait?token=" + token)
        second = self.browser.attach(target)
        self.owned_sessions.append(second)
        dispatched = threading.Event()
        self.browser.on_send = lambda command: dispatched.set() if (
            command.get("sessionId") == first and command["method"] == "Runtime.evaluate" and
            'const mode = "load"' in command.get("params", {}).get("expression", "")) else None
        try:
            self.assertTrue(started.wait(10))
            loading = self.start_preparation(first, "load", None)
            self.assertTrue(dispatched.wait(5))
            result = self.prepare(second, "javascript")
            self.assertEqual(result["document"]["readyState"], "interactive")
            self.assertFalse(loading.done())
            release.set()
            self.assertEqual(loading.result(5)["document"]["readyState"], "complete")
        finally:
            release.set()

    def test_25_native_size_and_capture_controls_leave_the_discarded_view_uninitialized(self):
        for mode in ("visible_size", "clipped_capture", "full_capture", "recording"):
            with self.subTest(mode=mode):
                _, original = self.create("/native-control-" + mode)
                prepared = self.prepare(original, "animation")
                original_id = prepared["initialTabState"]["tabId"]
                dimensions = prepared["document"]["window"]
                self.browser.detach(original)
                discarded = self.browser.eval(self.browser.observer,
                    f"chrome.tabs.discard({original_id}).then(t=>({{id:t.id,url:t.url}}))")
                target = self.browser.target_for(discarded["url"])["targetId"]
                self.owned_targets.append(target)
                sid = self.browser.attach(target)
                self.owned_sessions.append(sid)
                self.prepare(sid, "javascript")
                stream = None
                try:
                    if mode == "visible_size":
                        self.browser.call("Emulation.setVisibleSize", dimensions, sid)
                    elif mode == "recording":
                        stream = self.browser.call("Page.startScreenRecording", {
                            "audio": False, "maxWidth": dimensions["width"],
                            "maxHeight": dimensions["height"], "frameRate": 30,
                        }, sid)["stream"]
                    else:
                        params = {"format": "png", "captureBeyondViewport": True}
                        if mode == "clipped_capture":
                            params["clip"] = {"x": 0, "y": 0, **dimensions, "scale": 1}
                        with self.assertRaises(TimeoutError):
                            self.browser.call("Page.captureScreenshot", params, sid, .75)
                    document = self.browser.eval(sid,
                        "({innerWidth,innerHeight,outerWidth,outerHeight,frames:window.rafCount})")
                    bounds = self.browser.eval(self.browser.observer,
                        f"chrome.tabs.get({discarded['id']}).then(t=>({{width:t.width,height:t.height}}))")
                    self.assertEqual(document["outerWidth"], 0)
                    self.assertEqual(document["outerHeight"], 0)
                    self.assertEqual(document["frames"], 0)
                    with self.assertRaises(TimeoutError):
                        self.browser.eval(sid,
                            "new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))", timeout=.75)
                    self.measurements.append({"case": "native_view_control", "operation": mode,
                                              "document": document, "tabBoundsAfter": bounds,
                                              "response_wait_seconds": .75})
                finally:
                    if stream is not None:
                        self.browser.call("Page.stopScreenRecording", sid=sid)
                        self.browser.call("IO.close", {"handle": stream}, sid)
                    self.browser.detach(sid)
                    self.browser.call("Target.closeTarget", {"targetId": target})


if __name__ == "__main__":
    unittest.main()
