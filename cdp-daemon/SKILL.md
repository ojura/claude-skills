---
name: cdp-daemon
description: Drive an already-running Chrome over the DevTools Protocol from scripts without spawning per-call WebSocket connections (which each trigger a permission modal). Opens one persistent CDP WebSocket, auto-presses Chrome's "Allow remote debugging?" dialog via AT-SPI, and exposes a small local HTTP API for targets, attach, eval, arbitrary CDP calls, and buffered events. Use when you need to read cookies, evaluate JS, navigate, or watch network traffic in the user's real logged-in Chrome.
---

# CDP Daemon

Talks to a running Chrome instance over the Chrome DevTools Protocol through one
long-lived WebSocket, fronted by a local HTTP API on `127.0.0.1:7799`. The point
is to connect ONCE: every fresh raw WebSocket to Chrome's debugging endpoint
pops a "Allow remote debugging?" modal, so scripts that reconnect per call spam
the user with dialogs. This daemon connects a single time, auto-presses Allow,
and then every subsequent CDP call rides the same connection with no further
prompts.

## Prerequisites

- Chrome running with remote debugging enabled. The daemon expects the
  DevTools endpoint on `127.0.0.1:43809` (edit `PORT` in `cdp_daemon.py` to
  match your launch flag, e.g. `--remote-debugging-port=43809`).
- For the auto-Allow press: `python3-gi` with the AT-SPI bindings
  (`gi.repository.Atspi`), and Chrome launched with
  `--force-renderer-accessibility` (or "Native accessibility API support"
  enabled at `chrome://accessibility`). Without renderer accessibility the
  Allow dialog is invisible to AT-SPI and `clear_modals.py` aborts with a clear
  diagnostic instead of spinning.

## Start

```bash
python3 cdp_daemon.py & disown
curl 127.0.0.1:7799/status        # confirm connected
```

On connect it spawns `clear_modals.py --wait`, which polls the AT-SPI tree for
Chrome's Allow button(s) and presses every match (Chrome exposes two
push-button nodes under the same alert; pressing only the first is unreliable).

## HTTP API

| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET  | `/targets` | | `Target.getTargets` targetInfos array |
| POST | `/attach` | `{targetId}` | `{sessionId}` |
| POST | `/eval` | `{sessionId, expression, [returnByValue=true], [awaitPromise=false]}` | `Runtime.evaluate` result |
| POST | `/cdp` | `{method, [params], [sessionId], [timeout]}` | raw CDP response |
| GET  | `/events` | `?since=N&method=substr&limit=N` | buffered CDP events |
| GET  | `/status` | | `{connected, pending, events_buffered, log_tail, ...}` |
| POST | `/shutdown` | | exits the daemon |

`/events` is a rolling buffer (last 10k) of every CDP event with no `id`
(`Network.*`, `Runtime.*`, etc.). Enable the relevant CDP domain first via
`/cdp` (e.g. `Network.enable` on a session), then poll `/events?since=<seq>` to
stream new ones. Each event carries a monotonic `seq` for cheap incremental
reads.

## Typical use

```bash
# Find a claude.ai tab, attach, read its cookies
TID=$(curl -s 127.0.0.1:7799/targets | jq -r '.[] | select(.url|contains("claude.ai")) | .targetId' | head -1)
SID=$(curl -s -X POST 127.0.0.1:7799/attach -d "{\"targetId\":\"$TID\"}" | jq -r .sessionId)
curl -s -X POST 127.0.0.1:7799/cdp -d "{\"method\":\"Network.enable\",\"sessionId\":\"$SID\"}"
curl -s -X POST 127.0.0.1:7799/eval -d "{\"sessionId\":\"$SID\",\"expression\":\"location.href\"}"
```

## Files

- `cdp_daemon.py` - the daemon. Hand-rolled WebSocket framing (no external WS
  library), single reader thread demultiplexing responses by id and buffering
  events, HTTP server for the API.
- `clear_modals.py` - AT-SPI presser for Chrome's Allow dialog. Runnable
  standalone: bare invocation does a single scan-and-press, `--wait` polls until
  it presses at least one button or hits a 60s deadline. The daemon depends on
  it living alongside; if you relocate it, update `PRESSER` in `cdp_daemon.py`.

## Notes

- Auth and identity come entirely from the Chrome instance you attach to. The
  daemon does not handle credentials; it speaks CDP to whatever Chrome is
  already logged into.
- Keep the daemon to one instance. It binds `127.0.0.1:7799` and owns the single
  CDP socket.
- This is Linux/AT-SPI specific for the auto-Allow path. On other platforms the
  CDP machinery still works, but you handle the Allow dialog yourself (or launch
  Chrome so it does not prompt).
