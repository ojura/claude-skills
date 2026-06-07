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
| POST | `/reconnect` | | force a fresh CDP connect (use if the socket wedges) |
| POST | `/autohook` | `{urlSubstr, script}` \| `{clear:true}` | inject `script` into matching targets the instant they attach |
| GET  | `/autohooked` | | recent auto-hook injections `{sid, url, result}` |
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

## Instrumenting a worker before its code runs (auto-hook)

To wrap a function (a WASM export, `crypto.subtle`, a library like tweetnacl)
*before* a worker executes, you must inject while the worker is paused at start,
then resume it. The daemon automates that race:

1. Arm pause-on-start on the BROWSER session (no `sessionId`):
   `/cdp {"method":"Target.setAutoAttach","params":{"autoAttach":true,"waitForDebuggerOnStart":true,"flatten":true,"filter":[{"type":"shared_worker"}]}}`
2. Register the hook: `/autohook {"urlSubstr":"static_resources/webworker","script":"<js>"}`.
   On each `Target.attachedToTarget` whose URL contains `urlSubstr` **and** that
   arrived paused (`waitingForDebugger:true`), the daemon `Runtime.evaluate`s the
   script then `Runtime.runIfWaitingForDebugger` to resume. Non-matching paused
   targets are resumed too, so a broad pause never strands unrelated workers.
   Re-registering the same `urlSubstr` replaces (never stacks a stale duplicate).
3. Open/create the target. The cold worker pauses, the hook lands before its
   first line, then it resumes. Read the injection result via `/autohooked`.

If the worker's library isn't defined yet at the pause (e.g. an FB-module
`require('x')`), have the injected script poll (`setInterval(...,0)`) and wrap as
soon as it appears, rather than wrapping inline.

## Gotchas (CDP target/worker instrumentation)

- **Cold vs reused workers.** A genuinely COLD shared worker pauses under
  `waitForDebuggerOnStart` (Chrome M143+). A REUSED one does not: `Target.closeTarget`
  does not evict the `SharedWorkerHost` (it lingers in `terminated_hosts_`, pinned
  by your auto-attach session's refcount, with no time-based eviction), so the next
  `new SharedWorker()` reconnects the same host and skips the pause. To force cold:
  disarm auto-attach (drops the ref), close the client page(s), let the host evict,
  then re-arm and reopen; or restart the browser (deterministic cold).
- **Only inject into PAUSED targets.** A worker stuck in synchronous WASM never
  services the inspector: `Runtime.evaluate` AND `Debugger.enable` both hang to
  their full timeout (the same calls return in ms on a responsive worker), and a
  batch of such timeouts starves the single CDP socket. The auto-hook only fires
  on `waitingForDebugger:true` targets for this reason; never `/eval` a busy worker.
- **No browser-handshake calls in a paused-start injection.** `new BroadcastChannel(...)`
  (and similar) blocks forever when constructed in a worker paused at start: the
  constructor needs a browser-process handshake the suspended task loop cannot
  complete, so the injecting `Runtime.evaluate` never returns. Defer such calls
  with `setTimeout(0)` (runs post-resume) and read captures from a global instead.
- **WASM exports can't be monkey-patched.** They are non-writable and
  non-configurable, so `defineProperty`/assignment silently fails. Wrap
  `WebAssembly.instantiate`/`instantiateStreaming`/`Instance` and return a value
  whose `.exports` is a `Proxy` (emscripten reads each export lazily on first
  call, so the Proxy `get` trap is where you wrap).
- **Do not pause dedicated workers on Chrome 148.** `setAutoAttach{waitForDebuggerOnStart,
  filter:[{type:"worker"}]}` followed by a target create/navigate SIGSEGVs the
  browser (the renderer child-pause path is not covered by crrev.com/c/7552776 as
  of .215). Pause only `shared_worker`.
- **Navigation restarts the shared worker.** `Page.reload`/`Page.navigate` resets
  the SW's JS global (same target id, fresh scope), wiping any warm-injected hook.
  Hook a cold worker via auto-hook, not a warm one you then reload.
- **Recover a crashed browser via AT-SPI.** After a Chrome crash the relaunch
  shows a "Restore" prompt; press it the same way `clear_modals.py` presses
  "Allow" (find the `Restore` push-button in the a11y tree and invoke `press`).

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
