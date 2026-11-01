# Chrome page preparation

Use `/prepare` after attaching to a page and before executing application code.
The daemon loads or resumes the supplied target, holds its emulated visibility
and focus while clients use it, and checks the readiness requested by each
caller. The supported method preserves Chrome's selected tab and window focus.

Clients own their work sessions and application-specific conditions. The daemon
owns preparation. A client such as CLIProxyAPI calls this API instead of keeping
another implementation of Chrome's loading, freezing and focus rules.

## Install the browser-side state reader

Install the bundled `browser-state-extension` directory as an unpacked extension
in the Chrome profile being driven: open `chrome://extensions`, enable developer
mode, and choose **Load unpacked**. Installation into an existing user profile is
a separate action requiring that profile's owner's approval.

The extension declares only `debugger`. It supplies
`chrome.debugger.getTargets()`, which maps an exact CDP target ID to its Chrome
tab ID. Matching URLs would choose incorrectly when several tabs show the same
page. The tabs API provides the non-sensitive `status`, `discarded`, `frozen`
and `active` fields without a `tabs` permission declaration.

The `debugger` permission grants broader authority than the state reader uses.
Chrome describes its actual grants as **"Access the page debugger backend"**
and **"Read and change all your data on all websites"**. Review those grants
before installation. Omitting the redundant `tabs` declaration does not make
this extension read-only; this implementation uses its authority only for
lifecycle-state reads and target-to-tab identity mapping.

The production manifest is passive. The daemon creates one hidden extension
page per browser context when state is first needed, then evaluates the fixed
state-reading expression there. That page does not appear in the tab strip.
The reader calls `chrome.debugger.getTargets()`; it does not attach through the
extension debugger API. The manifest's public key determines the extension ID,
so the daemon and test fixtures derive the ID from the same definition.

A missing, disabled or invalidated extension produces
`browser_state_unavailable` with the failed stage and Chrome's diagnostic.
The daemon closes an invalid reader and creates a new one on a later request.
Cleanup failures remain visible in `/status`; a subsequent request retries
cleanup before replacing that reader.

The raw `/attach`, `/eval` and `/cdp` endpoints remain usable without this
extension. Full page preparation requires browser-maintained state because CDP's
target list does not identify a discarded tab.

## API

### Prepare an attached work session

```http
POST /prepare
Content-Type: application/json

{"sessionId":"work-session","readiness":"javascript","timeout_seconds":15}
```

| Field | Meaning |
|---|---|
| `sessionId` | The caller's existing flattened CDP session for a top-level page. |
| `readiness` | `javascript`, `dom`, `load` or `animation`; default `dom`. |
| `timeout_seconds` | One response-wait budget for the request, positive and finite; default 15. `null` waits without a deadline. |

Unexpected fields are rejected. Tab selection and automatic target replacement
are outside this API.

| Readiness | Condition checked |
|---|---|
| `javascript` | The main document answers the readiness expression. It can still be parsing. |
| `dom` | `document.readyState` is `interactive` or `complete`. |
| `load` | `document.readyState` is `complete`. |
| `animation` | DOM readiness, nonzero window and CSS visual-viewport dimensions, then two `requestAnimationFrame` callbacks. |

JavaScript, DOM and load readiness do not request layout metrics. A fetch-based
client such as a conversation-list operation can request `javascript` readiness.
An application that needs a composer, a particular element or loaded assets
checks that application condition after preparation. Animation readiness checks
frame callbacks; the subsequent screenshot or input command still returns its
own result.

A successful response includes the unchanged work-session and target IDs, the
browser state observed before physical preparation, and the document snapshot:

```json
{
  "sessionId": "work-session",
  "targetId": "page-target",
  "readiness": "javascript",
  "initialTabState": {
    "tabId": 123,
    "status": "complete",
    "discarded": false,
    "frozen": false,
    "active": false
  },
  "document": {
    "url": "https://example.test/",
    "readyState": "complete",
    "visibilityState": "visible",
    "hasFocus": true,
    "window": {"width": 1050, "height": 793},
    "wasDiscarded": false,
    "animationCallbacks": 0
  }
}
```

Animation responses additionally include `document.viewport`, measured by
`Page.getLayoutMetrics`, and `animationCallbacks: 2`. The returned snapshot is
checked again after the callbacks: its DOM readiness, visibility, focus and
positive window/viewport dimensions must still meet the animation conditions.
Zero final dimensions return `view_uninitialized` even when both callbacks ran.

### Release preparation

```http
POST /release
Content-Type: application/json

{"sessionId":"work-session","timeout_seconds":15}
```

Release ends the session's active managed waits even if its first target query
is still pending and registration has not yet been acquired. It leaves the raw
CDP session and already-sent Chrome command intact. If a registration exists,
release removes it. When the last managed client releases a target, the daemon
detaches its control session and removes its focus emulation. Repeated release
is safe. Detaching the work session through `Target.detachFromTarget` also
releases its registration.

The client detaches its work session when it finishes using CDP:

```json
{"method":"Target.detachFromTarget","params":{"sessionId":"work-session"}}
```

## How preparation works

1. Inspect the exact target through the caller's work session. Workers and
   prerender targets are rejected before page-specific operations run.
2. Acquire a reference to one daemon-owned control session for that target.
   Concurrent callers share its physical initialization, including restoration.
3. Read browser-maintained state through the hidden extension page.
4. For a tab still reported as both `unloaded` and `discarded`, obtain its
   current navigation entry, check browser state again, and navigate to that
   entry. A load observed during the second check is allowed to continue.
5. Enable `Emulation.setFocusEmulationEnabled(true)` on the control session.
   This comes before renderer-dependent domain setup and before the lifecycle
   command, because a newly created target can still have an inactive initial
   frame.
6. Call `Page.setWebLifecycleState({state:"active"})` on the control session.
7. Enable Page and Runtime events on the work session, identify its main frame,
   and perform that caller's readiness check. Observed main-frame navigation
   repeats only these read-only readiness checks. Iframe context changes do not
   invalidate the main document's snapshot.

Physical initialization is single-flight per target. Two clients must not both
restore the same discarded tab after reading stale browser state. Its native
commands keep their response wait until they finish or an actual terminal event
occurs. Callers have independent response budgets and readiness levels: one
client's `load` wait does not block another client's `javascript` check.

The current-entry operation is used only after discarded-state observations.
Applying it indiscriminately to a healthy page can abort a pending navigation.
The second state check is an observation, not an atomic navigation transaction:
independent navigation begun before that check is preserved when the check sees
it loading, but navigation begun after that snapshot and before restoration can
be aborted with `net::ERR_ABORTED` while preparation succeeds on the older URL.
Callers must coordinate navigation during discarded-page restoration and their
subsequent application mutations. Preparation registrations are references to
awake state, not exclusive application-operation locks.

### Why focus emulation loads deferred restoration

Focus emulation has an important browser-side effect beyond `document.hasFocus`:

```text
EmulationHandler::SetFocusEmulationEnabled(true)
  -> WebContentsImpl::IncrementCapturerCount(stay_hidden=false)
  -> UpdateVisibilityAndNotifyPageAndView
  -> NavigationControllerImpl::SetActive(true)
  -> LoadIfNecessary(), when needs_reload_ is true
```

That path loads a deferred session-restored document without selecting its tab.
A discarded replacement WebContents deliberately has `needs_reload_` set false,
so it requires the separate discarded-state restoration step.

`Page.setWebLifecycleState("active")` clears freezing; it does not itself request
a deferred load. Putting `Page.enable` or a renderer readiness evaluation before
the loading action can leave those commands waiting for a renderer.

### Why focus has one owner

Chrome's emulated focus flag belongs to the page. If two independent debugging
sessions enable it, detaching one can clear focus even while the other still
holds visible capture. The daemon therefore enables it in one control session
and reference-counts cooperating work sessions.

Managed clients leave focus emulation to this control session. Independent raw
CDP clients and external debuggers can change the same page-global state. A work
session that also manages its own focus override is outside that shared-owner
arrangement. Use a fresh `/attach` session for managed preparation.

## Timeouts and ownership

Each managed request and resource is bound to its original explicit connection
generation. Publication checks that generation under the manager lock, and
worker startup checks it again before issuing native calls. Invalidation ends
that generation's waits and removes its registrations. Delayed work and late
disconnect notifications from an older generation cannot acquire ownership on a
replacement connection. A missing connection is rejected before publication.

A helper's debugging session and its owned target have separate lifetimes.
Losing the attachment does not delete the hidden page. Cleanup closes that page
or confirms its absence before publishing a replacement reader.

`timeout_seconds` limits how long the caller waits. It does not cancel a command
already sent to Chrome, detach the work session, or release its registration.
An expired wait can therefore leave physical initialization in progress. A retry
joins that in-flight initialization rather than starting another restoration.
Use `/status` to inspect the target's initialization stage and completion state.

Release ends that caller's preparation wait. Releasing the last client ends the
shared initialization wait and detaches its physical owner. The daemon retains
an outstanding native attach response long enough to release the session ID
that arrives later, even if its original caller has already left.

With `timeout_seconds: null`, a request can wait indefinitely while its target
and debugging connection remain live and the requested readiness condition is
unmet. Target or session closure, renderer crash, explicit release and connection
loss end guarded waits from those events. Reconnection closes and joins the old
reader before opening another connection; callers then attach fresh sessions.

A raw `/eval` or `/cdp` timeout has the same wait-only behavior. Retrying an
application command with side effects can execute that action twice. The daemon
ignores a response that arrives after its local response waiter was removed.

## Rendering and target choice

A discarded replacement can load in the background and answer JavaScript while
its native window and viewport remain zero-sized. `animation` preparation
reports `view_uninitialized` with those measurements. Hidden CDP targets can
support document work while lacking a composited browser view too.

Strict preparation preserves the supplied physical target. An animation client
can explicitly create a fresh **regular background target** in the same browser
context, attach to it, and prepare it. The client owns that new target and its
cleanup. Cookies and localStorage belong to the shared origin/context;
sessionStorage belongs to the new tab. This is an explicit client resource
choice, not an automatic replacement for a pin or a copy of the old tab's state.

A selection-and-restore research prototype can initialize the old browser view,
but the public tabs API does not offer a conditional restore that is atomic
with a user's later selection. Its controlled counterexample, lifetime tests and
source are retained under `research/` and the test suite. The production
extension manifest does not load that worker, and `/prepare` does not expose it.

## Errors and diagnosis

Preparation errors return an object with `kind`, `stage`, `message`, `sessionId`
and `readiness`. When available, `details` carries Chrome's structured protocol
error, JavaScript exception, or the measured document snapshot. Invalid requests
return HTTP 400; preparation failures return HTTP 500.

Distinguish these observations:

- A listed target and a successful attachment establish a debugging session,
  not renderer readiness.
- `unloaded` with `discarded:false` can describe deferred session restoration.
- An explicitly frozen page can answer an immediate inspector evaluation while
  its frame callbacks are suspended. A short `2+2` probe is insufficient to
  diagnose the page's scheduling state.
- A busy worker can keep inspector commands pending. Page preparation rejects
  workers; worker startup instrumentation uses the separate auto-hook procedure.
- A pending application fetch and an unavailable renderer are different cases.
  Keep JavaScript exceptions, protocol errors, response timeouts and returned
  values distinct when inspecting `/eval` results.

## Tests and source evidence

Run from the `cdp-daemon` directory:

```bash
python3 -B -I -m unittest discover -s tests -p 'test_*.py'
node --test tests/view_initialization.test.mjs
python3 -B -I tests/browser_page_preparation.py -v
```

The browser suite uses the installed Chrome binary, owned profiles, a local
fixture server and a private Xvfb display. It removes `WAYLAND_DISPLAY`, forces
X11, and refuses the desktop's `:0` and `:1` displays. The production helper is
loaded for normal tests; the selection worker is added only to an owned research
fixture. Python tests use events and controlled clocks for synchronization.

See [the proof record](research/page-preparation-proof.md) for the tested Chrome
revision, observed outcomes, rendering controls and exact Chromium source paths.
