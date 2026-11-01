# Page-preparation proof record

The current controls use Chrome 156.0.8078.47, Chromium revision
`1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775`, on 2026-11-01. The binary is the
workstation's installed Chrome. The fixture controls use owned profiles, local
HTTP servers, extension installations and Xvfb displays. The separate approved
installation in the existing user profile is recorded below.

The supported interface is selection-free `/prepare` plus `/release`. A public
selection-based option remains a pending user decision. Its prototype is kept
under research and is loaded only by an owned-profile fixture, not by the
production extension manifest.

## Results by state

| Control | Observation |
|---|---|
| Loaded background page | Preparation retained document identity, the draft and history state, and did not add a page request. Release restored ordinary hidden/unfocused reporting. |
| Selected page | Preparation retained Chrome's selected tab. |
| Deferred session restoration | Restarting an owned profile produced 25 `unloaded`, non-discarded tabs. Focus emulation alone loaded one without selecting it. The draft, sessionStorage, history.state and scroll position were restored. |
| Unfreeze alone on a deferred tab | `Page.setWebLifecycleState(active)` returned, but the tab remained unloaded and immediate evaluation reached its response deadline. |
| Domain enable before loading | `Page.enable` reached its response deadline on an unloaded renderer. The loading action followed by domain setup succeeded. |
| Explicit freeze | Immediate `2+2` returned 4 while animation callbacks were suspended. DOM `freeze` and `resume` events were observed. Preparation resumed callbacks without changing document identity or adding a page request. |
| Discarded replacement | Both the Chrome tab ID and CDP target ID changed. State-aware current-entry restoration loaded the new target in the background. |
| State after discard | The draft, history.state and scroll position reset, while sessionStorage remained. Chrome's ordinary native tab-selection reload produced the same result in a paired control. |
| Two managed clients | Both held preparation through one physical focus owner. Releasing one kept the other's emulated focus; releasing the last removed it. |
| Two independent focus owners | Enabling focus separately on two debugging sessions and detaching either cleared the page-global focus flag, despite the remaining visible capture. |
| Held image response | DOM readiness returned while document state was interactive. Complete-load readiness waited until the image fixture was released. |
| Held application fetch | JavaScript/DOM preparation remained responsive while the application fetch was pending. |
| Independent readiness levels | A second session's JavaScript check returned while another session's unbounded complete-load check remained pending. |
| Navigation during readiness | The old context's readiness check was invalidated and repeated for the new main document. |
| Busy worker | An owned worker blocked in `Atomics.wait`. Its target URL was initially empty, but type and parent ID identified it. Page preparation rejected it after `Target.getTargetInfo`, without page wake commands. |
| Timed-out evaluation | An asynchronous evaluation continued after its response wait expired and performed its continuation after the fixture resolved its promise. |
| Hidden CDP target | Document work succeeded. Zero window dimensions caused explicit `view_uninitialized` for animation readiness. |
| Target close, crash, release or connection loss | Each corresponding event ended a guarded unbounded wait. Explicit release kept the caller's raw debugging session attached. |
| Missing helper | Uninstalling the helper caused `browser_state_unavailable`. Reinstallation and a new reader recovered preparation. |

The initial implementation reviewed at `f35a450` passed 52 Python tests,
10 JavaScript worker tests and 26 real-Chrome integration tests. That browser
suite completed in about 14 seconds on AC power. Its `MEASURE` records include the actual observations;
focus-only deferred loading took 11.1 ms in that run. Readiness and rendering are
checked separately instead of using one 25-second evaluation to guess the state.

## Discard restoration and navigation

Discarding and deferred restoration have different loading flags. The old
WebContents-replacement discard path copies the navigation controller with
`needs_reload=false`; normal tab activation later sets that flag and loads it.
Focus capture therefore loads a deferred restored tab but does not, by itself,
load that discarded replacement.

The method reads the current entry and requests it only while browser state
still reports unloaded and discarded. A controlled comparison established why
this restriction matters:

1. Requesting the current history entry on a loaded, stable page retained its
   identity and did not add a request.
2. Doing the same while another navigation was pending aborted that navigation
   with `net::ERR_ABORTED` and left the old document active.

Two managed callers also need shared initialization. The concurrency regression
held two discarded-state snapshots before either restoration. The earlier code
issued two current-entry navigations. Single-flight physical initialization now
issues one, while callers retain independent readiness waits and response
budgets.

An independent native pairing located that boundary precisely. Navigation
begun before the second discarded-state check changed its observation to
loading/non-discarded and completed successfully. Navigation begun after that
snapshot but before the current-entry request was aborted with `net::ERR_ABORTED`;
preparation then succeeded on the older URL. Chrome selection stayed unchanged
in both controls.

Clients must coordinate navigation during discarded-page restoration. The
browser APIs used here do not provide an atomic check-and-restore transaction
that excludes another actor's navigation.

## Rendering controls

A discarded replacement loaded in the background with JavaScript available,
`visibilityState: visible` and emulated focus, while its outer window dimensions
were zero and `requestAnimationFrame` did not run. Size and capture controls were
run on fresh fixtures:

- `Emulation.setVisibleSize` succeeded and changed inner layout dimensions, but
  outer dimensions stayed zero and a frame callback did not arrive within the
  0.75-second response wait.
- A clipped surface screenshot, using the original dimensions and
  `captureBeyondViewport:true`, reached the 0.75-second response deadline.
- Full-document capture also reached that deadline. Its layout dimensions
  changed to the document's content size, while the outer dimensions stayed
  zero and frame callbacks remained pending.
- Screen recording returned a stream handle, but outer dimensions stayed zero
  and the frame callback wait expired. Recording was stopped and its stream
  closed.
- Earlier controls with focus toggling, metrics overrides and screencasting
  likewise did not initialize the discarded view.

These observations keep those calls out of the preparation algorithm. Chrome's
tab bounds remained 0 by 0 after all four actions. The formal browser control
records both document dimensions and those browser-side bounds. The tests detach
their sessions to abandon pending diagnostic expressions and close their owned
targets.

### Fresh regular background target

A fresh regular background target in the same browser context passed animation
readiness while the original zero-view target remained unchanged. Chrome's
selected tab stayed the same, and input focus remained in a separate owned X11
window. Cookies and localStorage were shared by origin/context; sessionStorage
was fresh. This supports an explicit client choice to allocate a new work tab,
not silent substitution of another target for a pin.

### Selection prototype

Selecting the discarded tab through `chrome.tabs.update({active:true})`, then
restoring the previous selection, initialized its view without another page
request. Two frame callbacks and a PNG capture then succeeded. The browser's
original selected tab was restored, and an independent X11 focus-control window
kept input focus.

The prototype serializes its own operations per Chrome window. It retains an
already-applied selection's restoration in the extension worker even when its
response wait expires and CDP disconnects. The controlled lifetime test closed
the hidden requester target, reattached to the worker, released the held native
API completion, and observed restoration with unchanged input focus.

That does not make restoration atomic with other activity. The real Chrome
counterexample wrapped an active-tab query so that it first captured the
temporary target, then applied a newer selection of a third tab before returning
the snapshot. The prototype restored the older tab and overwrote that newer
selection. The public tabs API supplies neither an expected-current-selection
parameter nor a native compare-and-swap operation. Per-window serialization
orders cooperating requests; it cannot supply that missing browser primitive.

The research worker also tests observed third-tab selection, a previous tab
moved to another window, selection failure, queued requester closure and
independent windows. These controls are retained for the pending selection-option
decision.

### Hidden extension-page identity

A CDP-created hidden extension page had `MessageSender.id`, `url` and `origin`,
but omitted `documentId` and `frameId`. `chrome.runtime.getContexts()` listed the
background worker, not the hidden page. `chrome.debugger.getTargets()` did list
that page's exact target ID as type `other`. The research queue therefore checks
the supplied requester target ID before starting queued UI work. It still
finishes restoration after an already-applied selection loses its requester.

## Independent review and forward regressions

Independent review of `f35a450` found four defects beyond the initial matrix:

- A request could pass its connection check on A and later publish ownership
  from the current connection B, or publish with a missing connection. Delayed
  control and reader workers could also start after their resources were
  invalidated. Requests and resources now retain one explicit, non-null
  connection generation. Publication and worker entry validate it under the
  manager lock. A stale disconnect notification applies only to its generation.
  The transport writes through the local socket that passed its identity check.
- Release returned before acquisition without ending an already-active native
  wait. It now signals active work requests under the same lock used for
  publication, even when their first `Target.getTargetInfo` is pending. It leaves
  that already-sent command intact. Creator waits are distinct from work-request
  waits, preserving cleanup of a late native attach response.
- A helper's detached debugging session was treated as a closed owned target.
  Session detachment and target closure now have separate state. An early detach
  before attach-response binding still causes the hidden page to be closed or
  confirmed absent before another reader is created.
- Animation readiness checked positive dimensions before its callbacks but
  accepted zero final dimensions. It now validates the final returned DOM
  readiness, visibility, focus, callback count and positive dimensions. An owned
  Chrome control zeroes the actual viewport after both callbacks and before the
  final metrics request; preparation returns `view_uninitialized`.

The deterministic ownership model keeps native sessions separate from owned
helper targets and uses controlled gates around publication, scheduling, worker
entry and native replies. Its 11 properties detect 10 failures in `f35a450`; the
late-attach cleanup preservation control already passed there. The four
final-animation subcases and the captured-send-socket property also fail on that
reviewed commit. Their positive counterparts pass in the forward implementation.

The additional shared-reader lookup candidate was tested independently and
rejected as a defect. Ordinary closure, closure after an entered 1.5-second
unload handler, and discard replacement delivered A's inspector/session terminal
events before the helper's later missing-tab exception response. The guarded
call ended as detached, the shared reader stayed ready, and B's held
initialization later completed. That terminal handling was retained.

The current deterministic suite passes 72 Python tests and 10 research-worker
tests. The latest owned-Chrome matrix passed 29 tests. Its final-viewport control
completes both callbacks, sets the actual viewport to zero before final metrics,
and receives `view_uninitialized`. Socket-loss gates signal after the packet
write, so those controls exercise a guarded pending reply rather than a pre-write
failure.

An independent recheck of exact `8e238bca` passed three native final-size cases:
zero width, zero height, and both zero after callbacks each returned
`view_uninitialized`. It also passed 28 targeted readiness/snapshot checks. Native
method spans recorded zero layout-metrics calls for JavaScript, DOM and load
readiness, and two for stable animation readiness. The normal-profile shared
client, freeze/discard and post-restart readiness/release controls also passed
with the daemon and supporting modules loaded wholly from that commit.

An independent production-path control installed the reviewed passive helper
through Chrome's Developer mode / Load unpacked UI without either
`--load-extension` or `--enable-unsafe-extension-debugging`. After a normal
restart it remained `ENABLED` and `UNPACKED` at the same extension ID and path.
Developer mode persisted, and the profile entry recorded `location=4` with
`disable_reasons=[]`.

All four HTTP readiness levels and `/release` passed before and after that
restart. Release left the raw session attached, removed managed registrations,
and returned the document to ordinary hidden/unfocused state. Selected-tab IDs
were unchanged. The recorded method set did not contain `Extensions.*` calls,
and the passive helper did not have a background worker.

Additional normal-profile controls exercised two shared clients: releasing the
first retained emulated focus, and releasing the last restored hidden/unfocused
state. Explicit freeze/thaw retained document identity and delivered freeze and
resume events; immediate inspector evaluation still returned 4 while a frame
wait was suspended. Discarded replacement mapping and JavaScript/DOM restoration
passed, while strict animation reported the zero-sized view explicitly. The
separate input-focus window and Chrome's selected tab stayed unchanged.

Cleanup confirmed that the owned Chrome, Xvfb, D-Bus and controller processes had
exited. The retained profile still recorded the helper at the same ID/path with
`location=4`, `disable_reasons=[]` and Developer mode enabled.

## Browser-level automated installation

The approved installation on the actual running Chrome used the existing
browser-level `Extensions.loadUnpacked` automation. A non-mutating capability
probe first passed a missing child of an owned empty temporary directory through
`/cdp`, without `sessionId`. The endpoint returned HTTP 500 with protocol error
`-32600`, `File path cannot be resolved.` The child remained absent.

The real installation then sent:

```json
{"method":"Extensions.loadUnpacked","params":{"path":"/home/juraj/claude-skills/cdp-daemon/browser-state-extension"},"timeout_seconds":20}
```

The response was HTTP 200 with
`result.id:"mjkdoldgccjabmdemjlgnmkbomppkofg"`. The request omitted `sessionId`
and did not open a window or native directory chooser. This endpoint supported
the operation as it was running; the flags used by the isolated integration
fixture did not establish requirements for the live endpoint.

UI metadata identified the helper as `ENABLED`, `UNPACKED`, version `1.0.0`,
at the canonical directory, with every disable reason false. Its own runtime
reported the same ID/version, only `debugger` in both declared and granted
permissions, and `origins:[]`. The selected persisted entry in the Default
profile recorded `location=4`, the canonical path, `disable_reasons=[]`,
`debugger` as its only API permission and empty host-permission lists. Its
`creation_flags` value was `32806`, including `INSTALLED_VIA_CDP`. Browser-level
`Extensions.getExtensions` independently reported the same enabled helper,
canonical path and version.

### Startup policy and owned recovery control

The loader's WebSocket lifetime and Chrome startup have different behavior.
`ExtensionsHandler::LoadUnpacked` uses the default browser context and the normal
unpacked installer, marking the installation `INSTALLED_VIA_CDP`. The normal
installer registers it with the profile. The handler and Chrome DevTools session
have default destructors. Client detachment erases the DevTools session and
leaves the profile's extension registration intact.

On profile startup, the `ExtensionPrefs` constructor unconditionally calls
`CleanUpCdpInstalledExtensions`. That function deletes the preferences for every
installed record marked `INSTALLED_VIA_CDP`. It does not condition this cleanup
on Developer mode. Thus the saved `location=4` entry describes the current
registration but does not establish survival across a Chrome restart.

An owned native-API control confirmed the distinction on the exact revision.
It used an initially fresh profile, private Xvfb and private D-Bus, with ordinary
Chrome flags throughout. The missing-path probe and actual helper load both
worked without `--enable-unsafe-extension-debugging` or `--load-extension`.
Developer mode was enabled and verified before loading. The actual ID/path/
version and debugger-only grants matched, and all four readiness modes and
release passed.

Closing and reopening only the debugging connection retained the same browser
PID and enabled helper, with the same path/version/grants. A subsequent normal
Chrome restart retained Developer mode true, so its checkbox was skipped, but
`Extensions.getExtensions` returned an empty registry. Sending the approved
canonical path through the native loader again restored the helper. Actual
metadata/grants and all four readiness modes plus release passed after that
recovery.

Immediately after startup, the disk preferences could still show the previous
`location=4` and `creation_flags=32806` entry while the live extension registry
was already empty. An initial test incorrectly required immediate disk removal
and failed; after that browser exited, the entry was absent on disk. The
corrected control verifies the actual registry state. Recovery must inspect the
running browser, not infer availability from a saved preference record.

### Persistence after ordinary UI Reload

The ordinary Reload control in a background extensions tab cleared the CDP
marker. With Developer mode enabled, the helper card exposed the ordinary
Reload control at `#dev-reload-button` inside its shadow root. The older
`#dev-reload` selector was absent. The control was enabled, had positive
bounds and the title `Reload`. Calling its normal `click()` handler reported
`isTrusted:false`.

The actual saved `creation_flags` changed from `32806` to `38`, clearing
`INSTALLED_VIA_CDP` while retaining `location=4`, the canonical path, version
and debugger-only grants. The extensions UI and selected control target had the
same browser window ID. The selected document stayed visible with an empty
`visibilitychange` history. The native focus observer first detected a deliberate
`FocusOut`/`FocusIn` pair to verify that it could observe focus changes. During
the measured Reload, its event history stayed empty and `XGetInputFocus` stayed
on the owned control window.

After normal Chrome shutdown, the flushed helper entry still had flags `38`.
After normal restart, Developer mode remained true and was not toggled, the
native registry retained the enabled helper at the same ID/path/version, its
permissions still contained only `debugger` with `origins:[]`, and all four
readiness modes plus release passed without another native load.

The source explains the marker change. `DeveloperPrivateReloadFunction` calls
the registrar's normal Reload path. For an unpacked extension, the Chrome
registrar delegate creates a fresh `UnpackedInstaller` and calls `Load` without
setting `installed_via_cdp`. That member defaults false, so `GetFlags` omits
`INSTALLED_VIA_CDP`. The preference writer saves the new extension's creation
flags. The subsequent profile-start cleanup therefore keeps this registration.

The order of operations matters for an existing managed reader. One control had
already run preparation before UI Reload. Its first subsequent preparation
returned `browser_state_unavailable` with `Extension context invalidated` in the
exception details, while the native registry still showed the helper enabled.
Releasing that failed work registration and explicitly retrying succeeded; all
four readiness modes and release then passed. The invalidated reader was
retired and replaced. After Chrome restart, preparation passed directly on the
new connection.

A separate initially fresh control tested the installation order before managed
use: native load, raw metadata/permission verification with the manager's reader
registry still empty, ordinary background UI Reload, then first preparation.
Every readiness mode and release passed directly, the marker changed from
`32806` to `38`, selection/focus stayed unchanged, and normal browser-restart
persistence passed. This is the documented persistent automated installation
sequence. All controls used ordinary Chrome flags without
`--enable-unsafe-extension-debugging` or `--load-extension`.

### Approved daemon activation

The approved live activation stopped the previous daemon through `/shutdown`
and started the canonical daemon with its launch environment, executable and
working directory preserved. The browser PID and endpoint stayed the same.
Chrome required a new debugging-consent approval, which the owner clicked
manually. The new daemon then reported `connected`, with its prepared-page
registrations and pending waits empty.

The helper survived closure of all setup sessions/pages and the daemon-only
restart without another load. The native registry on the new connection showed
the helper enabled at the same canonical path and version; its runtime verified
debugger-only grants. Checks using an owned regular background page on that live
connection passed JavaScript, DOM, complete-load and animation readiness.
Animation completed two callbacks with positive final window and viewport
dimensions of 1247 by 1388.

Two managed clients retained shared focus after the first release and raw
session detach. Detaching another raw session that had never enabled focus also
left the managed client's focus intact. Final release returned the document to
hidden/unfocused state, repeated release succeeded, and raw evaluation still
returned 4. The selected tab and Chrome window focus matched their initial
states. Cleanup removed the owned verification targets and left `pending=0`,
`prepared_pages=[]` and one ready daemon-owned state reader.

### Persistent registration in the running profile

The approved live helper then received the ordinary Reload action in an owned
regular background extensions tab. Developer mode was already true and was left
unchanged. The actual `#dev-reload-button` invoked the normal UI handler. The
saved creation flags changed from `32806` to `38`, clearing `INSTALLED_VIA_CDP`.
The first-install time stayed the same while the update time changed.

The helper remained `ENABLED` and `UNPACKED` at the canonical path and version
`1.0.0`, with the canonical key and only `debugger` declared/granted,
`origins:[]` and false UI disable reasons. The selected tab, Chrome window focus
and other extensions' enabled/disabled states stayed unchanged. Chrome PID
7143 and daemon PID 2550514 stayed the same. Normal browser-restart persistence
was verified with owned profiles in the control above.

Reload invalidated the cached reader, so the first subsequent preparation failed
with `browser_state_unavailable` and `Extension context invalidated` in its
details. Releasing that failed work registration and explicitly retrying
succeeded. A new daemon-owned reader target replaced the old one while the
verified helper registration stayed enabled.

The subsequent live checks passed all four readiness levels, shared ownership
through the first release/raw detach, detachment of a raw session that had never
enabled focus, final release to hidden/unfocused state, repeated release and raw
evaluation after release. Selected tabs and Chrome window focus stayed
unchanged. Setup and verification targets were removed. The final status was
connected with `pending=0`, `prepared_pages=[]` and one ready state reader.

### Reloading an existing path

A historical repeated-load control returned the existing extension ID and old
cached code after source files changed at an already-loaded path. The transcript
records that observation at
`~/.claude/projects/-tmp/f15c61a7-f79c-4c64-9cf8-550ca5245e87.jsonl:2002`.
An explicit uninstall/load sequence followed, and line 2023 verified the new
function and state. The earlier successful browser-level installation is
recorded at lines 777 to 782. For general updates, Reload preserves extension
data that uninstall/reinstall can delete; verify the runtime manifest and the
changed behavior after reloading.

## Background directory-drop installation

The directory-drop control used the reviewed production helper from
`/home/juraj/claude-skills/cdp-daemon/browser-state-extension` at `5aa3b15`.
Both initially fresh profiles ran on private Xvfb displays and private D-Bus
sessions with ordinary Chrome flags. The command set excluded `Extensions.*`;
the launch arguments excluded `--load-extension` and
`--enable-unsafe-extension-debugging`. The test did not edit preference files
or connect to the user's browser.

The control kept a document selected in the original window and put native
input focus in a separate owned X11 window. It created `chrome://extensions/`
with `Target.createTarget({url:"chrome://extensions/",background:true})`, without
`newWindow`, then attached and enabled focus emulation on that target. The final
run confirmed that the extensions target and selected control target had the
same browser window ID. The selected control document remained visible, with an
empty `visibilitychange` history. Native input focus stayed in the control
window in 5 ms samples; the final run also recorded an empty X11
`FocusIn`/`FocusOut` event history for that window.

Developer mode was observed before taking action. In the fresh profiles it was
false and was enabled through trusted mouse input. The backend then reported
`inDeveloperMode:true` and `canLoadUnpacked:true`, and the drop overlay reported
`dragEnabled:true`. Normal restarts preserved Developer mode. The repeated
controls observed it already true and skipped the checkbox click entirely.

The trusted event sequence was `dragEnter`, `dragOver`, then `drop`, each sent
through `Input.dispatchDragEvent` at a point inside the extensions page with:

```json
{"items":[],"files":["/home/juraj/claude-skills/cdp-daemon/browser-state-extension"],"dragOperationsMask":1}
```

The page recorded `isTrusted:true`. At drop it received exactly one file named
`browser-state-extension`, and `webkitGetAsEntry().isDirectory` was true. Chrome
still rejected the installation:

- `Log.entryAdded` recorded `Unchecked runtime.lastError: No current drop data.`
- The extensions UI emitted `drag-and-drop-load-error` with
  `{message:"No dragged path"}`.
- The installed-extension list remained empty, including after the normal
  restarts. Chrome did not register the helper's expected ID, an installation
  path or a permission grant. The failure prevented helper preparation and a
  persistence check for an installed helper.

An initial check that waited only for registration reached its 10-second fixture
deadline. The instrumented controls captured the explicit errors above,
including with Developer mode already true. The selected document and native
focus controls stayed unchanged throughout those failures. The test did not
observe `Page.fileChooserOpened`; the native source also rejects this missing
path before the branch that opens a directory chooser.

The source at the tested revision explains the distinction. The WebUI directory
handler calls `loadUnpackedFromDrag()`, which passes `useDraggedPath:true`.
Earlier, `notifyDragInstallInProgress()` calls `SetDroppedPath()`, which reads
`WebContents::GetDropData()` and reports `No current drop data.` when that native
state is absent. The load operation subsequently reads the saved dragged file
and reports `No dragged path` when its path is empty. CDP's input handler sends
its drag data directly through `RenderWidgetHostImpl` drag-target calls. The
trusted renderer events in this control did not populate the native
`WebContents` drop state required by the installation API.

This failure belongs to the CDP directory-drop route. The browser-level
`Extensions.loadUnpacked` command succeeded on the actual endpoint as recorded
above. The manual **Load unpacked** UI with its native directory chooser remains
a fallback.

## Helper permissions

A fresh independent fixture changed only the helper's declared permissions from
`["tabs", "debugger"]` to `["debugger"]`, keeping the key and `bridge.html`
identical. The profile did not have prior user extensions.
`runtime.getManifest().permissions` and `permissions.getAll()` both reported
only `debugger`, with `origins: []`. Exact target/tab mapping and the
`status`, `discarded`, `frozen` and `active` fields worked, including true
frozen, discarded and active states. The sensitive `url`, `title` and
`favIconUrl` fields were absent from the returned tab object. All four readiness
modes, release, shared ownership, freeze/thaw, discard restoration and strict
rendering classification passed. Non-rendering readiness still made zero
layout-metrics requests.

With daemon modules from `8e238bca`, that debugger-only installation also survived
a normal restart. The declared and granted permissions remained only `debugger`
before operations, after lifecycle controls and after restart, with `origins: []`.
All four readiness modes and release passed after restart. The helper stayed
enabled at the same ID/path, Developer mode remained enabled, and the recorded
method set did not contain `Extensions.*` calls. Cleanup confirmed that the
owned Chrome, Xvfb, D-Bus and controller processes had exited. The retained
profile recorded `location=4`, `disable_reasons=[]` and only the `debugger` API
permission, with empty host-permission lists.

The production helper therefore declares only `debugger`, which is required for
`chrome.debugger.getTargets()`. The tabs API supplies the non-sensitive lifecycle
fields without a `tabs` declaration. The key and passive behavior are unchanged.
The separate test observer retains `tabs` because it reads URLs.

A deterministic test checks the exact permission declaration, passive manifest
and stable extension ID. The owned-Chrome suite also reads the actual declared
and granted permissions, checks that the helper does not have a service worker,
compares its exact target/tab mapping and lifecycle fields with the independent
observer, and verifies that the sensitive tab fields are absent. It observes
loaded, deferred, frozen, discarded and selected states, then confirms thaw and
discard restoration with the same permission grant. The full 29-test browser
suite passed with this manifest on AC power, alongside 72 Python tests and
10 JavaScript tests.

The independent fixture still received both Chrome grant descriptions:
"Access the page debugger backend" and "Read and change all your data on all
websites". Removing the redundant declaration does not make the helper read-only.
The installation guide records that broad authority alongside the narrower
lifecycle-state and identity operations used by this implementation.

## Transport and ownership regressions

These failures were demonstrated separately from the intentional wait-only
timeout behavior:

- The API server constructor ran in a background thread. With an actual owned
  listener occupying its port, binding failed with `EADDRINUSE` while main still
  logged API availability and attempted Chrome acquisition. Main now acquires the
  listener synchronously before logging, presser startup or acquisition. The
  independent reproducer then records bind failure propagation, an empty startup
  log and acquisition skipped, with zero real Chrome connections. Normal-startup
  controls serve `/status` while fake acquisition is pending and confirm listener
  release after thread-start or later startup failure.
- A socket send failure left a local response waiter registered. The regression
  failed on the old implementation and passes with cleanup in `finally`.
- The reader did not explicitly close its socket on EOF. It now closes the
  reader's captured connection before reporting disconnection.
- Manual reconnection began another connection while the old socket remained
  open, its reader was alive and pending replies had not ended. The socket-pair
  regression failed all three ordering assertions. The manager now shuts down
  and joins that reader before reconnecting, with event-driven notification.
- A response timeout still sends neither cancellation nor detach. Late replies
  are ignored locally; control commands verify their connection identity before
  writing so an old cleanup cannot affect a replacement connection.
- An externally closed state-reader target can make a repeated close command
  fail. Cleanup verifies target absence through the browser's target list before
  recording success; an unconfirmed failure remains `release_failed`.
- The owned-browser fixture retained the 10-second acquisition read timeout,
  unlike production, which already cleared it after upgrading. The fixture now
  calls `settimeout(None)` before starting its reader. A deterministic socket-pair
  control observed `10.0` before the correction and `None` afterward at reader
  startup; the real-browser suite also checks the established socket directly.

## Chromium source references

All links below refer to the tested revision.

- [Browser-level extension loader](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/devtools/protocol/extensions_handler.cc):
  the destructor at line 211 is default; `LoadUnpacked`, lines 213 to 245, uses
  the default browser context and marks the normal unpacked installation as
  installed through CDP.
- [Unpacked installer](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/extensions/browser/unpacked_installer.cc):
  lines 301 to 303 add `INSTALLED_VIA_CDP`; lines 407 to 409 load `kUnpacked`;
  lines 437 to 465 grant permissions and register the installed extension.
- [Ordinary Reload installer](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/extensions/chrome_extension_registrar_delegate.cc):
  `DoLoadExtensionForReload`, lines 264 to 293, creates a new unpacked installer
  without marking it as installed through CDP.
- [Unpacked installer defaults](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/extensions/browser/unpacked_installer.h):
  line 207 initializes `installed_via_cdp_` to false.
- [Registrar Reload dispatch](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/extensions/browser/extension_registrar.cc):
  `DoReloadExtension`, lines 1160 to 1241, disables the old extension for reload
  and delegates loading its path.
- [Reload control](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/resources/extensions/item.html.ts):
  lines 189 to 193 define the ordinary `dev-reload-button` and its click handler.
- [Profile startup cleanup](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/extensions/browser/extension_prefs.cc):
  the constructor, lines 2189 to 2222, calls `CleanUpCdpInstalledExtensions` at
  line 2213; lines 2611 to 2619 delete preferences for records with
  `INSTALLED_VIA_CDP`. `PopulateExtensionInfoPrefs`, lines 2334 to 2347, saves
  the reloaded extension's new creation flags.
- [Extension creation flags](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/extensions/common/extension.h):
  lines 126 to 128 define `INSTALLED_VIA_CDP` as `1 << 15`.
- [Chrome DevTools session](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/devtools/chrome_devtools_session.cc):
  line 151 defines the default destructor.
- [DevTools client detachment](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/devtools/chrome_devtools_manager_delegate.cc):
  `ClientDetached`, lines 430 to 433, erases the DevTools session.

- [Extensions directory-drop handler](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/resources/extensions/drag_and_drop_handler.ts):
  `doDragEnter`, lines 41 to 44, requests native drop-path tracking;
  `doDrop`, lines 54 to 80, recognizes the directory; `handleDirectoryDrop_`,
  lines 93 to 97, reports load errors.
- [Extensions UI service](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/resources/extensions/service.ts):
  `loadUnpackedFromDrag`, lines 559 to 561, passes `useDraggedPath:true`;
  `notifyDragInstallInProgress`, lines 567 to 569, calls the native API.
- [Native extensions installation](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/extensions/api/developer_private/developer_private_functions.cc):
  `SetDroppedPath`, lines 333 to 359, reads native `WebContents` drop data;
  `DeveloperPrivateReloadFunction::Run`, lines 775 to 816, uses the normal
  registrar Reload path; `DeveloperPrivateLoadUnpackedFunction::Run`, lines
  930 to 943, rejects a missing dragged path before directory selection;
  `DeveloperPrivateNotifyDragInstallInProgressFunction::Run`, lines 1122 to
  1137, calls `SetDroppedPath` before the drop is handled.
- [CDP drag dispatch](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/content/browser/devtools/protocol/input_handler.cc):
  `DispatchDragEvent`, lines 1436 to 1463, finds the renderer widget;
  `OnWidgetForDispatchDragEvent`, lines 1465 to 1534, sends renderer drag-target
  calls using the supplied protocol data.

- [Focus emulation](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/content/browser/devtools/protocol/emulation_handler.cc):
  `SetFocusEmulationEnabled`, lines 1042 to 1057, increments visible capture;
  `Disable`, lines 187 to 224, removes the session's override.
- [WebContents visibility and sizing](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/content/browser/web_contents/web_contents_impl.cc):
  `IncrementCapturerCount`, lines 2828 to 2875; visibility update and
  `GetController().SetActive`, lines 5285 to 5408; device-emulation sizing,
  lines 12398 to 12435.
- [Navigation controller](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/content/browser/renderer_host/navigation_controller_impl.cc):
  `SetActive` and `LoadIfNecessary`, lines 5059 to 5126; current-entry traversal
  through `GoToIndex`, lines 1603 to 1652.
- [Discard loading](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/resource_coordinator/tab_lifecycle_unit.cc):
  `Load`, lines 238 to 252; replacement and `CopyStateFrom(..., false)`, lines
  270 to 329.
- [Page lifecycle and capture](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/content/browser/devtools/protocol/page_handler.cc):
  lifecycle active, lines 2049 to 2067; screenshot capture and temporary sizing,
  lines 1420 to 1633.
- [Shared page focus flag](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/third_party/blink/renderer/core/inspector/inspector_emulation_agent.cc):
  `setFocusEmulationEnabled`, lines 603 to 612, updates the page's focus controller.
- [Unloaded tab status](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/chrome/browser/extensions/extension_tab_util.cc):
  main-frame process validity, lines 196 to 199; deferred restoration example,
  lines 422 to 424; loading-status calculation, lines 1270 to 1283.
- [Attachment](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/content/browser/devtools/protocol/target_handler.cc):
  `AttachToTarget`, lines 1195 to 1213, creates the browser-side session.
- [Pending renderer commands](https://chromium.googlesource.com/chromium/src/+/1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775/content/browser/devtools/devtools_session.cc):
  pending-message registration, lines 428 to 452; renderer dispatch, lines
  484 to 506; replay after renderer attachment, lines 268 to 276.
