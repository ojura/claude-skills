# CDP daemon

Drive a running Chrome through one persistent DevTools connection and a local
HTTP API. On Chrome's consent-controlled endpoint, reconnecting for each call
can repeatedly ask the user to allow remote debugging. The daemon acquires
consent once per connection and shares that connection among clients.

## Capabilities

- Discover Chrome's live endpoint through `DevToolsActivePort` and press its
  consent dialog through AT-SPI.
- Attach to targets, evaluate JavaScript, send arbitrary CDP methods and read a
  rolling buffer of protocol events.
- Prepare an attached page from its browser-maintained loading/discard state,
  share one focus-emulation owner among managed clients, and check the requested
  JavaScript, DOM, complete-load or animation readiness.
- Preserve the supplied target and Chrome's selected tab. Clients explicitly
  choose a new regular background target when their rendering task needs one.
- Keep already-sent commands intact when their response wait expires.
- Observe session closure and connection loss, and finish the old reader before
  opening a replacement connection.

The daemon acts with the authority of the connected browser profile. Page data,
cookies and application operations need the same care as that browser session.

## Start

From this directory:

```bash
python3 cdp_daemon.py & disown
curl -s http://127.0.0.1:7799/status
```

For the user's default Chrome profile, enable debugging through
`chrome://inspect`. Chrome 136 and later ignore `--remote-debugging-port` on the
default user-data directory. Automatic consent pressing needs `python3-gi`,
AT-SPI and Chrome renderer accessibility.

Raw CDP calls work with that connection alone. Full page preparation additionally
requires the bundled passive `browser-state-extension`, installed with the
profile owner's approval. Its `tabs` permission reads lifecycle state;
`debugger` provides exact target-to-tab identity mapping. The production
manifest does not run a background worker.

## Use

- [SKILL.md](SKILL.md) documents connection acquisition, every HTTP endpoint,
  worker instrumentation and event capture.
- [page-preparation.md](page-preparation.md) defines the supported preparation
  API, helper installation, readiness levels, ownership, release and errors.
- [research/page-preparation-proof.md](research/page-preparation-proof.md)
  records the Chrome revision, controlled results and Chromium source paths.

The supported HTTP preparation interface is selection-free. A selection-based
prototype is retained for research; exposing it remains a user decision because
conditional restoration cannot be made atomic with independent tab activity
through the public tabs API.

## Tests

```bash
python3 -B -I -m unittest discover -s tests -p 'test_*.py'
node --test tests/view_initialization.test.mjs
python3 -B -I tests/browser_page_preparation.py -v
```

The browser suite uses owned profiles and a guarded private Xvfb display, with
Wayland removed from the launched environment. It tests the installed Chrome
binary without restarting or reconfiguring the user's browser. Research worker
code is enabled only in the owned extension fixture.

## Files

- `cdp_daemon.py`: WebSocket framing, connection/reader lifetime and HTTP API.
- `page_preparation.py`: managed control sessions, shared initialization and
  per-caller readiness checks.
- `browser-state-extension/`: passive browser-side state reader.
- `clear_modals.py`: AT-SPI consent and restore-prompt support, also usable as a
  standalone manual tool.
- `research/`: proof record and the unsupported selection prototype.
- `tests/`: unit tests and controlled real-browser fixtures.
