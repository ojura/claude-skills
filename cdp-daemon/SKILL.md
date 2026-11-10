---
name: cdp-daemon
description: Drive an already-running Chrome through one persistent DevTools connection, avoiding repeated debugging-permission prompts. Provides a local HTTP API for targets, page preparation, evaluation, arbitrary CDP calls, worker startup hooks and events. Use for browser state, page JavaScript, navigation, cookies or network inspection in the user's logged-in Chrome.
---

# CDP daemon

The daemon holds one Chrome DevTools WebSocket and serves a local HTTP API at
`127.0.0.1:7799`. Clients reuse that connection instead of opening another
WebSocket for each call. On Chrome's consent-controlled endpoint, new debugging
connections can raise another **Allow remote debugging?** dialog.

## Prerequisites

- Enable remote debugging in the running Chrome through `chrome://inspect`.
  Chrome 136 and later ignore `--remote-debugging-port` on the default user-data
  directory. The daemon discovers the port and browser WebSocket path from
  `~/.config/google-chrome/DevToolsActivePort`, falling back to
  `127.0.0.1:43809` when the file is unavailable.
- Automatic consent pressing requires `python3-gi` with `gi.repository.Atspi`.
  Chrome needs `--force-renderer-accessibility` or Native accessibility API
  support enabled through `chrome://accessibility`. Without that accessibility
  information, the consent button may be unavailable to AT-SPI.
- Managed page preparation additionally requires the bundled browser-side state
  extension. The browser-level `Extensions.loadUnpacked` command installs it on
  endpoints that support the operation; omit `sessionId`. The helper's ordinary
  UI Reload action then retains the registration across Chrome restarts. Read
  [page-preparation.md](page-preparation.md) for installation, verification,
  updates, recovery after a Chrome restart and the manual fallback before
  installing it or using `/prepare`.
  Raw CDP calls remain available without that extension.

### Keep native accessibility enabled across Chrome launches

The `chrome://accessibility` Native accessibility API switch repairs the current
browser process while that settings tab stays open on that page. Chrome scopes
these flags to the page's lifetime; closing it or navigating away removes its
mode contribution. Keep that tab open when using this temporary repair. This
behavior was also observed on Chrome 157; see Chromium's
[accessibility UI implementation](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/chrome/browser/ui/webui/accessibility/accessibility_ui.cc).
For subsequent desktop launches, the bundled
`google-chrome.desktop` adds `--force-renderer-accessibility` to the standard
Google Chrome launcher and its New Window and New Incognito Window actions.
It uses the existing browser and profile. On a system with
`/usr/bin/google-chrome-stable`, install the user-level override from this folder:

```bash
install -Dm644 google-chrome.desktop ~/.local/share/applications/google-chrome.desktop
update-desktop-database ~/.local/share/applications
```

Inspect an existing user override before replacing it and preserve any local
launcher customizations. The override takes effect when Chrome next starts from
that desktop entry; it does not change an already-running process. A direct
shell launch needs the same flag. Do not restart a shared browser to apply this
setting during other work. The page-preparation extension reports tab state
after CDP has connected; it does not expose or press the native Allow dialog.

## Start and connection handling

```bash
python3 cdp_daemon.py & disown
curl -s http://127.0.0.1:7799/status
```

The daemon binds its HTTP listener synchronously before logging availability,
starting the presser or acquiring a Chrome connection. An occupied API port fails
startup at that bind, so another process does not acquire Chrome without owning
the API endpoint. Startup failures close the listener. Once the server is
running, `/status` reports connection progress and failure while acquisition
continues. One dedicated presser thread imports
`clear_modals` and performs the AT-SPI calls. During acquisition it repeatedly
finds native alert/dialog containers titled **Allow remote debugging?** or
**Restore pages?**, then invokes their Allow or Restore buttons. Renderer document
subtrees are excluded, so web content cannot qualify by imitating those titles.
A browser window's Restore button is outside these prompt containers and is
excluded. All distinct intended buttons retain their traversal order because
Chrome can expose multiple nodes for one prompt. Unsupported or conflicting
prompt structures remain observable in consent diagnostics; the presser does
not fall back to clicking buttons throughout the browser. Accessibility
availability is checked throughout the attempt; an earlier failed check does
not stop later scanning.

The same thread dispatches GLib's default context while connected or parked.
libatspi installs cache-signal subscriptions on initialization, so dispatch must
continue even when consent scanning is inactive. Idle dispatch blocks until bus
traffic or an acquisition/shutdown request wakes it; it does not poll. During
acquisition the existing half-second scan interval also dispatches signals.
Disarming an acquisition cancels its interval source without closing AT-SPI.
A scan, health query or native action that blocks also stops this thread from
dispatching signals. The bus can fill its queue and reject signals during that
call. When the call returns, dispatch resumes and drains the accepted messages;
it cannot recover rejected signals. If the call never returns, the connection
remains undrained until process shutdown closes it.
The thread calls `Atspi.exit` once when it stops. A dispatch failure is recorded
as `presser_dispatch_failed` and closes that thread's AT-SPI connection. Shutdown
wakes the thread for cleanup; the existing process-exit deadline also closes its
connections if a native call has not returned.

Chrome itself requests window activation when creating its consent dialog.
The presser does not issue window-activation commands. Reusing the persistent
connection avoids repeatedly creating that native prompt.

Each acquisition attempt holds one WebSocket upgrade for about 150 seconds.
Chrome completes that pending request when consent is granted. An attempt that
expires without consent waits for `POST /reconnect`; it does not repeatedly open
consent dialogs. While Chrome's endpoint is absent, the manager periodically
checks availability. `claude_web`'s `_ensure_daemon` requests reconnection when it
finds a parked daemon.

`POST /reconnect` closes and joins the old reader before opening another
connection. Existing work sessions then need fresh attachment. Use it to repair
a connection, rather than as preparation for every call.

After changing the daemon or its supporting Python module, stop the running
instance with `POST /shutdown` and relaunch it. The process keeps executing its
loaded code until restarted. Shutdown also works during connection acquisition.
Keep `clear_modals.py` beside the daemon, as its presser imports that file.

Coordinate browser clients before a planned reset, then check `pending=0` and
`prepared_pages=[]` immediately before shutdown. These fields describe current
response waits and managed registrations. They do not identify idle clients or
raw sessions that independently enabled focus. A client process exiting does not
detach sessions it created through the daemon: Chrome's WebSocket belongs to the
daemon. Resetting that connection detaches its sessions, including legacy focus
owners. Other clients must attach fresh sessions afterward. Debuggers using
separate Chrome connections retain their sessions and require separate
coordination.

### Consent diagnostics

`/status.consent_diagnostics` records acquisition attempts, presser import,
scan start/completion, recognized Allow/Restore matches, native action calls,
accessibility observations and WebSocket upgrade completion. Each event has a
sequence number, a Zagreb wall-clock timestamp and `monotonic_ns`. Scan and action
reports retain the attempt and scan IDs captured when that scan began, including
when they finish after another acquisition starts. Every health traversal also
has `accessibility_health_started` followed by `accessibility_health_finished`
or `accessibility_health_failed`, with duration and the same captured IDs.
Unchanged health observations are recorded on every scheduled invocation, so a
pending health traversal can be distinguished from the completed button scan.
`query_errors` counts failures during application, tree, matched-label and action
interface queries; a native action failure is reported separately.

`native_action_started` precedes the AT-SPI call; `native_action_finished`
follows its return and includes Chrome's actual boolean `native_result`.
`upgrade_request_sent` follows the socket write, and `connection_ready` follows
the completed WebSocket upgrade and socket publication. These observation times
allow comparison of action calls with connection progress. A native result of
`true` reports acceptance of that action request; the separate connection event
establishes when debugging became available. The completed-call count includes
calls returning `false`, preserving the existing scan return value and polling
schedule. It is not a successful-approval count.

One producer stores these events and derives the human log entries from them.
Text-writer failures leave the structured record available and do not cancel
an upgrade or native action. `text_log_failures` and `last_text_log_error_type`
report those failures without recording exception messages.
Scan-start records are retained only in structured history. Scan-completion
records also omit the text log when their match and query-error counts are both
zero. Rejection and unsupported-structure events remain logged independently.
`button_matched` includes the recognized `prompt_kind`;
`button_rejected` and `prompt_structure_unavailable` explain exclusions with
fixed reason codes. Scan summaries count skipped document subtrees, unsupported
prompts, excluded buttons and traversal depth limits. These observations remain
available when Chrome presents an unsupported prompt; the scanner does not use
a global-button fallback. Records contain fixed event names, recognized button
names, booleans, counts, timings and exception class names. They omit other
accessibility labels, page contents, exception messages and endpoint paths.

The history retains the latest 1,024 events. A 150-second attempt with the
half-second scan interval produces about 600 scan start/completion records,
leaving room for import, health and action records during an ordinary wait.
Repeated matches or multiple attempts can evict the beginning of an attempt.
`dropped_events`, `first_seq` and `last_seq` identify that loss. The separate text
log still retains 500 lines and exposes its latest 20 through `log_tail`.

### Wait for manual consent from an agent

When consent depends on a person, background one bounded wait and let its exit
notify the agent. Use `Bash(run_in_background)` for this single completion event.
Return a nonzero exit code if connection acquisition fails, so the completion
notification distinguishes failure from success.

```bash
curl -s -X POST http://127.0.0.1:7799/reconnect >/dev/null
for i in $(seq 1 240); do
  if curl -s --max-time 2 http://127.0.0.1:7799/status | grep -q '"connected": true'; then
    printf 'Connected after %s seconds\n' "$i"
    exit 0
  fi
  sleep 1
done
printf 'Remote debugging consent was not granted within 240 seconds\n' >&2
exit 1
```

The presser also recognizes Chrome's Restore pages prompt after an owner
relaunches a crashed browser. Starting or restarting the user's browser remains
a separate action.

## HTTP API

| Method | Path | Body or query | Returns |
|---|---|---|---|
| GET | `/targets` | | `Target.getTargets` target-info array |
| POST | `/attach` | `{targetId}` | `{sessionId}`; attachment alone does not establish renderer readiness |
| POST | `/prepare` | `{sessionId, [readiness="dom"], [timeout_seconds=15]}` | Managed page readiness; see the full method below |
| POST | `/release` | `{sessionId, [timeout_seconds=15]}` | Releases preparation while retaining the caller's raw session |
| POST | `/eval` | `{sessionId, expression, [returnByValue=true], [awaitPromise=false], [timeout_seconds=15]}` | `Runtime.evaluate` result, including `exceptionDetails` when present |
| POST | `/cdp` | `{method, [params], [sessionId], [timeout_seconds=15]}` | Raw CDP response; protocol error returns HTTP 500 with structured `error` |
| GET | `/events` | `?since=N&method=substring&limit=N` | Buffered CDP events |
| GET | `/status` | | Connection state, pending waits, prepared pages, state readers and recent logs |
| POST | `/reconnect` | | Requests a fresh connection |
| POST | `/autohook` | `{urlSubstr, script}` or `{clear:true}` | Registers or clears worker startup hooks |
| GET | `/autohooked` | | Recent hook results `{sid, url, result}` |
| POST | `/shutdown` | | Terminates the daemon |

`timeout_seconds` limits the response wait; it does not cancel the command in
Chrome. A queued command can execute later when its target can handle it. The
daemon removes its local response waiter on expiry and ignores a late reply.
Retrying a command with side effects can therefore perform the action twice.
Keep the HTTP client's read timeout above the daemon's requested response wait.
The unit is seconds for both `/eval` and `/cdp`.

A raw CDP protocol error is returned as `{"error":{"code":...,"message":...}}`.
A raw transport or response-timeout error is returned as `{"error":"..."}`.
Preparation errors have their own structured `kind` and `stage` fields. Preserve
the complete response rather than reading only `.result.value`: that extraction
would confuse JavaScript exceptions, protocol errors, expired waits and actual
values.

## Prepare pages before application work

Read [the complete page-preparation method](page-preparation.md). It defines
helper installation and permissions, ownership, readiness levels, release,
timeouts, errors and rendering choices.

The supported method preserves the supplied target and Chrome's selected tab.
It reads browser-maintained loading/discard state, restores a discarded target
when required, enables focus emulation through one shared control session, and
clears freezing before checking the requested condition. Clients share physical
initialization but have independent JavaScript, DOM, complete-load or animation
waits. JavaScript readiness is sufficient for a client that only needs to run a
fetch.

Two diagnostic rules matter:

- A target can be listed and attached while its renderer remains unloaded.
  Focus emulation can load a deferred restored document through Chrome's visible
  capture and `LoadIfNecessary` path.
- A frozen document can answer an immediate inspector evaluation while its
  animation callbacks remain suspended. An evaluation timeout alone does not
  identify freezing, and a returned `2+2` does not establish frame readiness.

The daemon handles these distinctions. Clients should use `/prepare` instead of
copying a separate wake procedure. An uninitialized browser view produces
`view_uninitialized` for animation requests; the supported API does not silently
select tabs or replace a pin. The proof and selection research are retained in
[the research record](research/page-preparation-proof.md).

A work-session example:

```bash
SID=$(curl -s -X POST http://127.0.0.1:7799/attach \
  -H 'Content-Type: application/json' -d '{"targetId":"chosen-target"}' | jq -r .sessionId)
curl -s -X POST http://127.0.0.1:7799/prepare \
  -H 'Content-Type: application/json' \
  -d "{\"sessionId\":\"$SID\",\"readiness\":\"javascript\"}"
curl -s -X POST http://127.0.0.1:7799/eval \
  -H 'Content-Type: application/json' \
  -d "{\"sessionId\":\"$SID\",\"expression\":\"document.title\"}"
curl -s -X POST http://127.0.0.1:7799/release \
  -H 'Content-Type: application/json' -d "{\"sessionId\":\"$SID\"}"
curl -s -X POST http://127.0.0.1:7799/cdp \
  -H 'Content-Type: application/json' \
  -d "{\"method\":\"Target.detachFromTarget\",\"params\":{\"sessionId\":\"$SID\"}}"
```

## Instrument workers before their code runs

Page preparation rejects workers. To wrap a worker's library before startup,
pause the new worker, inject its hook and resume it:

1. On the browser session, without `sessionId`, send through `/cdp`:

   ```json
   {"method":"Target.setAutoAttach","params":{"autoAttach":true,"waitForDebuggerOnStart":true,"flatten":true,"filter":[{"type":"shared_worker"}]}}
   ```

2. Register `/autohook {"urlSubstr":"static_resources/webworker","script":"..."}`.
3. Create the worker. On `Target.attachedToTarget` with a matching URL and
   `waitingForDebugger:true`, the daemon evaluates the script and calls
   `Runtime.runIfWaitingForDebugger`. It resumes unmatched paused targets too.
4. Read `/autohooked` for the result. Registering the same `urlSubstr` replaces
   that hook rather than adding a second copy.

The hook runs in its own thread so the socket reader can continue delivering
responses. Running-worker inspector commands can stay pending behind the
worker's work. The auto-hook avoids that startup race by using the explicit
paused-start event; unrelated targets remain usable on the shared connection.

### Worker instrumentation details

- **New and reused shared workers differ.** A new shared worker pauses under
  `waitForDebuggerOnStart`. A reused worker does not repeat startup. Closing its
  target may leave `SharedWorkerHost` retained by an auto-attach reference. To
  arrange a new worker, disarm auto-attach, close its client pages, allow those
  references to be released, then re-arm and create it again.
- **Defer browser handshakes until resume.** A constructor such as
  `new BroadcastChannel(...)` can wait for a browser-process handshake while
  the worker's paused task loop cannot complete it. Schedule that operation
  with `setTimeout(...,0)` and inspect its result after resume.
- **Queued evaluation remains pending.** An expired response wait does not
  reject or revoke the worker's command. Choose a response budget that fits
  the experiment, and keep its session while waiting for the worker to run it.
  A worker that never handles inspector work can leave that wait unbounded.
- **Install page hooks before navigation.**
  `Page.addScriptToEvaluateOnNewDocument` runs before the next document's page
  scripts. It can wrap `MessagePort`, `Worker`, `crypto.subtle` or library
  globals before the application captures their references.
- **Wrap WebAssembly instantiation rather than exported properties.** WASM
  exports are non-writable and non-configurable. Wrap
  `WebAssembly.instantiate`, `instantiateStreaming` or `Instance`, then return
  an object with a `Proxy` for `.exports` when the application's access pattern
  permits that wrapper.
- **Navigation can replace the worker.** A new worker has new globals and loses
  an earlier injected hook. Use pause-on-start for the replacement and inspect
  target events to distinguish replacement from reuse.
- **Dedicated-worker pause has a version-specific crash record.** Chrome 148
  with patch version .215 was observed to crash when a dedicated worker was
  created or navigated under paused auto-attach; the shared-worker path avoided
  it. The recipe here filters `shared_worker`.

If a library is undefined at the initial pause, the hook can register a short
poll to wrap it when it appears after resume. Wrapping the undefined value
immediately would miss the later assignment.

## Capture high-volume events incrementally

`/events` retains the last 10,000 events without a response ID. Enable the needed
domain first, for example `Network.enable` on the owning session, then read
`/events?since=<lastSeq>`. Each event has a monotonic `seq`.

A busy connection can produce more events than the buffer holds between reads.
Drain it during activity, advance the cursor to the maximum returned sequence,
and append each batch to the capture file. For WebSockets, inspect
`Network.webSocketFrameReceived` and its `response.payloadData`, which is base64
for binary frames. This records the application's network messages without
injecting into a worker or relying on data crossing a JavaScript hook.

## Files and scope

- `cdp_daemon.py` owns the WebSocket, HTTP API, reader and response waits.
- `page_preparation.py` owns managed control sessions and readiness checks.
- `browser-state-extension/` is the passive browser-side state reader.
- `page-preparation.md` defines the supported page method.
- `research/` retains the proof record and the unsupported selection prototype.
- `clear_modals.py` implements AT-SPI consent/restore pressing. It also runs as
  a manual tool: one scan by default, or `--wait` for a bounded wait. Its early
  accessibility health observation is diagnostic; it continues waiting for a
  matching native prompt until a native action completes or the deadline ends.
- `google-chrome.desktop` is an optional user-level Chrome launcher that keeps
  native accessibility available across desktop launches.

Run one daemon per configured endpoint. It acts with the authority of the Chrome
session it is connected to. Treat page data and credentials accordingly, and
avoid printing cookie or token values during diagnostics.
