#!/usr/bin/env python3
"""Long-lived CDP daemon. Opens one persistent WS to Chrome at its live DevTools port (auto-pressing Allow
on connect), exposes a local HTTP API on 127.0.0.1:7799 for CDP calls.

Endpoints:
  GET  /targets                                → Target.getTargets
  POST /attach        {targetId}               → returns sessionId
  POST /prepare       {sessionId, [readiness=dom], [timeout_seconds=15]} → page readiness
  POST /release       {sessionId, [timeout_seconds=15]} → release preparation
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
worker's first line. Hooks run only on paused (waitingForDebugger) targets.
Running workers can keep inspector commands pending; matching paused targets
receive the hook, and unmatched paused targets are resumed.

Start:  python3 cdp_daemon.py & disown
Test:   curl 127.0.0.1:7799/status | jq

The HTTP server starts before connection acquisition so /status can report
acquisition progress and failure. The Allow press runs in-process: one dedicated
thread (all AT-SPI stays on it)
imports clear_modals as a library and keeps scanning/pressing for the whole
connect attempt, streaming its findings into this log. Accessibility health is
a live diagnostic inside that loop - logged when it changes, never a reason to
stop scanning (AT-SPI visibility is transient; a one-shot check must not latch
the outcome). Each attempt holds exactly ONE WebSocket upgrade open for the
acquisition budget. One pending upgrade presents at most one Allow dialog.
Chrome completes it when consent is granted by the presser or a person.
A failed consent attempt waits for POST /reconnect, which claude_web's
_ensure_daemon sends when needed. Connection progress stays available in /status.

clear_modals.py (alongside) stays a standalone CLI for manual/debug pressing;
the daemon imports its scan_and_press / chrome_accessibility_health directly.
"""
import socket, struct, json, sys, secrets, threading, time, collections, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from zoneinfo import ZoneInfo

from page_preparation import PagePreparationError, PagePreparationManager, PagePreparationState

HOST = "127.0.0.1"


def _discover_endpoint():
    """Read Chrome's live DevTools endpoint from DevToolsActivePort.

    Chrome rewrites that file on every launch: line 1 is the port, line 2 the
    browser-target WS path (UUID-suffixed on current Chrome, where the bare
    /devtools/browser path 404s). Falls back to the legacy hardcoded endpoint
    when the file is missing.
    """
    try:
        with open(os.path.expanduser("~/.config/google-chrome/DevToolsActivePort")) as endpoint:
            lines = endpoint.read().split()
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

# /status reports acquisition progress and failure while the HTTP API stays up.
cdp_state = "init"   # init|connecting|waiting_for_allow|connected|no_chrome|failed|disconnected
cdp_diag = ""        # human-readable explanation of the current state
disconnected = threading.Event()
reconnect_now = threading.Event()
connection_wake = threading.Event()

# Auto-hook: inject a script into a target the instant it attaches PAUSED at start
# (arm Target.setAutoAttach{waitForDebuggerOnStart} on the browser session), so a
# worker is instrumented before its first line runs; the reader thread injects then
# resumes. Existing running workers can keep inspector calls pending, so the
# hook uses the explicit paused-start event as its injection point.
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


# Keep acquisition metadata separate from page/network CDP events. Old records
# can be evicted; sequence numbers and dropped_events make that loss explicit.
consent_lock = threading.Lock()
consent_events = collections.deque(maxlen=1024)
consent_sequence = 0
consent_attempt = 0
consent_scan = 0
CONSENT_EVENTS = {
    'attempt_started', 'presser_requested', 'upgrade_request_sent', 'upgrade_finished',
    'connection_ready', 'presser_import_started', 'presser_import_finished',
    'presser_import_failed', 'scan_started', 'scan_finished', 'scan_failed',
    'button_matched', 'native_action_started', 'native_action_finished',
    'press_action_unavailable', 'native_action_error', 'accessibility_query_error',
    'accessibility_health',
}
CONSENT_NUMBERS = {'scan_id', 'budget_seconds', 'duration_ms', 'chrome_applications',
                   'matched_buttons', 'completed_calls', 'query_errors'}


def consent_event(event, attempt_id=None, *, state=None, **metadata):
    """Record selected metadata once; derive the human log from that record."""
    global consent_sequence, cdp_state, cdp_diag
    if event not in CONSENT_EVENTS:
        raise ValueError('Unknown consent diagnostic event')
    fields = {}
    for key, value in metadata.items():
        if key in CONSENT_NUMBERS and type(value) in (int, float):
            fields[key] = value
        elif key in {'native_result', 'chrome_found', 'renderer_found'} and (value is None or type(value) is bool):
            fields[key] = value
        elif key == 'button':
            fields[key] = value if value in ('Allow', 'Restore') else 'unknown'
        elif key == 'outcome' and value in {'ok', 'noport', 'denied', 'timeout', 'error'}:
            fields[key] = value
        elif key == 'error_type' and isinstance(value, str) and value.isidentifier():
            fields[key] = value[:80]
    with consent_lock:
        consent_sequence += 1
        entry = {
            'seq': consent_sequence, 'attempt_id': attempt_id, 'event': event,
            'time_zagreb': datetime.now(ZoneInfo('Europe/Zagreb')).isoformat(timespec='microseconds'),
            'monotonic_ns': time.monotonic_ns(), **fields,
        }
        consent_events.append(entry)
        message = 'consent: ' + json.dumps(entry, sort_keys=True)
        if state is not None:
            cdp_state, cdp_diag = state, message
    if event not in {'scan_started', 'scan_finished'} or fields.get('matched_buttons') or fields.get('query_errors'):
        log(message)
    return entry


def consent_snapshot():
    with consent_lock:
        return {'capacity': consent_events.maxlen,
                'dropped_events': consent_sequence - len(consent_events),
                'first_seq': consent_events[0]['seq'] if consent_events else None,
                'last_seq': consent_sequence, 'events': list(consent_events)}


def begin_consent_attempt(budget):
    global consent_attempt
    with consent_lock:
        consent_attempt += 1
        attempt_id = consent_attempt
    consent_event('attempt_started', attempt_id, state='connecting', budget_seconds=budget)
    return attempt_id


def begin_consent_scan():
    global consent_scan
    with consent_lock:
        consent_scan += 1
        attempt_id, scan_id = consent_attempt, consent_scan
    started = time.monotonic_ns()
    consent_event('scan_started', attempt_id, scan_id=scan_id)

    def report(event, **metadata):
        if event == 'scan_finished':
            metadata['duration_ms'] = (time.monotonic_ns() - started) / 1_000_000
        consent_event(event, attempt_id, scan_id=scan_id, **metadata)
    return report


def set_state(state, diag=""):
    """Record connection state and its explanation in /status and the log."""
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
    import_started = time.monotonic_ns()
    consent_event('presser_import_started')
    try:
        sys.path.insert(0, os.path.dirname(PRESSER))
        import clear_modals
    except Exception as error:
        consent_event('presser_import_failed', error_type=type(error).__name__)
        return
    consent_event('presser_import_finished',
                  duration_ms=(time.monotonic_ns() - import_started) / 1_000_000)
    last_diag, laps = None, 0
    while True:
        if not press_wanted.is_set():
            last_diag, laps = None, 0
            press_wanted.wait()
        presser_beat[0] = time.time()
        report = begin_consent_scan()
        try:
            completed = clear_modals.scan_and_press(reporter=report)
            if completed:
                time.sleep(0.5)
                continue
            if laps % 10 == 0:
                found, renderer = clear_modals.chrome_accessibility_health()
                diag = (found, renderer)
                if diag != last_diag:
                    report('accessibility_health', chrome_found=found, renderer_found=renderer)
                    last_diag = diag
        except Exception as error:
            report('scan_failed', error_type=type(error).__name__)
        laps += 1
        time.sleep(0.5)


def ws_upgrade(timeout):
    """One connect + WS-upgrade attempt to Chrome's /devtools/browser endpoint.

    Returns one of:
      ("ok", sock, leftover_bytes)  upgraded (101)
      ("noport", errmsg)            devtools port unreachable (Chrome not up with the flag)
      ("denied", statusline)        HTTP response that wasn't 101 (e.g. 403 = not allowed yet)
      ("timeout", msg)              the upgrade response wait expired
      ("error", msg)
    """
    with consent_lock:
        attempt_id = consent_attempt or None
    try:
        s = socket.create_connection((HOST, PORT), timeout=10)
    except OSError as e:
        return ("noport", str(e))
    s.settimeout(timeout)
    try:
        s.sendall(_ws_req())
        consent_event('upgrade_request_sent', attempt_id)
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
        return ("timeout", "The WebSocket upgrade did not answer within the acquisition wait")
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
    click. The presser scans throughout the attempt and reports its current
    observations through the log."""
    global cdp_sock, cdp_leftover, PORT, PATH
    PORT, PATH = _discover_endpoint()   # re-read: Chrome rewrites these on restart
    attempt_id = begin_consent_attempt(budget)
    consent_event('presser_requested', attempt_id, state='waiting_for_allow', budget_seconds=budget)
    press_wanted.set()
    try:
        kind, *rest = ws_upgrade(timeout=max(5, budget))   # one held attempt = one dialog
    finally:
        press_wanted.clear()
    consent_event('upgrade_finished', attempt_id, outcome=kind)
    if kind == "ok":
        sock, leftover = rest
        sock.settimeout(None)
        cdp_sock, cdp_leftover = sock, leftover
        consent_event('connection_ready', attempt_id, state='connected')
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
    """Connect once per attempt and finish the old reader before reconnecting.

    A failed consent attempt waits for POST /reconnect. While Chrome is absent,
    the manager checks its endpoint periodically without opening a WebSocket.
    """
    while True:
        if connect_once():
            # A reconnect request made during this attempt is satisfied by the
            # new connection, so it must not open another consent dialog.
            reconnect_now.clear()
            disconnected.clear()
            connection_wake.clear()
            reader = threading.Thread(target=reader_thread, daemon=True)
            reader.start()
            while not disconnected.is_set() and not reconnect_now.is_set():
                connection_wake.wait()
                connection_wake.clear()
            if not disconnected.is_set() and cdp_sock is not None:
                try:
                    cdp_sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            reader.join()
            reconnect_now.clear()
        elif cdp_state == "no_chrome":
            # While the endpoint is absent, check availability or accept an
            # explicit reconnect request. A refused connection cannot show a dialog.
            reconnect_now.wait(timeout=20)
            reconnect_now.clear()
        else:
            # Connect needs a human (manual Allow click / enable accessibility). Do
            # NOT retry on a timer - that is what popped repeated dialogs. Go idle and
            # wait for an explicit POST /reconnect (or /shutdown).
            log("Connection acquisition is parked; POST /reconnect starts another attempt.")
            reconnect_now.wait()
            reconnect_now.clear()


# === WS framing ===
def ws_send(text, _expected_socket=None):
    p = text.encode("utf-8")
    n = len(p)
    m = secrets.token_bytes(4)
    masked = bytes(b ^ m[i % 4] for i, b in enumerate(p))
    if n < 126: hdr = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536: hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else: hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    with cdp_lock:
        connection = cdp_sock
        if _expected_socket is not None and connection is not _expected_socket:
            raise RuntimeError("The browser debugging connection changed")
        if connection is None:
            raise RuntimeError("The browser debugging connection is unavailable")
        connection.sendall(hdr + m + masked)


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
    """Inject before resuming a worker paused at startup.

    A separate thread lets the reader deliver the CDP replies these calls await.
    """
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
    """Inject matching hooks into a target paused at startup, then resume it.

    Running targets have already passed that injection point and can keep an
    inspector call pending. Resume unmatched paused targets so a broad startup
    filter does not leave unrelated workers waiting for a debugger.
    """
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
    """Deliver CDP replies and events, then close and report this connection.

    The reader notifies the manager after closing its captured socket.
    """
    global cdp_sock
    reader_socket = cdp_sock
    try:
        while True:
            text = ws_recv_raw()
            if text is None:
                log("CDP socket reached EOF")
                break
            if not text: continue
            obj = json.loads(text)
            mid = obj.get("id")
            with id_lock:
                ev = pending.get(mid) if mid is not None else None
            if ev is not None:
                ev.response = obj
                ev.set()
            elif obj.get("method"):
                notify_preparations(obj)
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
        if reader_socket is not None:
            try:
                reader_socket.close()
            except OSError:
                pass
        cdp_sock = None
        notify_preparations({"method": "daemon.disconnected", "connection": reader_socket})
        # Fail any in-flight calls so callers get a clear error, not a hang.
        with id_lock:
            waiters = tuple(pending.values())
        for ev in waiters:
            ev.set()
        set_state("disconnected", "CDP socket dropped; reconnecting.")
        disconnected.set()
        connection_wake.set()


class CDPError(Exception):
    """A CDP command returned a protocol-level `error` (JSON-RPC style): Chrome
    answered but rejected the command (stale/unknown sessionId, detached target,
    bad method/params). Distinct from a transport failure (TimeoutError / socket
    drop). Carries the raw error object so callers can read code/message/data."""
    def __init__(self, error):
        self.error = error
        super().__init__(f"CDP error {error.get('code')}: {error.get('message')}")


def cdp_call(method, params=None, sessionId=None, timeout_seconds=15,
             _page_state=None, _expected_socket=None):
    if cdp_sock is None:
        raise RuntimeError(f"CDP not connected (state={cdp_state}): {cdp_diag or 'connecting'}")
    ev = threading.Event()
    ev.response = None
    with id_lock:
        mid = next_id[0]
        next_id[0] += 1
        pending[mid] = ev
    try:
        if _page_state is not None:
            _page_state.register(ev)
            _page_state.check()
        msg = {"id": mid, "method": method, "params": params or {}}
        if sessionId:
            msg["sessionId"] = sessionId
        if _expected_socket is None:
            ws_send(json.dumps(msg))
        else:
            ws_send(json.dumps(msg), _expected_socket=_expected_socket)
        if not ev.wait(timeout_seconds):
            raise TimeoutError(f"{method} timed out after {timeout_seconds}s")
        if _page_state is not None:
            _page_state.check()
        if ev.response is None:
            raise RuntimeError(f"{method} failed: CDP socket dropped mid-call (state={cdp_state})")
        if ev.response.get("error"):
            raise CDPError(ev.response["error"])
        return ev.response
    finally:
        with id_lock:
            pending.pop(mid, None)
        if _page_state is not None:
            _page_state.unregister(ev)


page_preparation = PagePreparationManager(
    lambda *args, **kwargs: cdp_call(*args, **kwargs),
    lambda: cdp_sock, CDPError, clock=lambda: time.monotonic())


def notify_preparations(event):
    page_preparation.observe(event)


def prepare_page(session_id, readiness="dom", timeout_seconds=15):
    return page_preparation.prepare(session_id, readiness, timeout_seconds)


def release_page(session_id, timeout_seconds=15):
    return page_preparation.release(session_id, timeout_seconds)


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
                    "prepared_pages": page_preparation.status(),
                    "browser_state_pages": page_preparation.bridge_status(),
                    "log_tail": log_lines[-20:],
                    "consent_diagnostics": consent_snapshot(),
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
            elif self.path == "/prepare":
                if not isinstance(body, dict):
                    raise PagePreparationError("invalid_request", "validation",
                                               "The request body must be an object")
                unknown = body.keys() - {"sessionId", "readiness", "timeout_seconds"}
                if unknown:
                    raise PagePreparationError("invalid_request", "validation",
                                               "Unexpected preparation fields: " + ", ".join(sorted(unknown)))
                self._send(200, prepare_page(
                    body.get("sessionId"), body.get("readiness", "dom"),
                    body.get("timeout_seconds", 15)))
            elif self.path == "/release":
                if not isinstance(body, dict):
                    raise PagePreparationError("invalid_request", "validation",
                                               "The request body must be an object")
                unknown = body.keys() - {"sessionId", "timeout_seconds"}
                if unknown:
                    raise PagePreparationError("invalid_request", "validation",
                                               "Unexpected release fields: " + ", ".join(sorted(unknown)))
                self._send(200, release_page(body.get("sessionId"),
                                             body.get("timeout_seconds", 15)))
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
                connection_wake.set()
                self._send(200, {"ok": True, "state": cdp_state})
            elif self.path == "/autohook":
                # Each URL substring has one hook. Registering it again replaces
                # that hook; distinct substrings coexist. clear removes all hooks.
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
        except PagePreparationError as e:
            code = 400 if e.kind == "invalid_request" else 500
            self._send(code, {"error": e.as_dict()})
        except CDPError as e:
            self._send(500, {"error": e.error})   # structured CDP error object preserved
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    # Binding owns the API endpoint before any Chrome acquisition can start.
    with ThreadingHTTPServer(("127.0.0.1", DAEMON_PORT), Handler) as server:
        api_thread = threading.Thread(target=server.serve_forever, daemon=True)
        api_thread.start()
        try:
            host, port = server.server_address[:2]
            log(f"HTTP API on http://{host}:{port}")
            threading.Thread(target=presser_main, daemon=True).start()
            connection_manager()
        finally:
            server.shutdown()
            api_thread.join()


if __name__ == "__main__":
    main()
