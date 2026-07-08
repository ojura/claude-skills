#!/usr/bin/env python3
"""Long-lived CDP daemon. Opens one persistent WS to Chrome at its live DevTools port (auto-pressing Allow
on connect), exposes a local HTTP API on 127.0.0.1:7799 for CDP calls.

Endpoints:
  GET  /targets                                → Target.getTargets
  POST /attach        {targetId}               → returns sessionId
  POST /eval          {sessionId, expression, [returnByValue=true], [awaitPromise=false], [timeout_seconds=15]}
                                                → Runtime.evaluate
  POST /cdp           {method, [params], [sessionId], [timeout_seconds=15]}  → arbitrary CDP method
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
Allow press runs IN-PROCESS: one dedicated thread (all AT-SPI stays on it)
imports clear_modals as a library and keeps scanning/pressing for the whole
connect attempt, streaming its findings into this log. Accessibility health is
a live diagnostic inside that loop - logged when it changes, never a reason to
stop scanning (AT-SPI visibility is transient; a one-shot check must not latch
the outcome). Each attempt holds exactly ONE WebSocket upgrade open for the
whole budget - one pending upgrade = at most one Allow dialog (no spam) - and
Chrome completes that same upgrade the instant the dialog is pressed, by the
presser or by a human. A failed attempt parks the daemon (no timer retries)
until POST /reconnect, which claude_web's _ensure_daemon sends automatically.
It never exits with a bare traceback.

clear_modals.py (alongside) stays a standalone CLI for manual/debug pressing;
the daemon imports its scan_and_press / chrome_accessibility_health directly.
"""
import socket, struct, json, sys, secrets, threading, time, collections, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"


def _discover_endpoint():
    """Read Chrome's live DevTools endpoint from DevToolsActivePort.

    Chrome rewrites that file on every launch: line 1 is the port, line 2 the
    browser-target WS path (UUID-suffixed on current Chrome, where the bare
    /devtools/browser path 404s). Falls back to the legacy hardcoded endpoint
    when the file is missing.
    """
    try:
        lines = open(os.path.expanduser(
            "~/.config/google-chrome/DevToolsActivePort")).read().split()
        return int(lines[0]), lines[1]
    except Exception:
        return 43809, "/devtools/browser"


PORT, PATH = _discover_endpoint()
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

def _ws_req():
    """WebSocket upgrade request for Chrome's CURRENT browser-level endpoint.

    Built per call (not at import) because connect_once re-reads
    DevToolsActivePort on every attempt - Chrome rewrites port+path on restart."""
    return (
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
# In-process presser: ONE dedicated thread owns every AT-SPI call for the
# daemon's lifetime (clear_modals imported as a library, not subprocessed, so
# its findings land in this log). It parks on press_wanted between connect
# attempts; while armed it scans/presses Chrome's Allow/Restore dialogs and
# logs its diagnosis on change. Accessibility health is a live diagnostic
# INSIDE the loop - never an up-front reason to skip pressing, because AT-SPI
# visibility is transient (a one-shot check used to latch "not visible" here
# and disable auto-press for the whole attempt).
press_wanted = threading.Event()
presser_beat = [0.0]   # wall time of the presser's last live lap (hang detection)


def presser_main():
    try:
        sys.path.insert(0, os.path.dirname(PRESSER))
        import clear_modals
    except Exception as e:
        log(f"presser: AT-SPI unavailable ({type(e).__name__}: {e}) - auto-press disabled; "
            "click Allow manually when the dialog appears")
        return
    last_diag, laps = None, 0
    while True:
        if not press_wanted.is_set():
            last_diag, laps = None, 0
            press_wanted.wait()
        presser_beat[0] = time.time()
        try:
            n = clear_modals.scan_and_press()
            if n:
                log(f"presser: pressed {n} button(s)")
                time.sleep(0.5)   # brief lap gap; loop again for straggler dialogs
                continue
            if laps % 10 == 0:    # diagnose every ~5s, log only when the diagnosis changes
                found, renderer = clear_modals.chrome_accessibility_health()
                diag = ("watching for the Allow dialog" if (found and renderer) else
                        "Chrome not visible via AT-SPI right now (locked screen / a11y "
                        "bridge asleep?) - still scanning; a manual Allow also lands"
                        if not found else
                        "Chrome renderer accessibility looks OFF - the dialog may be "
                        "invisible to AT-SPI; enable chrome://accessibility or click "
                        "Allow manually")
                if diag != last_diag:
                    log("presser: " + diag)
                    last_diag = diag
        except Exception as e:
            log(f"presser: {type(e).__name__}: {e}")
        laps += 1
        time.sleep(0.5)


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
        s.sendall(_ws_req())
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
    """Drive one connect attempt to completion. Returns True if connected.

    Exactly ONE WebSocket upgrade is held open for the whole budget: one
    pending upgrade = at most one Allow dialog (the anti-spam mechanism), and
    Chrome completes that same upgrade the instant the dialog is pressed -
    whether by the in-process presser (armed for the duration) or a human
    click. No branching on a health snapshot: the presser scans regardless
    and reports what it actually sees through the log."""
    global cdp_sock, cdp_leftover, PORT, PATH
    PORT, PATH = _discover_endpoint()   # re-read: Chrome rewrites these on restart
    set_state("connecting", "opening CDP...")
    set_state("waiting_for_allow",
              f"holding ONE CDP upgrade open (= at most one Allow dialog) for up to {budget}s; "
              "the in-process presser is scanning for the dialog, and a manual Allow click "
              "lands on this same upgrade. Presser diagnostics stream into the log.")
    press_wanted.set()
    try:
        kind, *rest = ws_upgrade(timeout=max(5, budget))   # one held attempt = one dialog
    finally:
        press_wanted.clear()
    if kind == "ok":
        sock, leftover = rest
        sock.settimeout(None)
        cdp_sock, cdp_leftover = sock, leftover
        set_state("connected", "CDP connected.")
        return True
    if kind == "noport":
        set_state("no_chrome",
                  f"ALERT: cannot reach Chrome DevTools at {HOST}:{PORT} ({rest[0]}). "
                  f"Enable remote debugging in the already-running Chrome via its 'Allow remote debugging?' prompt; --remote-debugging-port does not work for the default/main user profile.")
        return False
    hung = (" NOTE: the presser thread looks inactive (AT-SPI hang?) - see log."
            if time.time() - presser_beat[0] > min(60, budget) else "")
    set_state("failed",
              f"Remote debugging was not allowed ({rest[0] if rest else kind}).{hung} "
              "POST /reconnect to retry (claude_web's _ensure_daemon does this automatically); "
              "the presser re-arms on retry, and a manual Allow also lands.")
    return False


def connection_manager():
    """Keep a live CDP socket: connect, hold until it drops, then reconnect.

    Runs forever in a thread so the HTTP API (started first) is always up. After
    a failed attempt it backs off and retries, so fixing accessibility / clicking
    Allow later self-heals without a restart."""
    while True:
        if connect_once():
            # A /reconnect kick issued before or during this attempt is satisfied
            # by it - clearing here stops a stale flag from tearing down the fresh
            # socket and popping a surplus Allow dialog.
            reconnect_now.clear()
            disconnected.clear()
            threading.Thread(target=reader_thread, daemon=True).start()
            # Block until the socket drops or a manual /reconnect is requested.
            while not disconnected.wait(timeout=1):
                if reconnect_now.is_set():
                    break
            reconnect_now.clear()
        elif cdp_state == "no_chrome":
            # Chrome not up yet: poll quietly (this pops no Allow dialog) until it
            # appears or a /reconnect is requested.
            reconnect_now.wait(timeout=20)
            reconnect_now.clear()
        else:
            # Connect needs a human (manual Allow click / enable accessibility). Do
            # NOT retry on a timer - that is what popped repeated dialogs. Go idle and
            # wait for an explicit POST /reconnect (or /shutdown).
            log("idle: connect needs a manual Allow; waiting for POST /reconnect (no auto-retry).")
            reconnect_now.wait()
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
                     sessionId=sid, timeout_seconds=10)
        res = ((r.get("result", {}) or {}).get("result", {}) or {}).get("value")
    except Exception as e:
        res = f"inject-err:{e}"
    try:
        cdp_call("Runtime.runIfWaitingForDebugger", {}, sessionId=sid, timeout_seconds=5)
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
            def _resume():
                try:
                    cdp_call("Runtime.runIfWaitingForDebugger", {}, sessionId=sid, timeout_seconds=5)
                except Exception:
                    pass   # best-effort resume of an unrelated paused target; ignore if it vanished
            threading.Thread(target=_resume, daemon=True).start()
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


class CDPError(Exception):
    """A CDP command returned a protocol-level `error` (JSON-RPC style): Chrome
    answered but rejected the command (stale/unknown sessionId, detached target,
    bad method/params). Distinct from a transport failure (TimeoutError / socket
    drop). Carries the raw error object so callers can read code/message/data."""
    def __init__(self, error):
        self.error = error
        super().__init__(f"CDP error {error.get('code')}: {error.get('message')}")


def cdp_call(method, params=None, sessionId=None, timeout_seconds=15):
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
    if not ev.wait(timeout_seconds):
        pending.pop(mid, None)
        raise TimeoutError(f"{method} timed out after {timeout_seconds}s")
    pending.pop(mid, None)
    if ev.response is None:
        raise RuntimeError(f"{method} failed: CDP socket dropped mid-call (state={cdp_state})")
    if ev.response.get("error"):  # Chrome rejected the command (vs transport failure above)
        raise CDPError(ev.response["error"])
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
        except CDPError as e:
            self._send(500, {"error": e.error})   # structured CDP error object preserved
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

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
                r = cdp_call("Runtime.evaluate", params, sessionId=body["sessionId"],
                             timeout_seconds=body.get("timeout_seconds", 15))
                self._send(200, r.get("result", {}))
            elif self.path == "/cdp":
                r = cdp_call(body["method"], body.get("params"),
                             sessionId=body.get("sessionId"),
                             timeout_seconds=body.get("timeout_seconds", 15))
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
        except CDPError as e:
            self._send(500, {"error": e.error})   # structured CDP error object preserved
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    # HTTP API FIRST (in a thread) so /status is always answerable - even while
    # connecting or after a connect failure. The daemon never dies silently.
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("127.0.0.1", DAEMON_PORT), Handler).serve_forever(),
        daemon=True).start()
    log(f"HTTP API on http://127.0.0.1:{DAEMON_PORT}")
    # In-process presser: parks between connect attempts, armed by connect_once.
    threading.Thread(target=presser_main, daemon=True).start()
    # Connection manager owns the single CDP socket and reconnects on drop.
    connection_manager()
