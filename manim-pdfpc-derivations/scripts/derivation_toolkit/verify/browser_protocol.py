"""Owned Chrome/Firefox sessions and displayed-frame acceptance primitives."""
from __future__ import annotations

from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.request import urlopen

import websocket


class CDP:
    def __init__(self, uri):
        self.ws = websocket.create_connection(uri, origin="http://127.0.0.1", timeout=15)
        self.seq = 0
        self.call("Runtime.enable")
        self.call("Page.enable")

    def call(self, method, params=None):
        self.seq += 1
        ident = self.seq
        self.ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            packet = json.loads(self.ws.recv())
            if packet.get("id") != ident:
                continue
            if "error" in packet:
                raise RuntimeError(str(packet["error"]))
            return packet.get("result", {})

    def eval(self, expression):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            raise RuntimeError(str(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    def wait(self, condition, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            result = self.eval(condition)
            if result:
                return
            time.sleep(0.25)
        diagnostics = self.eval("({url:location.href,page:document.getElementById('progress')?.textContent,"
                                "loading:document.getElementById('loading')?.textContent,"
                                "hidden:document.getElementById('loading')?.hidden,"
                                "error:document.getElementById('error')?.textContent})")
        raise AssertionError("Timed out waiting for " + condition + ": " + str(diagnostics))

    def capture(self, path):
        import base64
        image = self.call("Page.captureScreenshot", {"format": "png"})["data"]
        path.write_bytes(base64.b64decode(image))

    def key(self, key, ctrl=False):
        code = {" ": "Space", "ArrowRight": "ArrowRight", "ArrowLeft": "ArrowLeft",
                "Home": "Home", "End": "End"}.get(key, "Key" + key.upper())
        virtual = {" ": 32, "ArrowRight": 39, "ArrowLeft": 37, "Home": 36, "End": 35}.get(
            key, ord(key.upper()) if len(key) == 1 else 0)
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {"type": kind, "key": key, "code": code,
                                                "windowsVirtualKeyCode": virtual, "modifiers": 2 if ctrl else 0})

    def click(self, element_id):
        button = self.eval("(()=>{const r=document.getElementById(" + json.dumps(element_id) + ").getBoundingClientRect();"
                           "return {x:r.left+r.width/2,y:r.top+r.height/2}})()")
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {"type": kind, **button, "button": "left", "clickCount": 1})

    def playback_state(self):
        # The projector protocol carries the unrounded time and playback target.
        return self.eval("""new Promise((resolve, reject) => {
            const probe = new BroadcastChannel("derivation-html5-" + location.pathname);
            const timer = setTimeout(() => {
                probe.close();
                reject(new Error("No presenter state arrived"));
            }, 5000);
            probe.onmessage = ({data}) => {
                if (data?.type !== "state") return;
                clearTimeout(timer);
                probe.close();
                const {theme, page, time, speed, playing, target, blank} = data;
                resolve({theme, page, time, speed, playing, target, blank});
            };
            probe.postMessage({type: "request"});
        })""")

    def close(self):
        self.ws.close()


def json_pages(port):
    with urlopen("http://127.0.0.1:" + str(port) + "/json/list", timeout=3) as reply:
        return json.load(reply)


def wait_rendered(view, page, theme):
    view.wait("document.getElementById('slide')?.dataset.renderedPage === " + json.dumps(str(page)) +
              " && document.getElementById('slide').dataset.renderedTheme === " + json.dumps(theme) +
              " && document.getElementById('slide').getAttribute('aria-busy') === 'false'")


def begin_transition(view):
    # Sample the displayed layers every frame, including pixels hidden by a loading overlay.
    return view.eval(r"""(()=>{
        const prior = window.__derivationTransitionProbe;
        if (prior?.active) throw new Error("A transition probe is already active");
        const sample = document.createElement("canvas");
        sample.width = 64; sample.height = 48;
        const context = sample.getContext("2d", {willReadFrequently: true});
        const hash = bytes => {
            let value = 2166136261;
            for (const byte of bytes) value = Math.imul(value ^ byte, 16777619);
            return value >>> 0;
        };
        const hashText = text => hash(new TextEncoder().encode(text));
        const pixels = canvas => {
            context.clearRect(0, 0, 64, 48);
            context.drawImage(canvas, 0, 0, 64, 48);
            return [canvas.width, canvas.height, hash(context.getImageData(0, 0, 64, 48).data)];
        };
        const snapshot = () => {
            const slide = document.getElementById("slide");
            const vector = document.getElementById("vector");
            const text = document.getElementById("text-layer");
            const visible = getComputedStyle(vector).display !== "none";
            return {
                surface: JSON.stringify({
                    box: [slide.style.width, slide.style.height, slide.style.aspectRatio],
                    pdf: pixels(document.getElementById("pdf")),
                    vector: visible ? pixels(vector) : null,
                    region: visible ? vector.style.cssText : null,
                    text: [hashText(text.textContent), text.querySelectorAll("span").length,
                           text.style.cssText],
                    page: slide.dataset.renderedPage, theme: slide.dataset.renderedTheme,
                    blank: !document.getElementById("blank").hidden,
                }),
                preview: JSON.stringify({
                    pixels: pixels(document.getElementById("next-preview")),
                    title: document.getElementById("next-title").textContent,
                }),
                loading: !document.getElementById("loading").hidden,
                revision: slide.dataset.renderedRevision,
            };
        };
        const probe = {active: true, frames: [], snapshot, before: snapshot()};
        window.__derivationTransitionProbe = probe;
        const tick = () => {
            probe.frames.push(snapshot());
            if (probe.active) requestAnimationFrame(tick);
        };
        tick();
        return probe.before;
    })()""")


def assert_transition(view, page, theme, *, preview=False, total_pages=None, require_commit=True):
    wait_rendered(view, page, theme)
    if preview:
        if total_pages is None:
            raise ValueError("Preview checks require the actual PDF page count")
        view.wait("document.getElementById('next-preview').dataset.renderedPage === " +
                  json.dumps(str(min(page + 1, total_pages))) +
                  " && document.getElementById('next-preview').dataset.renderedTheme === " + json.dumps(theme))
    report = view.eval("""new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => {
        const probe = window.__derivationTransitionProbe;
        probe.active = false;
        const after = probe.snapshot();
        const partial = probe.frames.filter(frame =>
            frame.surface !== probe.before.surface && frame.surface !== after.surface);
        const partialPreview = probe.frames.filter(frame =>
            frame.preview !== probe.before.preview && frame.preview !== after.preview);
        resolve({sampledFrames: probe.frames.length,
                 loadingFrames: probe.frames.filter(frame => frame.loading).length,
                 intermediateSurfaceFrames: partial.length,
                 intermediatePreviewFrames: partialPreview.length,
                 committed: probe.before.revision !== after.revision,
                 firstIntermediate: partial[0] || null});
    })))""")
    if require_commit:
        assert report["committed"], report
    assert report["sampledFrames"] >= 2, report
    assert report["loadingFrames"] == report["intermediateSurfaceFrames"] == 0, report
    if preview:
        assert report["intermediatePreviewFrames"] == 0, report
    return report


def assert_blank_playback_ignored(presenter, audience=None):
    views = (presenter, audience) if audience else (presenter,)
    for view in views:
        view.wait("document.getElementById('blank').hidden === false && "
                  "document.getElementById('slide').getAttribute('aria-busy') === 'false'")
    before = presenter.playback_state()
    assert before["blank"] and not before["playing"], before
    clock = presenter.eval("document.getElementById('clock').textContent")
    for view in views:
        view.wait("document.getElementById('clock').textContent === " + json.dumps(clock))
    for key in ("a", "v"):
        presenter.key(key)
        time.sleep(0.2)
        after = presenter.playback_state()
        assert after == before, {"key": key, "before": before, "after": after}
        for view in views:
            assert view.eval("document.getElementById('blank').hidden") is False, key
            assert view.eval("document.getElementById('clock').textContent") == clock, key
    return before


def assert_visible_page_start(presenter, audience, page):
    theme = presenter.eval("document.getElementById('theme').value")
    for view in (presenter, audience):
        wait_rendered(view, page, theme)
        view.wait("document.getElementById('blank').hidden === true")
    state = presenter.playback_state()
    assert state["page"] == page - 1 and state["time"] == state["target"] == 0, state
    assert not state["blank"] and not state["playing"], state
    clock = presenter.eval("document.getElementById('clock').textContent")
    audience.wait("document.getElementById('clock').textContent === " + json.dumps(clock))


class Chrome(CDP):
    def __init__(self, uri, port, context=None):
        self.ws = websocket.create_connection(uri, suppress_origin=True, timeout=15)
        self.seq = 0
        self.port = port
        self.context = context
        self.call("Runtime.enable")
        self.call("Page.enable")
        self.call("Emulation.setFocusEmulationEnabled", {"enabled": True})

    def navigate(self, url):
        # The active player writes its own page hash. Destroy that document
        # before navigating to another fixture hash, so it cannot restore the
        # old page between same-document navigation and reload.
        if self.eval("location.href") != "about:blank":
            self._load_document("about:blank")
        self._load_document(url)

    def _load_document(self, url):
        previous = self.eval("performance.timeOrigin")
        result = self.call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise RuntimeError(result["errorText"])
        # A hash-only navigation does not rerun the player. Explicitly reload
        # that document so every fixture starts from its declared URL state.
        if not result.get("loaderId"):
            self.call("Page.reload")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if self.eval("performance.timeOrigin !== " + json.dumps(previous) +
                             " && document.readyState === 'complete'"):
                    return
            except RuntimeError as error:
                if not any(message in str(error) for message in
                           ("context was destroyed", "Cannot find context", "Inspected target navigated")):
                    raise
            time.sleep(.1)
        raise AssertionError("Chrome did not finish loading a new document: " + url)

    def pages(self):
        return [page for page in json_pages(self.port) if page.get("type") == "page"]

    def view(self, page):
        return Chrome(page["webSocketDebuggerUrl"], self.port, page["id"])

    def close_page(self):
        self.call("Target.closeTarget", {"targetId": self.context})


class Bidi:
    def __init__(self, uri):
        self.ws = websocket.create_connection(uri, suppress_origin=True, timeout=15)
        self.seq = 0
        self.call("session.new", {"capabilities": {}})

    def call(self, method, params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            packet = json.loads(self.ws.recv())
            if packet.get("id") != self.seq:
                continue
            if packet.get("type") == "error":
                raise RuntimeError(str(packet))
            return packet["result"]

    def close(self):
        self.ws.close()


class Firefox:
    wait = CDP.wait
    playback_state = CDP.playback_state

    def __init__(self, protocol, context):
        self.protocol = protocol
        self.context = context

    def eval(self, expression):
        result = self.protocol.call("script.evaluate", {
            "expression": "(async()=>JSON.stringify(await (" + expression + ")))()",
            "target": {"context": self.context}, "awaitPromise": True,
        })
        if result["type"] != "success":
            raise RuntimeError(result.get("exceptionDetails", {}).get("text", str(result)))
        encoded = result["result"].get("value")
        return json.loads(encoded) if encoded is not None else None

    def navigate(self, url):
        if self.eval("location.href") != "about:blank":
            self.protocol.call("browsingContext.navigate", {
                "context": self.context, "url": "about:blank", "wait": "complete",
            })
        self.protocol.call("browsingContext.navigate", {
            "context": self.context, "url": url, "wait": "complete",
        })

    def pages(self):
        pages = self.protocol.call("browsingContext.getTree", {})["contexts"]
        return [{"id": page["context"], "url": page["url"]} for page in pages]

    def view(self, page):
        return Firefox(self.protocol, page["id"])

    def key(self, key, ctrl=False):
        value = {"ArrowRight": "\ue014", "ArrowLeft": "\ue012",
                 "Home": "\ue011", "End": "\ue010"}.get(key, key)
        actions = [{"type": "keyDown", "value": value}, {"type": "keyUp", "value": value}]
        if ctrl:
            actions = [{"type": "keyDown", "value": "\ue009"}, *actions,
                       {"type": "keyUp", "value": "\ue009"}]
        self.protocol.call("input.performActions", {
            "context": self.context,
            "actions": [{"type": "key", "id": "keyboard", "actions": actions}],
        })

    def click(self, element_id):
        point = self.eval("(()=>{const r=document.getElementById(" + json.dumps(element_id) +
                          ").getBoundingClientRect(); return {x:Math.round(r.left+r.width/2),"
                          "y:Math.round(r.top+r.height/2)}})()")
        self.protocol.call("input.performActions", {
            "context": self.context,
            "actions": [{"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"},
                         "actions": [{"type": "pointerMove", **point},
                                     {"type": "pointerDown", "button": 0},
                                     {"type": "pointerUp", "button": 0}]}],
        })

    def capture(self, path):
        import base64
        result = self.protocol.call("browsingContext.captureScreenshot", {"context": self.context})
        path.write_bytes(base64.b64decode(result["data"]))

    def close_page(self):
        self.protocol.call("browsingContext.close", {"context": self.context})

    def close(self):
        pass


@contextmanager
def browser(kind, executable, directory):
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    profile = directory / (kind + "-profile")
    profile.mkdir()
    flags = [executable, "--remote-debugging-port=" + str(port)]
    if kind == "chrome":
        flags += ["--headless=new", "--user-data-dir=" + str(profile),
                  "--no-first-run", "--no-default-browser-check",
                  "--disable-background-networking", "--window-size=1480,1080", "about:blank"]
    else:
        flags += ["--headless", "--no-remote", "--profile", str(profile), "about:blank"]
    protocol = view = None
    with (directory / (kind + ".log")).open("w") as log:
        process = subprocess.Popen(flags, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(kind + " exited during startup; see " + str(log.name))
                try:
                    if kind == "chrome":
                        target = next(page for page in json_pages(port) if page.get("type") == "page")
                        view = Chrome(target["webSocketDebuggerUrl"], port, target["id"])
                    else:
                        protocol = Bidi("ws://127.0.0.1:" + str(port) + "/session")
                        contexts = protocol.call("browsingContext.getTree", {})["contexts"]
                        view = Firefox(protocol, contexts[0]["context"])
                    break
                except (OSError, ValueError, StopIteration, websocket.WebSocketException):
                    time.sleep(0.2)
            if view is None:
                raise RuntimeError(kind + " did not expose its local automation endpoint")
            yield view
        finally:
            try:
                for connection in (view, protocol):
                    if connection:
                        with suppress(Exception):
                            connection.close()
            finally:
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=8)


def ready(view, page, theme):
    wait_rendered(view, page, theme)
    error = view.eval("document.getElementById('error').textContent")
    assert not error, error


def check_document_resources(view):
    requests = view.eval("performance.getEntriesByType('resource').map(x=>new URL(x.name).protocol)"
                         ".filter(x=>!['data:','blob:'].includes(x))")
    assert not requests, requests
