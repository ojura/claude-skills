"""Prepare page targets through one focus-emulation owner per target."""
import base64
import hashlib
import json
from pathlib import Path
import threading
import time


class PagePreparationError(Exception):
    """Report the failed stage and preserve the caller's session ownership."""

    def __init__(self, kind, stage, message, details=None):
        super().__init__(message)
        self.kind = kind
        self.stage = stage
        self.details = details
        self.session_id = None
        self.readiness = None

    def as_dict(self):
        error = {"kind": self.kind, "stage": self.stage, "message": str(self),
                 "sessionId": self.session_id, "readiness": self.readiness}
        if self.details is not None:
            error["details"] = self.details
        return error


class _ConnectionGeneration:
    """Identify one explicit transport connection and retain its invalidation."""

    def __init__(self, connection):
        if connection is None:
            raise ValueError("A managed connection must have an explicit identity")
        self._connection = connection
        self.valid = True

    @property
    def connection(self):
        return self._connection


class PagePreparationState:
    """Observe navigation and terminal events while one request is waiting."""

    def __init__(self, session_id, remember_detachments=False, generation=None, request=False):
        self.session_id = session_id
        self.generation = generation
        self.request = request
        self.control_session_id = None
        self.additional_sessions = set()
        self.target_id = None
        self.main_frame_id = None
        self.default_contexts = {}
        self.remember_detachments = remember_detachments
        self.early_detachments = set()
        self.stage = "target"
        self.revision = 0
        self.terminal = None
        self.last_document = None
        self.waiters = set()
        self.lock = threading.Lock()

    def observe(self, event):
        method = event.get("method")
        params = event.get("params") or {}
        with self.lock:
            if self.remember_detachments and method == "Target.detachedFromTarget":
                self.early_detachments.add(params.get("sessionId"))
            session_ids = ({self.session_id, self.control_session_id} | self.additional_sessions) - {None}
        terminal = None
        if method == "daemon.disconnected":
            connection = event.get("connection")
            if (connection is not None and self.generation is not None and
                    self.generation.connection is not connection):
                return
            terminal = ("disconnected", "The browser debugging connection closed")
        elif method == "daemon.preparationReleased":
            if not self.request or params.get("sessionId") != self.session_id:
                return
            terminal = ("released", "The preparation registration was released")
        elif method == "daemon.controlReleased":
            if params.get("sessionId") != self.session_id:
                return
            terminal = ("released", "The preparation owner was released")
        elif method == "Target.detachedFromTarget":
            if params.get("sessionId") not in session_ids:
                return
            terminal = ("detached", "A debugging session used for preparation detached")
        elif method == "Target.targetDestroyed":
            if self.target_id is None or params.get("targetId") != self.target_id:
                return
            terminal = ("detached", "The page target closed")
        elif event.get("sessionId") not in session_ids:
            return
        elif method == "Inspector.targetCrashed":
            terminal = ("crashed", "The page renderer crashed during preparation")
        elif method == "Inspector.detached":
            terminal = ("detached", params.get("reason", "The inspector detached"))

        with self.lock:
            if terminal is not None:
                if self.terminal is None:
                    self.terminal = terminal
                for waiter in self.waiters:
                    waiter.set()
            elif event.get("sessionId") == self.session_id:
                if method == "Runtime.executionContextsCleared":
                    self.default_contexts.clear()
                    self.revision += 1
                elif method == "Runtime.executionContextDestroyed":
                    removed = [frame for frame, context in self.default_contexts.items()
                               if context == params.get("executionContextId")]
                    for frame in removed:
                        self.default_contexts.pop(frame)
                        if frame == self.main_frame_id:
                            self.revision += 1
                elif method == "Runtime.executionContextCreated":
                    context = params.get("context", {})
                    auxiliary = context.get("auxData") or {}
                    if auxiliary.get("isDefault"):
                        frame = auxiliary.get("frameId")
                        self.default_contexts[frame] = context.get("id")
                        if frame == self.main_frame_id:
                            self.revision += 1
                elif method == "Page.frameStartedNavigating":
                    if self.main_frame_id is None or params.get("frameId") == self.main_frame_id:
                        self.revision += 1
                elif (method == "Page.frameNavigated" and
                      "parentId" not in params.get("frame", {})):
                    self.main_frame_id = params.get("frame", {}).get("id")
                    self.revision += 1

    def bind_session(self, session_id):
        with self.lock:
            self.session_id = session_id
            if session_id in self.early_detachments and self.terminal is None:
                self.terminal = ("detached", "The debugging session detached before its attach response")
                for waiter in self.waiters:
                    waiter.set()
            self.early_detachments.clear()
            self.remember_detachments = False

    def bind_frame(self, frame_id):
        with self.lock:
            self.main_frame_id = frame_id

    def register(self, waiter):
        with self.lock:
            self.waiters.add(waiter)
            if self.terminal is not None:
                waiter.set()

    def unregister(self, waiter):
        with self.lock:
            self.waiters.discard(waiter)

    def check(self):
        with self.lock:
            if self.terminal is None and self.generation is not None and not self.generation.valid:
                self.terminal = ("disconnected", "The browser debugging connection closed")
            terminal = self.terminal
        if terminal is not None:
            raise PagePreparationError(terminal[0], self.stage, terminal[1])

    def navigation_revision(self):
        with self.lock:
            return self.revision


class _Waitable:
    """Keep a result and wake its response waiters under the manager lock."""

    def __init__(self, generation):
        self.generation = generation
        self.error = None
        self.ready = threading.Event()
        self.waiters = set()

    def wake(self):
        for waiter in self.waiters:
            waiter.set()


class _InitializationJob(_Waitable):
    def __init__(self, generation):
        super().__init__(generation)
        self.tab_state = None
        self.state = None


class _TargetControl(_Waitable):
    """Own the physical emulation session and the callers that still need it."""

    def __init__(self, target_id, generation):
        super().__init__(generation)
        self.target_id = target_id
        self.session_id = None
        self.clients = set()
        self.released_clients = set()
        self.phase = "attaching"
        self.session_detached = False
        self.target_closed = False
        self.completed = threading.Event()
        self.initialization = None

    @property
    def connection(self):
        return self.generation.connection


class _BrowserStateBridge(_TargetControl):
    """Own the hidden extension page used to read browser-maintained state."""

    def __init__(self, context_id, generation):
        super().__init__(None, generation)
        self.context_id = context_id


BROWSER_STATE_EXPRESSION = """(async () => {
  if (document.readyState !== 'complete') {
    await new Promise(resolve => addEventListener('load', resolve, {once:true}));
  }
  if (location.origin !== %s || typeof chrome !== 'object' ||
      typeof chrome.debugger?.getTargets !== 'function' ||
      typeof chrome.tabs?.get !== 'function') {
    return {available:false};
  }
  const targetId = %s;
  if (targetId === null) return {available:true};
  const targets = await chrome.debugger.getTargets();
  const target = targets.find(target => target.id === targetId);
  if (!target) return {available:true, found:false, targetId};
  if (typeof target.tabId !== 'number') {
    return {available:true, found:true, targetId, tabId:null};
  }
  const tab = await chrome.tabs.get(target.tabId);
  return {available:true, found:true, targetId, tabId:tab.id,
          status:tab.status, discarded:tab.discarded,
          frozen:tab.frozen, active:tab.active};
})()"""


VIEW_INITIALIZATION_EXPRESSION = """chrome.runtime.sendMessage({
  method:'initialize-view', targetId:%s, requesterTargetId:%s
})"""


PAGE_READINESS = frozenset(("javascript", "dom", "load", "animation"))
PAGE_READINESS_EXPRESSION = """(async () => {
  const mode = %s;
  const satisfied = () => mode === 'javascript' ||
    (mode === 'load' ? document.readyState === 'complete' :
                       document.readyState !== 'loading');
  if (!satisfied()) {
    await new Promise(resolve => {
      const changed = () => {
        if (satisfied()) {
          document.removeEventListener('readystatechange', changed);
          resolve();
        }
      };
      document.addEventListener('readystatechange', changed);
      changed();
    });
  }
  if (mode === 'animation') {
    await new Promise(resolve => requestAnimationFrame(() =>
      requestAnimationFrame(resolve)));
  }
  return {
    url: location.href,
    readyState: document.readyState,
    visibilityState: document.visibilityState,
    hasFocus: document.hasFocus(),
    window: {width: outerWidth, height: outerHeight},
    wasDiscarded: document.wasDiscarded,
    animationCallbacks: mode === 'animation' ? 2 : 0
  };
})()"""


class PagePreparationManager:
    """Keep emulation separate from client sessions and share it by target.

    Chrome's emulated focus flag is shared by the page. Two independent
    emulation sessions can disable each other's focus when either detaches.
    Managed clients leave focus emulation to the control session and retain
    their work sessions for application operations. Independent raw or external
    debugging sessions can override the page-global flag. Releasing the last
    managed client closes the control session before its replacement is created.
    """

    def __init__(self, call, current_connection, protocol_error_type, clock=None):
        self.call = call
        self.current_connection = current_connection
        self.protocol_error_type = protocol_error_type
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.controls = {}
        self.bridges = {}
        self.active_states = set()
        self.generation = None
        manifest = Path(__file__).with_name("browser-state-extension") / "manifest.json"
        key = base64.b64decode(json.loads(manifest.read_text())["key"])
        digest = hashlib.sha256(key).hexdigest()[:32]
        extension_id = "".join(chr(ord("a") + int(digit, 16)) for digit in digest)
        self.extension_origin = "chrome-extension://" + extension_id

    def _invalidate_generation_locked(self, generation):
        generation.valid = False
        event = {"method": "daemon.disconnected", "connection": generation.connection}
        for registry in (self.controls, self.bridges):
            for key, resource in tuple(registry.items()):
                if resource.generation is generation:
                    resource.error = PagePreparationError(
                        "disconnected", "control", "The browser debugging connection closed")
                    resource.phase = "closed"
                    resource.ready.set()
                    resource.completed.set()
                    resource.wake()
                    registry.pop(key)
        for state in tuple(self.active_states):
            if state.generation is generation:
                state.observe(event)

    def _validate_generation_locked(self, generation):
        if generation is None or not generation.valid:
            raise PagePreparationError("disconnected", "control",
                                       "The managed connection was invalidated")
        if self.current_connection() is not generation.connection:
            self._invalidate_generation_locked(generation)
            raise PagePreparationError("disconnected", "control",
                                       "The browser debugging connection changed")

    def _register_request(self, state):
        with self.lock:
            connection = self.current_connection()
            if connection is None:
                raise PagePreparationError("disconnected", state.stage,
                                           "The browser debugging connection is unavailable")
            generation = self.generation
            if generation is None or generation.connection is not connection:
                if generation is not None:
                    self._invalidate_generation_locked(generation)
                generation = _ConnectionGeneration(connection)
                self.generation = generation
            self._validate_generation_locked(generation)
            state.generation = generation
            self.active_states.add(state)

    def _validate_resource_locked(self, resource):
        self._validate_generation_locked(resource.generation)
        registry = self.bridges if isinstance(resource, _BrowserStateBridge) else self.controls
        key = resource.context_id if isinstance(resource, _BrowserStateBridge) else resource.target_id
        if registry.get(key) is not resource or resource.phase in ("closed", "releasing", "release_failed"):
            raise PagePreparationError("released", "control",
                                       "The managed resource was retired before initialization")

    def _start(self, resource, function, args, completion, failure_phase=None):
        try:
            with self.lock:
                self._validate_generation_locked(resource.generation)
            threading.Thread(target=function, args=args, daemon=True).start()
        except Exception as error:
            with self.lock:
                resource.error = error
                if failure_phase is not None and resource.phase != "closed":
                    resource.phase = failure_phase
                completion.set()
                if completion is resource.ready and isinstance(resource, _BrowserStateBridge):
                    resource.completed.set()
                resource.wake()

    def status(self):
        with self.lock:
            entries = []
            for control in self.controls.values():
                entry = {"targetId": control.target_id, "clients": len(control.clients),
                         "state": control.phase}
                if control.error is not None:
                    entry["error"] = str(control.error)
                job = control.initialization
                if job is not None:
                    entry["initialization"] = {
                        "stage": job.state.stage if job.state is not None else "starting",
                        "complete": job.ready.is_set(),
                    }
                    if job.error is not None:
                        entry["initialization"]["error"] = str(job.error)
                entries.append(entry)
            return entries

    def bridge_status(self):
        with self.lock:
            return [{"browserContextId": bridge.context_id, "state": bridge.phase,
                     **({"error": str(bridge.error)} if bridge.error is not None else {})}
                    for bridge in self.bridges.values()]

    def observe(self, event):
        method = event.get("method")
        params = event.get("params") or {}
        cleanup = []
        bridge_cleanup = []
        with self.lock:
            if method == "daemon.disconnected":
                connection = event.get("connection")
                generations = {resource.generation for resource in
                               (*self.controls.values(), *self.bridges.values())}
                generations.update(state.generation for state in self.active_states
                                   if state.generation is not None)
                if self.generation is not None:
                    generations.add(self.generation)
                for generation in generations:
                    if connection is None or generation.connection is connection:
                        self._invalidate_generation_locked(generation)
            elif method == "Target.detachedFromTarget":
                session_id = params.get("sessionId")
                for control in tuple(self.controls.values()):
                    if session_id == control.session_id:
                        control.session_detached = True
                        if control.phase != "releasing":
                            control.error = PagePreparationError(
                                "detached", "control", "The emulation owner detached")
                            control.phase = "failed"
                            control.wake()
                    if session_id in control.clients:
                        control.clients.remove(session_id)
                        if not control.clients:
                            control.released_clients.add(session_id)
                            if self._begin_release(control):
                                cleanup.append(control)
                for bridge in self.bridges.values():
                    if session_id == bridge.session_id:
                        bridge.session_detached = True
                        if bridge.phase == "ready":
                            bridge_cleanup.append((bridge, PagePreparationError(
                                "browser_state_unavailable", "browser_state",
                                "The hidden extension page detached")))
            elif method == "Inspector.targetCrashed":
                for bridge in self.bridges.values():
                    if event.get("sessionId") == bridge.session_id and bridge.phase == "ready":
                        bridge_cleanup.append((bridge, PagePreparationError(
                            "browser_state_unavailable", "browser_state",
                            "The hidden extension page crashed")))
            elif method == "Target.targetDestroyed":
                control = self.controls.pop(params.get("targetId"), None)
                if control is not None:
                    control.session_detached = True
                    control.target_closed = True
                    control.error = PagePreparationError("detached", "control", "The page target closed")
                    control.phase = "closed"
                    control.ready.set()
                    control.completed.set()
                    control.wake()
                for bridge in self.bridges.values():
                    if params.get("targetId") == bridge.target_id:
                        bridge.session_detached = True
                        bridge.target_closed = True
                        if bridge.error is None:
                            bridge.error = PagePreparationError(
                                "browser_state_unavailable", "browser_state",
                                "The hidden extension page closed")
                        bridge.phase = "failed"
                        bridge.completed.set()
                        bridge.wake()
            states = tuple(self.active_states)
        for state in states:
            state.observe(event)
        for control in cleanup:
            self._start(control, self._close_control, (control,), control.completed, "release_failed")
        for bridge, error in bridge_cleanup:
            self._retire_bridge(bridge, error)

    def _retire_bridge(self, bridge, error):
        with self.lock:
            if bridge.phase in ("releasing", "closed"):
                return
            bridge.error = error
            bridge.phase = "releasing"
            bridge.completed.clear()
        self._start(bridge, self._close_bridge, (bridge,), bridge.completed, "release_failed")

    def _begin_release(self, control):
        if control.phase in ("releasing", "closed"):
            return False
        control.phase = "releasing"
        control.completed.clear()
        job = control.initialization
        if job is not None and job.state is not None:
            job.state.observe({"method": "daemon.controlReleased",
                               "params": {"sessionId": control.session_id}})
        return True

    def _attach_control(self, control):
        guard = PagePreparationState(None, remember_detachments=True,
                                     generation=control.generation)
        guard.target_id = control.target_id
        error = None
        try:
            with self.lock:
                self._validate_resource_locked(control)
                if control.phase != "attaching":
                    raise PagePreparationError("released", "control",
                                               "The resource stopped waiting for attachment")
                self.active_states.add(guard)
            if isinstance(control, _BrowserStateBridge):
                params = {"url": self.extension_origin + "/bridge.html", "hidden": True}
                if control.context_id is not None:
                    params["browserContextId"] = control.context_id
                control.target_id = self.call(
                    "Target.createTarget", params, timeout_seconds=None, _page_state=guard,
                    _expected_socket=control.connection)["result"]["targetId"]
                guard.target_id = control.target_id
            control.session_id = self.call(
                "Target.attachToTarget", {"targetId": control.target_id, "flatten": True},
                timeout_seconds=None, _page_state=guard,
                _expected_socket=control.connection)["result"]["sessionId"]
            guard.bind_session(control.session_id)
            guard.check()
            if isinstance(control, _BrowserStateBridge):
                for method, params in (
                    ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                    ("Runtime.enable", {}),
                    ("Runtime.evaluate", {
                        "expression": BROWSER_STATE_EXPRESSION % (json.dumps(self.extension_origin), "null"),
                        "returnByValue": True, "awaitPromise": True,
                    }),
                ):
                    response = self.call(method, params, sessionId=control.session_id,
                                         timeout_seconds=None, _page_state=guard,
                                         _expected_socket=control.connection)["result"]
                available = response.get("result", {}).get("value", {})
                if not isinstance(available, dict) or not available.get("available"):
                    raise PagePreparationError(
                        "browser_state_unavailable", "browser_state",
                        "Install browser-state-extension from the cdp-daemon directory in this Chrome profile",
                        {"extensionOrigin": self.extension_origin})
        except Exception as caught:
            error = caught
            if isinstance(caught, PagePreparationError) and caught.kind == "detached":
                control.session_detached = True
            if isinstance(control, _BrowserStateBridge):
                control.error = caught
                self._close_bridge(control)
        finally:
            with self.lock:
                self.active_states.discard(guard)
                if control.phase not in ("closed", "release_failed"):
                    control.error = error
                    if control.phase != "releasing":
                        control.phase = "failed" if error is not None else "ready"
                control.ready.set()
                control.wake()

    def _close_bridge(self, bridge):
        error = None
        try:
            if (bridge.target_id is not None and not bridge.target_closed and
                    bridge.generation.valid and self.current_connection() is bridge.connection):
                response = self.call("Target.closeTarget", {"targetId": bridge.target_id},
                                     timeout_seconds=None, _expected_socket=bridge.connection)
                if response.get("result", {}).get("success") is not True:
                    raise PagePreparationError("invalid_response", "cleanup",
                                               "Chrome did not confirm closing the owned target")
                bridge.target_closed = True
                bridge.session_detached = True
        except Exception as caught:
            error = caught
            if (bridge.generation.valid and self.current_connection() is bridge.connection and
                    isinstance(caught, (self.protocol_error_type, PagePreparationError))):
                try:
                    response = self.call("Target.getTargets", timeout_seconds=None,
                                         _expected_socket=bridge.connection)
                    targets = response.get("result", {}).get("targetInfos")
                    if (isinstance(targets, list) and
                            all(isinstance(target, dict) and isinstance(target.get("targetId"), str)
                                for target in targets) and
                            not any(target["targetId"] == bridge.target_id for target in targets)):
                        bridge.target_closed = True
                        bridge.session_detached = True
                        error = None
                except Exception:
                    pass
        finally:
            with self.lock:
                if error is not None and not bridge.target_closed and bridge.phase != "closed":
                    bridge.error = error
                    bridge.phase = "release_failed"
                elif bridge.phase != "closed":
                    bridge.phase = "failed"
                bridge.completed.set()
                bridge.wake()

    def _close_control(self, control):
        # Keep the native attach response accounted for even if the last caller
        # leaves first. Its late response contains the session we must release.
        control.ready.wait()
        error = None
        try:
            if (control.session_id is not None and not control.session_detached and
                    control.generation.valid and self.current_connection() is control.connection):
                self.call("Target.detachFromTarget", {"sessionId": control.session_id},
                          timeout_seconds=None, _expected_socket=control.connection)
                control.session_detached = True
        except Exception as caught:
            error = caught
        finally:
            if control.initialization is not None:
                control.initialization.ready.wait()
            with self.lock:
                if error is not None and not control.session_detached and control.phase != "closed":
                    control.error = error
                    control.phase = "release_failed"
                else:
                    control.phase = "closed"
                    if self.controls.get(control.target_id) is control:
                        self.controls.pop(control.target_id)
                control.completed.set()
                control.wake()

    def _wait(self, control, event, state, remaining):
        waiter = threading.Event()
        state.register(waiter)
        with self.lock:
            control.waiters.add(waiter)
        try:
            while not event.is_set():
                state.check()
                if not waiter.wait(remaining()):
                    raise TimeoutError("The preparation response wait expired")
                waiter.clear()
            state.check()
        finally:
            with self.lock:
                control.waiters.discard(waiter)
            state.unregister(waiter)

    def _get_bridge(self, context_id, state, remaining):
        while True:
            state.check()
            start = False
            start_close = False
            with self.lock:
                state.check()
                self._validate_generation_locked(state.generation)
                bridge = self.bridges.get(context_id)
                if bridge is not None and bridge.generation is not state.generation:
                    raise PagePreparationError("disconnected", "browser_state",
                                               "The state reader belongs to another connection")
                if bridge is not None and bridge.phase == "failed" and bridge.completed.is_set():
                    self.bridges.pop(context_id)
                    bridge = None
                if bridge is None:
                    bridge = _BrowserStateBridge(context_id, state.generation)
                    self.bridges[context_id] = bridge
                    start = True
                if bridge.phase == "release_failed":
                    bridge.phase = "releasing"
                    bridge.completed.clear()
                    start_close = True
                closing = bridge.phase == "releasing"
            if start_close:
                self._start(bridge, self._close_bridge, (bridge,), bridge.completed, "release_failed")
            if closing:
                self._wait(bridge, bridge.completed, state, remaining)
                if bridge.phase == "release_failed":
                    raise self._failure(bridge.error, "browser_state", state.session_id, None)
                continue
            if start:
                self._start(bridge, self._attach_control, (bridge,), bridge.ready, "failed")
            self._wait(bridge, bridge.ready, state, remaining)
            if bridge.error is not None:
                raise self._failure(bridge.error, "browser_state", state.session_id, None)
            with state.lock:
                state.additional_sessions.add(bridge.session_id)
            return bridge

    def _extension_result(self, target, state, remaining, expression, stage):
        state.stage = stage
        bridge = self._get_bridge(target.get("browserContextId"), state, remaining)
        if callable(expression):
            expression = expression(bridge)
        try:
            result = self.call("Runtime.evaluate", {
                "expression": expression, "returnByValue": True, "awaitPromise": True,
            }, sessionId=bridge.session_id, timeout_seconds=remaining(), _page_state=state,
                _expected_socket=bridge.connection)["result"]
        except self.protocol_error_type as error:
            self._retire_bridge(bridge, error)
            raise
        failure = None
        if result.get("exceptionDetails"):
            kind = "browser_state_unavailable" if stage == "browser_state" else "extension"
            message = ("Browser-maintained tab state is unavailable; install or enable browser-state-extension in this Chrome profile"
                       if stage == "browser_state" else "The extension operation raised an exception")
            failure = PagePreparationError(kind, stage, message, result["exceptionDetails"])
        value = result.get("result", {}).get("value")
        if failure is None and not isinstance(value, dict):
            failure = PagePreparationError("invalid_response", stage,
                                          "The extension page did not return an operation result")
        if failure is None and stage == "browser_state" and not value.get("available"):
            failure = PagePreparationError("browser_state_unavailable", stage,
                "Install or enable browser-state-extension in this Chrome profile")
        if failure is not None:
            self._retire_bridge(bridge, failure)
            raise failure
        return value

    def _tab_state(self, target, state, remaining):
        snapshot = self._extension_result(target, state, remaining,
            BROWSER_STATE_EXPRESSION % (json.dumps(self.extension_origin), json.dumps(target["targetId"])),
            "browser_state")
        if not snapshot.get("available"):
            raise PagePreparationError("browser_state_unavailable", "browser_state",
                                       "The extension page did not return browser-maintained tab state")
        if not snapshot.get("found"):
            if target.get("type") == "other":
                return {"tabId": None, "status": "not_tab"}
            raise PagePreparationError("target_unavailable", "browser_state",
                                       "Chrome's browser-side target list does not contain the selected page")
        if snapshot.get("tabId") is not None:
            if (snapshot.get("status") not in ("unloaded", "loading", "complete") or
                    any(not isinstance(snapshot.get(key), bool)
                        for key in ("discarded", "frozen", "active"))):
                raise PagePreparationError("invalid_response", "browser_state",
                                           "The browser did not return complete tab lifecycle state")
        return {key: value for key, value in snapshot.items()
                if key not in ("available", "found", "targetId")}

    def _initialize_view(self, target, state, remaining):
        response = self._extension_result(target, state, remaining,
            lambda bridge: VIEW_INITIALIZATION_EXPRESSION % (
                json.dumps(target["targetId"]), json.dumps(bridge.target_id)), "view_initialization")
        if not response.get("ok") or not isinstance(response.get("result"), dict):
            raise PagePreparationError("view_initialization", "view_initialization",
                                       "Chrome could not initialize the browser view", response)
        result = response["result"]
        if result.get("changed") and not result.get("restored") and result.get("restoreSkipped") != "selection_changed":
            raise PagePreparationError("selection_restore", "view_initialization",
                                       "Chrome could not restore the previous selected tab", result)
        return result

    def _acquire(self, target_id, session_id, state, remaining):
        while True:
            state.check()
            start = False
            with self.lock:
                state.check()
                self._validate_generation_locked(state.generation)
                if any(control.session_id == session_id for control in self.controls.values()):
                    raise PagePreparationError("invalid_request", "control",
                                               "The supplied session is managed internally")
                control = self.controls.get(target_id)
                if control is not None and control.generation is not state.generation:
                    raise PagePreparationError("disconnected", "control",
                                               "The control belongs to another connection")
                if control is None:
                    control = _TargetControl(target_id, state.generation)
                    self.controls[target_id] = control
                    start = True
                closing = control.phase in ("releasing", "release_failed", "closed")
                if not closing:
                    control.clients.add(session_id)
            if closing:
                self._wait(control, control.completed, state, remaining)
                if control.error is not None and control.phase == "release_failed":
                    raise self._failure(control.error, "control", session_id, None)
                continue
            if start:
                self._start(control, self._attach_control, (control,), control.ready, "failed")
            self._wait(control, control.ready, state, remaining)
            if control.error is not None:
                raise self._failure(control.error, "control", session_id, None)
            state.control_session_id = control.session_id
            return control

    def _initialize_target(self, control, target, state, remaining):
        with self.lock:
            state.check()
            self._validate_generation_locked(state.generation)
            self._validate_resource_locked(control)
            if control.generation is not state.generation or state.session_id not in control.clients:
                raise PagePreparationError("released", "initialization",
                                           "The work session is not registered on this connection")
            job = control.initialization
            start = job is None or job.ready.is_set()
            if start:
                job = _InitializationJob(control.generation)
                control.initialization = job
        if start:
            self._start(job, self._run_initialization, (control, target, job), job.ready)
        state.stage = "initialization"
        try:
            self._wait(job, job.ready, state, remaining)
        except TimeoutError:
            if job.state is not None:
                state.stage = job.state.stage
            raise
        if job.error is not None:
            stage = job.state.stage if job.state is not None else "initialization"
            raise self._failure(job.error, stage, state.session_id, None)
        return job.tab_state

    def _run_initialization(self, control, target, job):
        state = PagePreparationState(control.session_id, generation=control.generation)
        state.target_id = control.target_id
        with self.lock:
            job.state = state

        def call(stage, method, params=None):
            state.stage = stage
            state.check()
            with self.lock:
                self._validate_generation_locked(control.generation)
                if (self.controls.get(control.target_id) is not control or
                        not control.clients or control.session_detached or
                        control.phase in ("releasing", "closed", "release_failed")):
                    raise PagePreparationError("released", stage,
                                               "The preparation owner was released")
            response = self.call(method, params, sessionId=control.session_id,
                                 timeout_seconds=None, _page_state=state,
                                 _expected_socket=control.connection)
            if not isinstance(response.get("result"), dict):
                raise PagePreparationError("invalid_response", stage,
                                           "Chrome did not return a command result")
            return response["result"]

        try:
            state.check()
            with self.lock:
                self._validate_resource_locked(control)
                if (control.initialization is not job or not control.clients or
                        control.session_detached):
                    raise PagePreparationError("released", "initialization",
                                               "The preparation owner was released")
                self.active_states.add(state)
            tab_state = self._tab_state(target, state, lambda: None)
            if tab_state.get("discarded") and tab_state.get("status") == "unloaded":
                history = call("restore_history", "Page.getNavigationHistory")
                entries = history.get("entries", [])
                index = history.get("currentIndex", -1)
                if not isinstance(index, int) or not 0 <= index < len(entries):
                    raise PagePreparationError("navigation_state", "restore_history",
                        "Chrome did not return a current history entry for the discarded page")
                current = self._tab_state(target, state, lambda: None)
                if current.get("discarded") and current.get("status") == "unloaded":
                    call("restore", "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})
            # Focus capture loads deferred documents through SetActive and
            # LoadIfNecessary. A new target can still have an inactive initial
            # frame, so focus must precede the lifecycle command.
            call("focus", "Emulation.setFocusEmulationEnabled", {"enabled": True})
            call("unfreeze", "Page.setWebLifecycleState", {"state": "active"})
            job.tab_state = tab_state
        except Exception as error:
            job.error = error
        finally:
            with self.lock:
                self.active_states.discard(state)
                job.ready.set()
                job.wake()

    def _deadline(self, timeout_seconds):
        if timeout_seconds is None:
            return None
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or
                not 0 < timeout_seconds < float("inf")):
            raise PagePreparationError("invalid_request", "validation",
                                       "timeout_seconds must be positive and finite, or null")
        return self.clock() + timeout_seconds

    def _remaining(self, deadline):
        if deadline is None:
            return None
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("The preparation response wait expired")
        return remaining

    def _failure(self, error, stage, session_id, readiness):
        if isinstance(error, PagePreparationError):
            failure = PagePreparationError(error.kind, error.stage, str(error), error.details)
        elif isinstance(error, TimeoutError):
            failure = PagePreparationError("timeout", stage, str(error))
        elif isinstance(error, self.protocol_error_type):
            failure = PagePreparationError("cdp", stage, str(error), error.error)
        else:
            failure = PagePreparationError("transport", stage, str(error))
        failure.session_id = session_id
        failure.readiness = readiness
        return failure

    def prepare(self, session_id, readiness="dom", timeout_seconds=15):
        """Prepare a borrowed session while preserving Chrome's selected tab."""
        return self._prepare(session_id, readiness, timeout_seconds, False)

    def _prototype_prepare_with_selection(self, session_id, readiness="animation", timeout_seconds=15):
        """Research path for view initialization through temporary tab selection."""
        return self._prepare(session_id, readiness, timeout_seconds, True)

    def _prepare(self, session_id, readiness, timeout_seconds, allow_tab_selection):
        """Retain the caller's preparation registration on a response timeout."""
        state = PagePreparationState(session_id, request=True)
        try:
            if not isinstance(session_id, str) or not session_id:
                raise PagePreparationError("invalid_request", "validation",
                                           "sessionId must be a nonempty string")
            if not isinstance(readiness, str) or readiness not in PAGE_READINESS:
                raise PagePreparationError("invalid_request", "validation",
                                           "readiness must be javascript, dom, load, or animation")
            if not isinstance(allow_tab_selection, bool):
                raise PagePreparationError("invalid_request", "validation",
                                           "allowTabSelection must be a boolean")
            deadline = self._deadline(timeout_seconds)

            def remaining():
                return self._remaining(deadline)

            self._register_request(state)
            connection = state.generation.connection

            def call(stage, method, params=None):
                state.stage = stage
                state.check()
                response = self.call(method, params, sessionId=session_id,
                                     timeout_seconds=remaining(), _page_state=state,
                                     _expected_socket=connection)
                if not isinstance(response.get("result"), dict):
                    raise PagePreparationError("invalid_response", stage,
                                               "Chrome did not return a command result")
                return response["result"]

            result = call("target", "Target.getTargetInfo")
            target = result.get("targetInfo")
            if not isinstance(target, dict):
                raise PagePreparationError("invalid_response", "target",
                                           "Chrome did not return target information")
            if target.get("type") not in ("page", "other") or target.get("subtype"):
                raise PagePreparationError("unsupported_target", "target",
                                           "Preparation requires a top-level page or hidden page target",
                                           {"type": target.get("type"), "subtype": target.get("subtype")})
            state.target_id = target["targetId"]
            with self.lock:
                if any(bridge.target_id == state.target_id for bridge in self.bridges.values()):
                    raise PagePreparationError("invalid_request", "target",
                                               "The target is managed internally")
            state.stage = "control"
            remaining()
            control = self._acquire(state.target_id, session_id, state, remaining)
            tab_state = self._initialize_target(control, target, state, remaining)
            call("page_events", "Page.enable")
            frame = call("frame", "Page.getFrameTree").get("frameTree", {}).get("frame", {}).get("id")
            if not isinstance(frame, str) or not frame:
                raise PagePreparationError("invalid_response", "frame",
                                           "Chrome did not return the main frame identifier")
            state.bind_frame(frame)
            call("runtime_events", "Runtime.enable")
            def snapshot(mode, stage, measure_view=False):
                while True:
                    revision = state.navigation_revision()
                    try:
                        result = call(stage, "Runtime.evaluate", {
                            "expression": PAGE_READINESS_EXPRESSION % json.dumps(mode),
                            "returnByValue": True, "awaitPromise": True,
                        })
                        if result.get("exceptionDetails"):
                            raise PagePreparationError("javascript", stage,
                                "The readiness expression raised an exception", result["exceptionDetails"])
                        document = result.get("result", {}).get("value")
                        if not isinstance(document, dict):
                            raise PagePreparationError("invalid_response", stage,
                                "Chrome did not return a document readiness snapshot")
                        ready = document.get("readyState")
                        if (mode != "javascript" and
                                (ready != "complete" if mode == "load" else
                                 ready not in ("interactive", "complete"))):
                            raise PagePreparationError("readiness", stage,
                                "The final document does not meet the requested readiness", document)
                        if mode == "animation" and document.get("animationCallbacks") != 2:
                            raise PagePreparationError("readiness", stage,
                                "The final snapshot does not report both animation callbacks", document)
                        metrics = (call(stage, "Page.getLayoutMetrics").get("cssVisualViewport", {})
                                   if measure_view else None)
                    except (self.protocol_error_type, PagePreparationError):
                        if state.navigation_revision() != revision:
                            continue
                        raise
                    if state.navigation_revision() != revision:
                        continue
                    if measure_view:
                        viewport = {"width": metrics.get("clientWidth"), "height": metrics.get("clientHeight")}
                        dimensions = (*viewport.values(), *document.get("window", {}).values())
                        if (len(dimensions) != 4 or any(
                                isinstance(value, bool) or not isinstance(value, (int, float)) or
                                not 0 <= value < float("inf") for value in dimensions)):
                            raise PagePreparationError("invalid_response", stage,
                                                       "Chrome did not return window and viewport dimensions")
                        document["viewport"] = viewport
                    if document.get("visibilityState") != "visible" or not document.get("hasFocus"):
                        raise PagePreparationError("visibility", stage,
                            "The document did not become emulated visible and focused", document)
                    state.check()
                    state.last_document = document
                    return document

            document = snapshot("dom" if readiness == "animation" else readiness,
                                "readiness", measure_view=readiness == "animation")
            initialization = None
            if readiness == "animation":
                def view_has_size():
                    return all(value > 0 for value in (
                        *document["window"].values(), *document["viewport"].values()))

                if not view_has_size() and allow_tab_selection and tab_state.get("tabId") is not None:
                    initialization = self._initialize_view(target, state, remaining)
                    document = snapshot("dom", "view_initialization", measure_view=True)
                def require_view_size():
                    if not view_has_size():
                        raise PagePreparationError("view_uninitialized", "animation",
                            "The page has an uninitialized browser view",
                            {"document": document, "tabState": tab_state,
                             "viewInitialization": initialization})

                require_view_size()
                document = snapshot("animation", "animation", measure_view=True)
                require_view_size()
            response = {"sessionId": session_id, "targetId": state.target_id,
                        "readiness": readiness, "initialTabState": tab_state,
                        "document": document}
            if initialization is not None:
                response["viewInitialization"] = initialization
            return response
        except Exception as error:
            failure = self._failure(error, state.stage, session_id, readiness)
            if failure.details is None and state.last_document is not None:
                failure.details = {"document": state.last_document}
            raise failure from error
        finally:
            with self.lock:
                self.active_states.discard(state)

    def release(self, session_id, timeout_seconds=15):
        """Release preparation, while leaving the caller's CDP session attached."""
        state = PagePreparationState(None)
        state.stage = "release"
        try:
            if not isinstance(session_id, str) or not session_id:
                raise PagePreparationError("invalid_request", "validation",
                                           "sessionId must be a nonempty string")
            deadline = self._deadline(timeout_seconds)
            with self.lock:
                event = {"method": "daemon.preparationReleased", "params": {"sessionId": session_id}}
                for request in tuple(self.active_states):
                    if request.request and request.session_id == session_id:
                        request.observe(event)
                control = next((control for control in self.controls.values()
                                if session_id in control.clients or
                                session_id in control.released_clients), None)
                if control is None:
                    return {"sessionId": session_id, "released": True}
                state.generation = control.generation
                control.clients.discard(session_id)
                last = not control.clients
                if last:
                    control.released_clients.add(session_id)
                    start = self._begin_release(control)
                else:
                    start = False
            if start:
                self._start(control, self._close_control, (control,), control.completed, "release_failed")
            if last:
                with self.lock:
                    self.active_states.add(state)
                self._wait(control, control.completed, state,
                           lambda: self._remaining(deadline))
                if control.phase == "release_failed":
                    raise self._failure(control.error, "release", session_id, None)
            return {"sessionId": session_id, "released": True}
        except Exception as error:
            raise self._failure(error, state.stage, session_id, None) from error
        finally:
            with self.lock:
                self.active_states.discard(state)
