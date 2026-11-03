# CDP daemon

Drive a running Chrome through one persistent DevTools connection and a local
HTTP API. On Chrome's consent-controlled endpoint, reconnecting for each call
can repeatedly ask the user to allow remote debugging. The daemon acquires
consent once per connection and shares that connection among clients.

## Capabilities

- Bind the local API listener before starting consent pressing or Chrome
  acquisition; an occupied port fails startup before either action.
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
profile owner's approval. It declares only `debugger` for exact target-to-tab
identity mapping. The tabs API supplies the lifecycle fields without a `tabs`
permission declaration. The debugger grant still gives broad access to website
data; see the installation guide's permission warnings. The production manifest
does not run a background worker.

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
python3 -B -I tests/browser_consent.py
python3 -B -I tests/atspi_dispatch_bus.py
```

The browser suite uses owned profiles and a guarded private Xvfb display, with
Wayland removed from the launched environment. It tests the installed Chrome
binary without restarting or reconfiguring the user's browser. Research worker
code is enabled only in the owned extension fixture.

The native consent suite additionally requires system Python with GI/AT-SPI,
`dbus-run-session`, `xfwm4`, `xdotool`, `xprop`, `xwininfo` and `gdbus`. It runs only
on AC power and records observed AC loss. Its browser profile, display, session bus
and accessibility bus are private; every fresh presser process verifies the
observed Chrome application PID against the owned profile and endpoint before
invoking actions. The suite calibrates focus and window-state observations,
rejects a maximized window's Restore button and webpage prompt imitations, then
acquires Chrome through its actual native consent dialog. It deliberately
crashes only the owned browser to test actual session recovery and checks that
the saved navigation returns in the background while the selected tab, focus
and maximization remain unchanged. It then acquires native consent twice through
the production presser thread, checking that disarming stops scans and that the
same thread handles the next acquisition. Results and selected native metadata are
retained in the reported `/tmp/cdp-native-prompts.*` directory. It does not call
the user's daemon or change the user's Chrome profile.

The AT-SPI dispatch fixture requires system Python with GI/AT-SPI and
`dbus-daemon`. It runs on AC power with an owned bus and registry. It reproduces
queue saturation when an initialized client parks without dispatching, then
checks signal delivery through idle, acquisition and rearm transitions. It also
measures idle CPU time and thread context switches, tests recovery after a
blocked scan fills the queue, and verifies that shutdown removes the connection.
The fixture deletes its temporary files after the run.

## Files

- `cdp_daemon.py`: WebSocket framing, connection/reader lifetime and HTTP API.
- `page_preparation.py`: managed control sessions, shared initialization and
  per-caller readiness checks.
- `browser-state-extension/`: passive browser-side state reader.
- `clear_modals.py`: AT-SPI consent and restore-prompt support, also usable as a
  standalone manual tool.
- `research/`: proof record and the unsupported selection prototype.
- `tests/`: unit tests and controlled real-browser fixtures.
