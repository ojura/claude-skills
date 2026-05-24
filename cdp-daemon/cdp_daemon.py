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
  GET  /status                                 → daemon status (connected, pending, log_tail)
  POST /shutdown                               → exit cleanly

Start:  python3 ~/.local/bin/cdp_daemon.py & disown
Test:   curl 127.0.0.1:7799/targets | jq

Depends on ~/.local/bin/clear_modals.py for the AT-SPI Allow press.
"""
import socket, struct, json, sys, subprocess, secrets, threading, time, collections, os
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST, PORT, PATH = "127.0.0.1", 43809, "/devtools/browser"
DAEMON_PORT = 7799
PRESSER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clear_modals.py")

# === CDP connection (single global socket + lock) ===
cdp_sock = None
cdp_leftover = b""
cdp_lock = threading.Lock()
next_id = [0]
pending = {}  # id -> threading.Event with .response attribute
log_lines = []

# Event buffer: all CDP events (messages without `id`) are appended here.
events = collections.deque(maxlen=10000)
events_lock = threading.Lock()
event_seq = [0]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    log_lines.append(line)
    if len(log_lines) > 500: log_lines.pop(0)


def press_allow_once():
    """Spawn presser subprocess, returns the Popen object (caller waits/timeouts)."""
    p = subprocess.Popen(["/usr/bin/python3", PRESSER, "--wait"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p


def open_cdp():
    global cdp_sock, cdp_leftover
    presser = press_allow_once()
    log(f"presser pid={presser.pid}")
    s = socket.create_connection((HOST, PORT), timeout=60)
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    req = (
        f"GET {PATH} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "User-Agent: curl/8.5.0\r\n"
        "Accept: */*\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n\r\n"
    ).encode()
    s.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf:
        ch = s.recv(4096)
        if not ch: raise RuntimeError("socket closed before headers")
        buf += ch
    status = buf.split(b"\r\n", 1)[0].decode()
    if "101" not in status: raise RuntimeError(f"WS upgrade failed: {status}")
    log(f"CDP connected ({status})")
    try:
        out, _ = presser.communicate(timeout=2)
        if out: log(f"presser: {out.strip()[:200]}")
    except Exception:
        presser.terminate()
    s.settimeout(None)
    cdp_sock = s
    cdp_leftover = buf[buf.find(b"\r\n\r\n") + 4:]


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


def reader_thread():
    while True:
        try:
            text = ws_recv_raw()
            if text is None:
                log("CDP closed by server")
                return
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
        except EOFError:
            log("CDP socket EOF in reader")
            return
        except Exception as e:
            log(f"reader err: {e}")
            return


def cdp_call(method, params=None, sessionId=None, timeout=15):
    mid = next_id[0]
    next_id[0] += 1
    msg = {"id": mid, "method": method, "params": params or {}}
    if sessionId: msg["sessionId"] = sessionId
    ev = threading.Event()
    ev.response = None
    pending[mid] = ev
    ws_send(json.dumps(msg))
    if not ev.wait(timeout):
        del pending[mid]
        raise TimeoutError(f"{method} timed out after {timeout}s")
    del pending[mid]
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
            elif self.path == "/shutdown":
                self._send(200, {"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.1), sys.exit(0))).start()
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    log("opening CDP...")
    open_cdp()
    threading.Thread(target=reader_thread, daemon=True).start()
    log(f"HTTP API on http://127.0.0.1:{DAEMON_PORT}")
    HTTPServer(("127.0.0.1", DAEMON_PORT), Handler).serve_forever()
