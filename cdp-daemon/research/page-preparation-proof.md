# Page-preparation proof record

The controls use Chrome 156.0.8078.47, Chromium revision
`1d17a2e2ee147bb7d6ae2ab6abdb3a2510c17775`, on 2026-11-01. The binary is the
workstation's installed Chrome; the profiles, local HTTP fixtures, extension
installations and Xvfb displays belong to the tests. The existing user profile
was left unchanged.

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
decision. They do not authorize installation or a selection-based supported API.

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

The forward implementation passes 66 Python tests, 10 research-worker tests and
27 owned-Chrome tests. The native final-viewport control completes both callbacks,
sets the actual viewport to zero before final metrics, and receives
`view_uninitialized`. Socket-loss gates now signal after the packet write, so
those controls exercise a guarded pending reply rather than a pre-write failure.

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

Chrome's actual grant descriptions were "Access the page debugger backend" and
"Read and change all your data on all websites". The installation guide records
those broad permissions alongside the narrower lifecycle-state and identity
operations used by this implementation. This owned-profile production-path
proof is separate from approval to install into an existing user profile;
landing and installation remain held for review.

## Transport and ownership regressions

These failures were demonstrated separately from the intentional wait-only
timeout behavior:

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

## Chromium source references

All links below refer to the tested revision.

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
