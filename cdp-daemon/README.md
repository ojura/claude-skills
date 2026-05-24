# CDP Daemon

Drive an already-running Chrome over the DevTools Protocol from scripts, without
the per-call permission modal.

## The problem it solves

Chrome's remote debugging endpoint pops an "Allow remote debugging?" dialog the
first time something connects. If your script opens a fresh WebSocket for every
CDP call, you get a fresh modal every time, which makes scripted automation
against a real logged-in browser unusable. This daemon connects to Chrome
exactly once, auto-presses Allow through the accessibility tree, and then serves
all further CDP calls over that single persistent connection through a small
local HTTP API. One modal, then silence.

## What it does

- Holds one long-lived CDP WebSocket to Chrome (default `127.0.0.1:43809`).
- Auto-presses the Allow dialog via AT-SPI on connect (`clear_modals.py`).
- Exposes `127.0.0.1:7799` with endpoints for `getTargets`, `attachToTarget`,
  `Runtime.evaluate`, arbitrary CDP methods, a rolling event buffer, and status.
- Demultiplexes responses by request id and buffers all id-less CDP events
  (`Network.*`, `Runtime.*`, ...) for incremental polling by sequence number.

## When to use it

When you need to script against the user's real, logged-in Chrome: read
httpOnly cookies, evaluate JS in a page, navigate a tab, or watch network
requests, and you do not want to either spawn a separate automation browser or
drown the user in permission prompts. It is the connection-management layer; you
bring the CDP calls.

## Quick start

```bash
python3 cdp_daemon.py & disown
curl 127.0.0.1:7799/status
curl 127.0.0.1:7799/targets | jq
```

See [`SKILL.md`](SKILL.md) for the full endpoint table, the prerequisites (Chrome
remote-debugging port, AT-SPI renderer accessibility), and worked examples.

## Files

- `cdp_daemon.py` - the daemon (hand-rolled WebSocket framing, reader thread,
  HTTP API). No third-party dependencies.
- `clear_modals.py` - AT-SPI presser for Chrome's Allow dialog. Needs
  `python3-gi` with the Atspi bindings. Also runnable standalone.

## Scope

The daemon speaks CDP to whatever Chrome is already running and logged in. It
holds no credentials of its own. Treat anything it can reach (cookies, page
state) with the same care as the browser session itself.
