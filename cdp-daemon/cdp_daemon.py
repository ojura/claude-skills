#!/usr/bin/env python3
"""Long-lived CDP daemon. Opens one persistent WS to Chrome at 43809 (auto-pressing Allow
on connect), exposes a local HTTP API on 127.0.0.1:7799 for CDP calls.

Endpoints:
  GET  /targets                                → Target.getTargets
  POST /attach        {targetId}               → returns sessionId
  POST /eval          {sessionId, expression, [returnByValue=true], [awaitPromise=false]}
                                                → Runtime.evaluate
  POST /cdp           {method, [params], [sessionId]}  → arbitrary CDP method
  GET  /events?since=N&method=substr&limit=N   → buffered CDP events (Network.*, Runtime.*, etc.)
  GET  /status                                 → daemon status (state, diag, connected, log_tail)
  POST /reconnect                              → force a fresh connect attempt
  POST /autohook      {urlSubstr, script}|{clear:true}  → inject script into matching targets at attach
                                                          (re-registering same urlSubstr REPLACES, never stacks a duplicate)
  GET  /autohooked                             → recent auto-hook injections (sid, url, result)
  POST /shutdown                               → exit cleanly

Auto-hook: pair with Target.setAutoAttach{waitForDebuggerOnStart, filter incl.
shared_worker, flatten} on the BROWSER session. A COLD shared worker pauses at
start (Chrome M143+); on Target.attachedToTarget the reader thread injects the
script then runIfWaitingForDebugger to resume, so the hook lands before the
worker's first line. Only PAUSED (waitingForDebugger) targets are injected - a
busy worker never services eval and would stall the socket to timeout; matching
paused targets get the script, non-matching paused ones are just resumed.

Start:  python3 cdp_daemon.py & disown
Test:   curl 127.0.0.1:7799/status | jq

Connection is self-complete and fails loudly: the HTTP API comes up FIRST, so
/status is always answerable even while (re)connecting or after a failure. The
connect path diagnoses *why* the auto-press can't click Chrome's "Allow remote
debugging?" dialog (almost always: renderer accessibility is OFF, so the dialog
is invisible to AT-SPI), tells the user exactly what to do, and keeps
re-pressing + retrying so a manual Allow (or flipping on accessibility mid-wait)
also lands. It never exits with a bare traceback.

Depends on clear_modals.py (alongside) for the AT-SPI Allow press + the
accessibility self-check.
"""
import socket, struct, json, sys, subprocess, secrets, threading, time, collections, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT, PATH = "127.0.0.1", 43809, "/devtools/browser"
DAEMON_PORT = 7799
PRESSER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clear_modals.py")

# === CDP connection (single global socket + lock) ===
cdp_sock = None
cdp_leftover = b""
cdp_lock = threading.Lock()   # serializes socket writes
id_lock = threading.Lock()    # serializes id allocation + pending registration (threaded HTTP)
next_id = [0]
pending = {}  # id -> threading.Event with .response attribute
log_lines = []

# Event buffer: all CDP events (messages without `id`) are appended here.
events = collections.deque(maxlen=10000)
events_lock = threading.Lock()
event_seq = [0]

# Connection state, surfaced via /status so a failed/odd connect is always
# observable rather than a dead process with a bare traceback.
cdp_state = "init"   # init|connecting|waiting_for_allow|connected|no_chrome|failed|disconnected
cdp_diag = ""        # human-readable explanation of the current state
disconnected = threading.Event()
reconnect_now = threading.Event()

# Auto-hook: inject a script into a target the instant it attaches PAUSED at start
# (arm Target.setAutoAttach{waitForDebuggerOnStart} on the browser session), so a
# worker is instrumented before its first line runs; the reader thread injects then
# resumes. Only paused (waitingForDebugger) targets are injected: a busy worker
# never services Runtime.evaluate and would stall the single socket to timeout.
autohooks = []                              # [{"urlSubstr": str, "script": str}]
autohook_lock = threading.Lock()
autohooked = collections.deque(maxlen=80)   # recent {sid, url, result} for observability

# Pre-built WebSocket upgrade request to Chrome's browser-level DevTools endpoint.
WS_REQ = (
    f"GET {PATH} HTTP/1.1\r\n"
    "Host: localhost\r\n"
    "User-Agent: curl/8.5.0\r\n"
    "Accept: */*\r\n"
    "Connection: Upgrade\r\n"
    "Upgrade: websocket\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
).encode()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    log_lines.append(line)
    if len(log_lines) > 500: log_lines.pop(0)


def set_state(state, diag=""):
    """Record connection state + a human diagnosis (surfaced via /status and logged)."""
    global cdp_state, cdp_diag
    cdp_state = state
    if diag:
        cdp_diag = diag
        log(diag)


# === Connect + Allow flow ===
def press_allow_once():
    """Spawn the AT-SPI presser (clear_modals.py --wait). Returns the Popen."""
    return subprocess.Popen(["/usr/bin/python3", PRESSER, "--wait"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def accessibility_health():
    """(chrome_found, renderer_a11y) via clear_modals, or (None, None) if unavailable.

    Lets the daemon explain *why* the auto-press will fail up front instead of
    silently waiting out a timeout."""
    try:
        sys.path.insert(0, os.path.dirname(PRESSER))
        import clear_modals
        return clear_modals.chrome_accessibility_health()
    except Exception as e:
        log(f"(accessibility self-check unavailable: {e})")
        return (None, None)


def ws_upgrade(timeout):
    """One connect + WS-upgrade attempt to Chrome's /devtools/browser endpoint.

    Returns one of:
      ("ok", sock, leftover_bytes)  upgraded (101)
      ("noport", errmsg)            devtools port unreachable (Chrome not up with the flag)
      ("denied", statusline)        HTTP response that wasn't 101 (e.g. 403 = not allowed yet)
      ("timeout", msg)              no response in time (Allow dialog up, not yet clicked)
      ("error", msg)
    """
    try:
        s = socket.create_connection((HOST, PORT), timeout=10)
    except OSError as e:
        return ("noport", str(e))
    s.settimeout(timeout)
    try:
        s.sendall(WS_REQ)
        buf = b""
        while b"\r\n\r\n" not in buf:
            ch = s.recv(4096)
            if not ch:
                s.close()
                return ("denied", "socket closed before headers")
            buf += ch
        status = buf.split(b"\r\n", 1)[0].decode()
        if "101" in status:
            return ("ok", s, buf[buf.find(b"\r\n\r\n") + 4:])
        s.close()
        return ("denied", status)
    except socket.timeout:
        s.close()
        return ("timeout", "no response (Allow not granted yet)")
    except Exception as e:
        try: s.close()
        except Exception: pass
        return ("error", str(e))


def connect_once(budget=150):
    """Drive the connect + Allow flow to completion. Returns True if connected.

    Self-complete: diagnoses an auto-press failure up front, keeps re-pressing and
    retrying so a *manual* Allow (or enabling accessibility mid-wait) also lands,
    and on giving up records a clear reason in /status instead of crashing."""
    global cdp_sock, cdp_leftover
    set_state("connecting", "opening CDP...")
    presser = press_allow_once()
    log(f"presser pid={presser.pid}")

    found, renderer = accessibility_health()
    if found is False:
        set_state("waiting_for_allow",
                  "ACTION REQUIRED: Chrome is not visible via AT-SPI, so the 'Allow remote "
                  "debugging?' dialog cannot be auto-clicked. Click Allow in Chrome manually.")
    elif renderer is False:
        set_state("waiting_for_allow",
                  "ACTION REQUIRED: auto-press UNAVAILABLE - Chrome renderer accessibility is OFF, "
                  "so the 'Allow remote debugging?' dialog is invisible to AT-SPI. FIX: click Allow "
                  "in Chrome now, or enable 'Native accessibility API support' at "
                  "chrome://accessibility (or relaunch with --force-renderer-accessibility). "
                  "Retrying until allowed.")
    elif renderer:
        log("accessibility OK - auto-pressing Allow.")

    deadline = time.time() + budget
    next_press = time.time() + 15
    last = None
    while time.time() < deadline:
        # Re-press periodically: if accessibility gets switched on mid-wait, the
        # next presser run will click the dialog automatically.
        if time.time() >= next_press:
            if presser.poll() is not None:
                presser = press_allow_once()
            next_press = time.time() + 15

        kind, *rest = ws_upgrade(timeout=min(20, max(2, deadline - time.time())))
        if kind == "ok":
            sock, leftover = rest
            sock.settimeout(None)
            cdp_sock, cdp_leftover = sock, leftover
            try:
                out, _ = presser.communicate(timeout=1)
                if out: log(f"presser: {out.strip()[:200]}")
            except Exception:
                try: presser.terminate()
                except Exception: pass
            set_state("connected", "CDP connected.")
            return True
        if kind == "noport":
            set_state("no_chrome",
                      f"ALERT: cannot reach Chrome DevTools at {HOST}:{PORT} ({rest[0]}). "
                      f"Launch Chrome with --remote-debugging-port={PORT}.")
            time.sleep(3)
            continue
        msg = rest[0] if rest else kind
        if msg != last:
            last = msg
            tail = "" if cdp_state == "waiting_for_allow" else " - click Allow if a dialog is shown"
            log(f"not allowed yet ({kind}: {msg}){tail}")
        time.sleep(2)

    set_state("failed",
              "ALERT: gave up after %ds - remote debugging was never allowed. The auto-presser "
              "could not click Allow (likely renderer accessibility OFF) and no manual Allow was "
              "detected. Enable accessibility or click Allow, then POST /reconnect (or just wait; "
              "it keeps retrying)." % budget)
    return False


def connection_manager():
    """Keep a live CDP socket: connect, hold until it drops, then reconnect.

    Runs forever in a thread so the HTTP API (started first) is always up. After
    a failed attempt it backs off and retries, so fixing accessibility / clicking
    Allow later self-heals without a restart."""
    while True:
        if connect_once():
            disconnected.clear()
            threading.Thread(target=reader_thread, daemon=True).start()
            # Block until the socket drops or a manual /reconnect is requested.
            while not disconnected.wait(timeout=1):
                if reconnect_now.is_set():
                    break
            reconnect_now.clear()
        else:
            # Failed; wait a bit (or until /reconnect) before trying again.
            reconnect_now.wait(timeout=20)
            reconnect_now.clear()


# === WS framing ===
def ws_send(text):
    p = text.encode("utf-8")
    n = len(p)
    m = secrets.token_bytes(4)
    masked = bytes(b ^ m[i % 4] for i, b in enumerate(p))
    if n < 126: hdr = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536: hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else: hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    with cdp_lock:
        cdp_sock.sendall(hdr + m + masked)


def ws_recv_raw():
    """Read one frame from cdp_sock. Uses cdp_leftover. Returns text or None on close/ctl."""
    global cdp_leftover

    def rd(n):
        global cdp_leftover
        while len(cdp_leftover) < n:
            ch = cdp_sock.recv(65536)
            if not ch: raise EOFError
            cdp_leftover += ch
        out, cdp_leftover = cdp_leftover[:n], cdp_leftover[n:]
        return out

    b1, b2 = rd(2)
    opcode = b1 & 0x0F
    plen = b2 & 0x7F
    if plen == 126: plen = struct.unpack("!H", rd(2))[0]
    elif plen == 127: plen = struct.unpack("!Q", rd(8))[0]
    masked = b2 & 0x80
    payload = rd(plen)
    if masked:
        m = rd(4)
        payload = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8: return None  # close
    if opcode in (0x9, 0xA): return ""  # ping/pong
    return payload.decode("utf-8", errors="replace")


def _run_autohook(sid, url, script):
    """Inject `script` into a freshly-attached target ASAP, then resume it if it
    was paused. Runs in its own thread so the reader keeps demuxing (cdp_call
    would otherwise deadlock the reader waiting on its own response)."""
    res = None
    try:
        r = cdp_call("Runtime.evaluate", {"expression": script, "returnByValue": True},
                     sessionId=sid, timeout=10)
        res = ((r.get("result", {}) or {}).get("result", {}) or {}).get("value")
    except Exception as e:
        res = f"inject-err:{e}"
    try:
        cdp_call("Runtime.runIfWaitingForDebugger", {}, sessionId=sid, timeout=5)
    except Exception:
        pass
    autohooked.append({"sid": sid, "url": (url or "")[:90], "result": res})
    log(f"autohook -> {(url or '')[:48]} : {str(res)[:48]}")


def _on_attached(params):
    """On Target.attachedToTarget for a target PAUSED at start (waitingForDebugger=true),
    inject any matching autohook then resume it. Targets that are already running
    (not waiting) are ignored: injecting then is too late to beat their init AND each
    Runtime.evaluate into a busy worker blocks for the full timeout, which - multiplied
    across every pre-existing worker when a broad filter is armed - wedges the single
    CDP socket. Non-matching paused targets are resumed so a browser-wide
    waitForDebuggerOnStart does not hang unrelated workers."""
    try:
        sid = params.get("sessionId")
        url = (params.get("targetInfo") or {}).get("url", "")
        if not sid or not params.get("waitingForDebugger"):
            return
        with autohook_lock:
            hooks = list(autohooks)
        matched = False
        for h in hooks:
            if h["urlSubstr"] in url:
                matched = True
                threading.Thread(target=_run_autohook, args=(sid, url, h["script"]), daemon=True).start()
        if not matched:
            threading.Thread(
                target=lambda: cdp_call("Runtime.runIfWaitingForDebugger", {}, sessionId=sid, timeout=5),
                daemon=True).start()
    except Exception as e:
        log(f"_on_attached err: {e}")


def reader_thread():
    """Demux CDP responses (by id) and buffer events. On socket drop, clear the
    connection and signal the manager to reconnect instead of dying silently."""
    global cdp_sock
    try:
        while True:
            text = ws_recv_raw()
            if text is None:
                log("CDP closed by server")
                break
            if not text: continue
            obj = json.loads(text)
            mid = obj.get("id")
            if mid is not None and mid in pending:
                ev = pending[mid]
                ev.response = obj
                ev.set()
            elif obj.get("method"):
                with events_lock:
                    event_seq[0] += 1
                    events.append({
                        "seq": event_seq[0],
                        "ts": time.time(),
                        "method": obj["method"],
                        "params": obj.get("params", {}),
                        "sessionId": obj.get("sessionId"),
                    })
                if obj["method"] == "Target.attachedToTarget" and autohooks:
                    _on_attached(obj.get("params", {}))
    except EOFError:
        log("CDP socket EOF in reader")
    except Exception as e:
        log(f"reader err: {e}")
    finally:
        cdp_sock = None
        # Fail any in-flight calls so callers get a clear error, not a hang.
        for ev in list(pending.values()):
            try: ev.set()
            except Exception: pass
        set_state("disconnected", "CDP socket dropped; reconnecting.")
        disconnected.set()


def cdp_call(method, params=None, sessionId=None, timeout=15):
    if cdp_sock is None:
        raise RuntimeError(f"CDP not connected (state={cdp_state}): {cdp_diag or 'connecting'}")
    ev = threading.Event()
    ev.response = None
    with id_lock:
        mid = next_id[0]
        next_id[0] += 1
        pending[mid] = ev
    msg = {"id": mid, "method": method, "params": params or {}}
    if sessionId: msg["sessionId"] = sessionId
    ws_send(json.dumps(msg))
    if not ev.wait(timeout):
        pending.pop(mid, None)
        raise TimeoutError(f"{method} timed out after {timeout}s")
    pending.pop(mid, None)
    if ev.response is None:
        raise RuntimeError(f"{method} failed: CDP socket dropped mid-call (state={cdp_state})")
    return ev.response


# === HTTP server ===
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *a): pass

    def do_GET(self):
        try:
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/targets":
                r = cdp_call("Target.getTargets")
                self._send(200, r.get("result", {}).get("targetInfos", []))
            elif u.path == "/status":
                self._send(200, {
                    "state": cdp_state,
                    "diag": cdp_diag,
                    "connected": cdp_sock is not None,
                    "pending": len(pending),
                    "next_id": next_id[0],
                    "events_buffered": len(events),
                    "event_seq": event_seq[0],
                    "log_tail": log_lines[-20:],
                })
            elif u.path == "/events":
                since = int(qs.get("since", ["0"])[0])
                method_filter = qs.get("method", [None])[0]
                limit = int(qs.get("limit", ["1000"])[0])
                with events_lock:
                    out = [e for e in events if e["seq"] > since
                           and (method_filter is None or method_filter in e["method"])]
                self._send(200, out[-limit:])
            elif u.path == "/autohooked":
                self._send(200, list(autohooked))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/attach":
                r = cdp_call("Target.attachToTarget", {"targetId": body["targetId"], "flatten": True})
                self._send(200, {"sessionId": r["result"]["sessionId"]})
            elif self.path == "/eval":
                params = {
                    "expression": body["expression"],
                    "returnByValue": body.get("returnByValue", True),
                    "awaitPromise": body.get("awaitPromise", False),
                }
                r = cdp_call("Runtime.evaluate", params, sessionId=body["sessionId"])
                self._send(200, r.get("result", {}))
            elif self.path == "/cdp":
                r = cdp_call(body["method"], body.get("params"),
                             sessionId=body.get("sessionId"),
                             timeout=body.get("timeout", 15))
                self._send(200, r)
            elif self.path == "/reconnect":
                reconnect_now.set()
                self._send(200, {"ok": True, "state": cdp_state})
            elif self.path == "/autohook":
                # Register a script to inject into every target whose URL contains
                # urlSubstr, the instant it attaches. Registering REPLACES any existing
                # rule with the same urlSubstr (re-registering updates in place and never
                # stacks a stale duplicate, which the already-installed hook would
                # otherwise win via `if(globalThis.__W) return 'already'`). Distinct
                # urlSubstrs coexist; {clear:true} wipes all rules.
                with autohook_lock:
                    if body.get("clear"):
                        autohooks.clear()
                    if body.get("script") and body.get("urlSubstr") is not None:
                        autohooks[:] = [h for h in autohooks if h["urlSubstr"] != body["urlSubstr"]]
                        autohooks.append({"urlSubstr": body["urlSubstr"], "script": body["script"]})
                    count = len(autohooks)
                self._send(200, {"ok": True, "count": count})
            elif self.path == "/shutdown":
                self._send(200, {"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.1), os._exit(0))).start()
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    # HTTP API FIRST (in a thread) so /status is always answerable - even while
    # connecting or after a connect failure. The daemon never dies silently.
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("127.0.0.1", DAEMON_PORT), Handler).serve_forever(),
        daemon=True).start()
    log(f"HTTP API on http://127.0.0.1:{DAEMON_PORT}")
    # Connection manager owns the single CDP socket and reconnects on drop.
    connection_manager()
