import * as pdfjs from "pdfjs";
import { checkpointAt, prepareTimeline, renderSceneFitted, seekCheckpoint } from "derivation-vector";
import { configurePdfWorker, readManifest, openPdf, readVectors } from "derivation-resources";

configurePdfWorker(pdfjs);

const byId = (id) => document.getElementById(id);
const ui = {
  projection: document.querySelector(".projection"),
  slide: byId("slide"), pdf: byId("pdf"), vector: byId("vector"),
  text: byId("text-layer"), loading: byId("loading"), blank: byId("blank"),
  nextPreview: byId("next-preview"), nextTitle: byId("next-title"),
  progress: byId("progress"), stepCaption: byId("step-caption"),
  stepLabel: byId("step-label"), play: byId("play"), restart: byId("restart"),
  previous: byId("previous"), next: byId("next"),
  stepBack: byId("step-back"), stepNext: byId("step-next"),
  theme: byId("theme"), speed: byId("speed"), clock: byId("clock"),
  presenter: byId("presenter"), presenterMode: byId("presenter-mode"),
  fullscreen: byId("fullscreen"), projector: byId("projector"),
  error: byId("error"), connection: byId("connection"),
};
const query = new URLSearchParams(location.search);
const audience = query.get("audience") === "1";
// A deck can be open in several presenter tabs. Pair each projector with the
// controller that opened it, including when the same file is opened twice.
let controllerId = audience ? query.get("controller") :
  Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
let channel = null;
let controllerConnected = !audience;
let connectionGeneration = 0;
let projectorWindow = null;
let pairingTimer = null;
let lastControllerContact = 0;
const documents = new Map();
const timelines = new Map();
const commandActions = new Set([
  "next", "previous", "step-next", "step-back", "play", "continuous",
  "restart", "finish", "blank", "first", "last", "theme", "speed",
]);
const commands = [];
let executingCommands = false;

let manifest;
let theme;
let pageIndex = 0;
let scene = null;
let time = 0;
let speed = 1;
let playing = false;
let anchorTime = 0;
let anchorStamp = 0;
let playTarget = 0;
let renderSerial = 0;
let pageRender = null;
let currentPdfPage = null;
let isBlank = false;
let pendingPreview = 0;
let previewTask = null;
let lastBroadcast = 0;
let lastDisplayedTime = -1;
let animationFrame = null;
let latestRemote = null;
let applyingRemote = null;
let vectorScale = 1;

function scheduleAnimation() {
  if (animationFrame === null) animationFrame = requestAnimationFrame(updateFrame);
}

function errorMessage(error) {
  console.error(error);
  ui.error.hidden = false;
  ui.error.textContent = "Presentation error: " + (error?.message || String(error));
}

function activeMetadata() {
  return manifest.themes[theme];
}

function currentMeta() {
  return activeMetadata().pages[pageIndex];
}

function themeAssets(name) {
  if (!documents.has(name)) {
    const entry = manifest.themes[name];
    documents.set(name, openPdf(pdfjs, entry.pdf));
  }
  if (!timelines.has(name)) {
    const entry = manifest.themes[name];
    timelines.set(name, entry.vectors === null ? Promise.resolve(new Map()) :
      readVectors(entry.vectors).then(prepareTimeline));
  }
  return Promise.all([documents.get(name), timelines.get(name)]);
}

function setProgress() {
  const meta = currentMeta();
  const step = scene ? checkpointAt(scene, time) : 0;
  const state = scene ? scene.checkpoints[step].label : "No animation";
  const at = scene ? (step + 1) + "/" + scene.checkpoints.length + " · " + state : state;
  ui.progress.textContent = String(pageIndex + 1) + " / " +
    String(activeMetadata().pages.length) + " (slide " + String(meta.logical) + "/" +
    String(manifest.logicalSlides) + ")";
  ui.stepCaption.textContent = at;
  ui.stepLabel.textContent = at;
  ui.clock.textContent = scene ? time.toFixed(2) + " / " +
    scene.duration.toFixed(2) + " s · " +
    (playing ? "playing" : "paused") + " · " + speed + "×" : "Static slide";
  ui.play.textContent = playing ? "Ⅱ" : "▶";
  ui.play.setAttribute("aria-label", playing ? "Pause animation" : "Play animation");
  ui.play.disabled = !scene;
  ui.restart.disabled = !scene;
  ui.stepBack.disabled = !scene || time <= 0.0001;
  ui.stepNext.disabled = !scene || time >= scene.duration - 0.0001;
  ui.previous.disabled = pageIndex <= 0 && (!scene || time <= 0.0001);
  ui.next.disabled = !isBlank && pageIndex >= activeMetadata().pages.length - 1 &&
    (!scene || time >= scene.duration - 0.0001);
}

function positionVector(meta, activeScene, canvas, pdfCanvas) {
  const region = meta.scene?.rect;
  if (!region || !activeScene) {
    canvas.style.display = "none";
    return;
  }
  // Identical canvas dimensions and CSS placement keep PDF and vector pixels
  // aligned, including fractional link origins and device-pixel ratios.
  canvas.style.left = "0";
  canvas.style.top = "0";
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.display = "block";
  canvas.width = pdfCanvas.width;
  canvas.height = pdfCanvas.height;
}

function drawVector(canvas, activeScene, at, region, scale) {
  if (!activeScene) return;
  const ctx = canvas.getContext("2d", { alpha: true });
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const width = region[2] - region[0];
  const height = region[3] - region[1];
  ctx.setTransform(scale, 0, 0, scale, region[0] * scale, region[1] * scale);
  renderSceneFitted(ctx, activeScene, at, width, height, { pixelBackground: true });
}

function paintVector() {
  if (!scene || !currentMeta().scene || ui.vector.style.display === "none") return;
  if (time === lastDisplayedTime) return;
  drawVector(ui.vector, scene, time, currentMeta().scene.rect, vectorScale);
  lastDisplayedTime = time;
}

function sendState(force = false) {
  if (!channel || audience || !manifest) return;
  const now = performance.now();
  if (!force && now - lastBroadcast < 100) return;
  lastBroadcast = now;
  channel.postMessage({
    type: "state", theme, page: pageIndex, time, speed, playing, target: playTarget,
    sentAt: performance.timeOrigin + now, blank: isBlank,
  });
}

function updateFrame(timestamp) {
  animationFrame = null;
  if (!playing || !scene) return;
  time = Math.min(playTarget, anchorTime + (timestamp - anchorStamp) / 1000 * speed);
  if (time >= playTarget) playing = false;
  paintVector();
  setProgress();
  sendState();
  if (playing) scheduleAnimation();
  else sendState(true);
}

function pause() {
  if (!playing) return;
  time = Math.min(playTarget, anchorTime + (performance.now() - anchorStamp) / 1000 * speed);
  playing = false;
  paintVector();
  setProgress();
  sendState(true);
}

function beginPlayback(target) {
  if (!scene || isBlank) return;
  playTarget = Math.max(time, Math.min(scene.duration, target));
  if (time >= playTarget) return;
  playing = true;
  anchorTime = time;
  anchorStamp = performance.now();
  setProgress();
  sendState(true);
  scheduleAnimation();
}

function play() {
  if (!scene || isBlank) return;
  if (playing) { pause(); return; }
  if (time >= scene.duration - 0.0001) seek(0);
  beginPlayback(playTarget > time + 0.0001 ? playTarget : seekCheckpoint(scene, time, 1));
}

function playContinuous() {
  if (!scene || isBlank) return;
  pause();
  if (time >= scene.duration - 0.0001) seek(0);
  beginPlayback(scene.duration);
}

function seek(value, outgoing = true) {
  if (!scene) return;
  playing = false;
  time = Math.max(0, Math.min(scene.duration, value));
  playTarget = time;
  paintVector();
  setProgress();
  if (outgoing) sendState(true);
}

function nextCheckpoint(direction) {
  if (!scene) return;
  pause();
  seek(seekCheckpoint(scene, time, direction));
}

function entryTime(activeScene, at, backwards) {
  if (!activeScene) return 0;
  return backwards ? activeScene.duration : Math.max(0, Math.min(activeScene.duration, at));
}

function fittedPageBox(meta) {
  const box = ui.projection.getBoundingClientRect();
  const style = getComputedStyle(ui.projection);
  const availableWidth = box.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  const availableHeight = box.height - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
  const ratio = meta.width / meta.height;
  const width = Math.max(1, Math.min(availableWidth, availableHeight * ratio));
  return { width, height: width / ratio };
}

async function showPage(number, opts = {}) {
  const requestedTheme = opts.theme || theme;
  const entry = manifest.themes[requestedTheme];
  const next = Math.max(0, Math.min(entry.pages.length - 1, number));
  const changed = next !== pageIndex;
  const navigation = changed || ui.slide.dataset.renderedTheme !== requestedTheme;
  const previous = pageRender;
  const generation = connectionGeneration;
  const sameRequest = previous?.page === next && previous.theme === requestedTheme &&
    previous.generation === generation;
  const token = ++renderSerial;
  const currentRequest = () => token === renderSerial && (!audience || generation === connectionGeneration);
  const request = {
    page: next, theme: requestedTheme, generation,
    backwards: opts.backwards || (opts.keepTime && sameRequest && previous.backwards),
    remoteState: opts.remoteState || (sameRequest && previous.remoteState),
    pdfTask: null, textLayer: null, finished: Promise.withResolvers(),
  };
  pageRender = request;
  previous?.pdfTask?.cancel();
  previous?.textLayer?.cancel();
  if (!audience) {
    pageIndex = next;
    if (changed && !opts.keepTime) time = 0;
    if (navigation) scene = null;
    if (changed) playTarget = time;
  }
  if (!opts.keepTime || navigation) playing = false;
  ui.slide.setAttribute("aria-busy", "true");
  ui.loading.hidden = currentPdfPage !== null;
  ui.loading.textContent = "Rendering slide " + String(next + 1) + "…";
  if (!audience || currentPdfPage) setProgress();
  if (!audience && navigation) {
    history.replaceState(null, "", "#p=" + String(next + 1));
  }
  try {
    const [pdf, scenes] = await themeAssets(requestedTheme);
    if (!currentRequest()) return;
    const page = await pdf.getPage(next + 1);
    if (!currentRequest()) return;
    const meta = entry.pages[next];
    const nextScene = meta.scene ? scenes.get(meta.scene.id) : null;
    if (meta.scene && !nextScene) throw new Error("Missing Manim scene: " + meta.scene.id);
    if (!audience && navigation) {
      time = entryTime(nextScene, time, request.backwards);
      if (changed || request.backwards) playTarget = time;
      // Resolve backward entry before the projector prepares its first frame.
      sendState(true);
    }
    const box = fittedPageBox(meta);
    const viewport = page.getViewport({ scale: box.width / meta.width });
    const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    const nextPdf = ui.pdf.cloneNode(false);
    const nextVector = ui.vector.cloneNode(false);
    const nextText = ui.text.cloneNode(false);
    nextPdf.width = Math.round(viewport.width * dpr);
    nextPdf.height = Math.round(viewport.height * dpr);
    request.pdfTask = page.render({
      canvasContext: nextPdf.getContext("2d", { alpha: false }), viewport,
      transform: [dpr, 0, 0, dpr, 0, 0],
    });
    await request.pdfTask.promise;
    request.pdfTask = null;
    if (!currentRequest()) return;
    nextText.style.setProperty("--total-scale-factor", String(viewport.scale));
    request.textLayer = new pdfjs.TextLayer({
      textContentSource: page.streamTextContent(),
      container: nextText, viewport,
    });
    await request.textLayer.render();
    request.textLayer = null;
    if (!currentRequest()) return;
    const remoteState = audience ? latestRemote || request.remoteState : null;
    if (remoteState && (remoteState.page !== next || remoteState.theme !== requestedTheme)) return;
    // Keep projector metadata and playback attached to its displayed page until
    // the current connection's PDF, vectors and text can commit together.
    pageIndex = next;
    theme = requestedTheme;
    scene = nextScene;
    if (remoteState) setRemotePlayback(remoteState);
    else {
      time = entryTime(scene, time, request.backwards);
      if (changed || request.backwards) playTarget = time;
    }
    const nextVectorScale = viewport.scale * dpr;
    positionVector(meta, scene, nextVector, nextPdf);
    drawVector(nextVector, scene, time, meta.scene?.rect, nextVectorScale);
    // Keep the complete old slide visible until all three new layers are ready.
    ui.slide.style.width = box.width + "px";
    ui.slide.style.height = box.height + "px";
    ui.slide.style.aspectRatio = meta.width + " / " + meta.height;
    ui.slide.replaceChildren(nextPdf, nextVector, nextText, ui.blank, ui.loading);
    ui.pdf = nextPdf;
    ui.vector = nextVector;
    vectorScale = nextVectorScale;
    ui.text = nextText;
    currentPdfPage = page;
    lastDisplayedTime = time;
    ui.blank.hidden = !isBlank;
    ui.loading.hidden = true;
    ui.slide.dataset.renderedPage = String(next + 1);
    ui.slide.dataset.renderedTheme = requestedTheme;
    ui.slide.dataset.renderedRevision = String(token);
    document.title = meta.title + " | " + manifest.title;
    setProgress();
    if (playing) scheduleAnimation();
    if (!audience) {
      history.replaceState(null, "", "#p=" + String(next + 1));
      updatePreview(token);
      sendState(true);
    }
  } catch (error) {
    if (currentRequest()) {
      ui.loading.hidden = true;
      errorMessage(error);
    }
  } finally {
    if (pageRender === request) {
      pageRender = null;
      ui.slide.setAttribute("aria-busy", "false");
    }
    request.finished.resolve();
  }
}

async function updatePreview(token) {
  if (ui.presenter.hidden || audience || !manifest) return;
  const entry = activeMetadata();
  const index = Math.min(pageIndex + 1, entry.pages.length - 1);
  const own = ++pendingPreview;
  const nextMeta = entry.pages[index];
  const requestedTheme = theme;
  try {
    if (previewTask) {
      const previous = previewTask;
      previous.cancel();
      try { await previous.promise; } catch (error) {
        if (error?.name !== "RenderingCancelledException") throw error;
      }
      if (previewTask === previous) previewTask = null;
    }
    const pdf = await documents.get(requestedTheme);
    const page = await pdf.getPage(index + 1);
    if (own !== pendingPreview || token !== renderSerial) return;
    const width = ui.nextPreview.getBoundingClientRect().width;
    const scale = width / nextMeta.width;
    const viewport = page.getViewport({ scale });
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const canvas = ui.nextPreview.cloneNode(false);
    canvas.width = Math.round(viewport.width * dpr);
    canvas.height = Math.round(viewport.height * dpr);
    const ctx = canvas.getContext("2d", { alpha: false });
    const task = page.render({
      canvasContext: ctx, viewport, transform: [dpr, 0, 0, dpr, 0, 0],
    });
    previewTask = task;
    await task.promise;
    if (previewTask === task) previewTask = null;
    if (own !== pendingPreview || token !== renderSerial) return;
    ui.nextPreview.replaceWith(canvas);
    ui.nextPreview = canvas;
    ui.nextTitle.textContent = "Page " + String(index + 1) + ": " + nextMeta.title;
    ui.nextPreview.dataset.renderedPage = String(index + 1);
    ui.nextPreview.dataset.renderedTheme = requestedTheme;
  } catch (error) {
    if (error?.name !== "RenderingCancelledException") console.debug("Preview unavailable:", error);
  }
}

function toggleBlank() {
  pause();
  isBlank = !isBlank;
  ui.blank.hidden = !isBlank;
  setProgress();
  sendState(true);
}

function setPresenter(enabled) {
  ui.presenter.hidden = !enabled;
  document.body.classList.toggle("presenter-layout", enabled);
  ui.presenterMode.setAttribute("aria-pressed", String(enabled));
  requestAnimationFrame(() => showPage(pageIndex, { keepTime: true }));
}

function stepSlide(delta) {
  pause();
  showPage(pageIndex + delta, { backwards: delta < 0 });
}

function next() {
  if (isBlank) {
    isBlank = false;
    stepSlide(1);
    sendState(true);
    return;
  }
  if (scene && time < scene.duration - 0.0001) {
    if (playing) seek(playTarget);
    else beginPlayback(seekCheckpoint(scene, time, 1));
  } else stepSlide(1);
}

function previous() {
  if (scene && time > 0.0001) nextCheckpoint(-1);
  else stepSlide(-1);
}

function validCommand(action, value) {
  if (!commandActions.has(action)) return false;
  if (action === "theme") return typeof value === "string" && Object.hasOwn(manifest.themes, value);
  if (action === "speed") return [0.5, 0.75, 1, 1.5, 2].includes(value);
  return value === undefined;
}

function submitCommand(action, value) {
  if (!validCommand(action, value)) return;
  if (audience) {
    if (channel && controllerConnected) channel.postMessage({ type: "command", action, value });
    else requestPairing();
    return;
  }
  commands.push({ action, value });
  drainCommands();
}

async function drainCommands() {
  if (executingCommands) return;
  executingCommands = true;
  try {
    while (commands.length) {
      // Navigation clears scene before PDF.js commits the incoming page.
      // Decide each command only after that page is ready, so a second Next
      // starts its derivation instead of accidentally skipping the page.
      while (pageRender) await pageRender.finished.promise;
      const { action, value } = commands.shift();
      executeCommand(action, value);
    }
  } catch (error) {
    commands.length = 0;
    errorMessage(error);
  } finally {
    executingCommands = false;
  }
}

function executeCommand(action, value) {
  switch (action) {
    case "next": next(); break;
    case "previous": previous(); break;
    case "step-next": nextCheckpoint(1); break;
    case "step-back": nextCheckpoint(-1); break;
    case "play": play(); break;
    case "continuous": playContinuous(); break;
    case "restart": seek(0); break;
    case "finish": if (scene) seek(scene.duration); break;
    case "blank": toggleBlank(); break;
    case "first": showPage(0); break;
    case "last": showPage(activeMetadata().pages.length - 1); break;
    case "theme":
      pause();
      theme = value;
      ui.theme.value = value;
      showPage(pageIndex, { keepTime: true });
      break;
    case "speed":
      pause();
      speed = value;
      ui.speed.value = String(value);
      setProgress();
      sendState(true);
      break;
  }
}

function keyboardAction(event) {
  if (event.altKey || event.metaKey || event.isComposing) return null;
  if (event.ctrlKey) {
    if (event.shiftKey) return null;
    return event.key === "ArrowRight" ? "step-next" : event.key === "ArrowLeft" ? "step-back" : null;
  }
  if (event.shiftKey && ["ArrowRight", "ArrowLeft", "Home", "End", " "].includes(event.key)) return null;
  switch (event.key.toLowerCase()) {
    case "arrowright": case " ": return "next";
    case "arrowleft": return "previous";
    case "v": return "continuous";
    case "a": return "play";
    case "r": return "restart";
    case "e": return "finish";
    case "b": return "blank";
    case "home": return "first";
    case "end": return "last";
    case "f": return "fullscreen";
    case "p": return audience ? null : "presenter";
    default: return null;
  }
}

function invalidateRemote() {
  connectionGeneration++;
  latestRemote = null;
  playing = false;
  const previous = pageRender;
  pageRender = null;
  previous?.pdfTask?.cancel();
  previous?.textLayer?.cancel();
  previous?.finished.resolve();
  ui.slide.setAttribute("aria-busy", "false");
  ui.loading.hidden = currentPdfPage !== null;
}

function setConnection(connected, message = "Connecting to presenter…") {
  if (audience && connected !== controllerConnected) invalidateRemote();
  controllerConnected = connected;
  document.documentElement.dataset.controllerConnected = String(connected);
  ui.connection.hidden = connected || !audience;
  ui.connection.textContent = connected ? "" : message;
  if (!connected && audience) {
    playing = false;
    if (manifest) setProgress();
  }
}

function selectChannel(identifier) {
  if (audience) invalidateRemote();
  channel?.close();
  controllerId = identifier;
  const name = identifier ? "derivation-html5-" + location.pathname + "-" + identifier : "";
  document.documentElement.dataset.presentationChannel = name;
  channel = name && "BroadcastChannel" in window ? new BroadcastChannel(name) : null;
  const selected = channel;
  if (selected) selected.onmessage = ({ data }) => {
    if (selected !== channel) return;
    if (data?.type === "request" && !audience) sendState(true);
    else if (data?.type === "command" && !audience) submitCommand(data.action, data.value);
    else if (data?.type === "state" && audience && controllerConnected) receiveRemote(data);
    else if (data?.type === "disconnect" && audience) setConnection(false);
  };
}

function requestPairing() {
  if (!audience) return;
  if (!window.opener || window.opener.closed) {
    setConnection(false, "Presenter closed. Open a projector window from the presentation again.");
    return;
  }
  if (performance.now() - lastControllerContact > 5000) setConnection(false);
  // WindowProxy survives a reload. Only this projector's specific opener can
  // supply a replacement channel; another same-deck presenter cannot adopt it.
  window.opener.postMessage({ type: "derivation-connect", path: location.pathname }, "*");
}

function installPairing() {
  selectChannel(controllerId);
  setConnection(!audience);
  window.addEventListener("message", (event) => {
    const data = event.data;
    // File documents send an opaque postMessage origin even when their
    // location.origin is "file://". The exact opener and path still pair them.
    const messageOrigin = location.protocol === "file:" ? "null" : location.origin;
    if (event.origin !== messageOrigin || data?.path !== location.pathname) return;
    if (audience) {
      if (event.source !== window.opener || data.type !== "derivation-connected" ||
          typeof data.controller !== "string" || !/^[0-9a-f]{32}$/.test(data.controller)) return;
      lastControllerContact = performance.now();
      if (data.controller !== controllerId || !channel) {
        selectChannel(data.controller);
        query.set("controller", controllerId);
        history.replaceState(null, "", location.pathname + "?" + query + location.hash);
      }
      setConnection(data.ready === true);
      if (controllerConnected) channel?.postMessage({ type: "request" });
    } else if (data.type === "derivation-connect") {
      // Reading opener is permitted across WindowProxy boundaries, including
      // local-file windows; guard revoked/navigated references nevertheless.
      try {
        if (!event.source || event.source.opener !== window) return;
      } catch { return; }
      projectorWindow = event.source;
      projectorWindow.postMessage({ type: "derivation-connected", path: location.pathname,
        controller: controllerId, ready: currentPdfPage !== null }, "*");
    }
  });
  const start = () => {
    if (!channel) selectChannel(controllerId);
    if (audience && pairingTimer === null) {
      requestPairing();
      pairingTimer = window.setInterval(requestPairing, 1000);
    }
  };
  window.addEventListener("pagehide", () => {
    if (!audience) channel?.postMessage({ type: "disconnect" });
    else {
      invalidateRemote();
      setConnection(false);
    }
    if (pairingTimer !== null) window.clearInterval(pairingTimer);
    pairingTimer = null;
    channel?.close();
    channel = null;
  });
  window.addEventListener("pageshow", start);
  start();
}

function installControls() {
  installPairing();
  for (const [control, action] of [[ui.previous, "previous"], [ui.next, "next"],
    [ui.play, "play"], [ui.restart, "restart"], [ui.stepBack, "step-back"], [ui.stepNext, "step-next"]]) {
    control.addEventListener("click", () => submitCommand(action));
  }
  ui.theme.addEventListener("change", () => submitCommand("theme", ui.theme.value));
  ui.speed.addEventListener("change", () => submitCommand("speed", Number(ui.speed.value)));
  ui.presenterMode.addEventListener("click", () => setPresenter(ui.presenter.hidden));
  ui.fullscreen.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.body.requestFullscreen().catch(errorMessage);
  });
  ui.projector.addEventListener("click", () => {
    if (audience || !channel) return;
    if (projectorWindow && !projectorWindow.closed) {
      projectorWindow.focus();
      sendState(true);
      return;
    }
    const url = location.pathname + "?audience=1&theme=" + encodeURIComponent(theme) +
      "&controller=" + encodeURIComponent(controllerId) +
      "#p=" + String(pageIndex + 1);
    projectorWindow = window.open(url, "derivation-projector-" + controllerId,
                                  "popup,width=1280,height=960");
    if (projectorWindow) projectorWindow.focus();
    else errorMessage(new Error("Allow pop-up windows for this presentation, then click Open projector window again."));
    sendState(true);
  });
  window.addEventListener("keydown", (event) => {
    if (event.target instanceof HTMLSelectElement || event.target instanceof HTMLInputElement ||
        event.target instanceof HTMLTextAreaElement || event.target?.isContentEditable) return;
    const action = keyboardAction(event);
    if (!action) return;
    event.preventDefault();
    // Fullscreen needs the receiving window's user gesture. Only document
    // controls go through the presenter, never window-local layout actions.
    if (action === "fullscreen") ui.fullscreen.click();
    else if (action === "presenter") setPresenter(ui.presenter.hidden);
    else submitCommand(action);
  });
  new ResizeObserver(() => {
    if (currentPdfPage) showPage(pageRender?.page ?? pageIndex,
      { keepTime: true, theme: pageRender?.theme ?? theme });
  }).observe(ui.projection);
}

function setRemotePlayback(state) {
  time = Math.max(0, Math.min(scene?.duration || 0, state.time));
  speed = state.speed;
  playTarget = Math.max(0, Math.min(scene?.duration || 0, state.target || 0));
  playing = false;
  isBlank = state.blank;
  if (scene && state.playing) {
    time = Math.min(playTarget, time + Math.max(0, performance.timeOrigin + performance.now() - state.sentAt) / 1000 * speed);
    playing = time < playTarget;
    anchorTime = time;
    anchorStamp = performance.now();
  }
}

// One serialized state consumer per connection and one animation-frame request.
// Presenter's 10 Hz clock updates must not start competing RAF loops or
// invalidate a PDF render that's still loading in the projector window.
async function receiveRemote(data) {
  if (!controllerConnected) return;
  const generation = connectionGeneration;
  const currentConnection = () => controllerConnected && generation === connectionGeneration;
  latestRemote = data;
  if (applyingRemote === generation) return;
  applyingRemote = generation;
  try {
    while (currentConnection() && latestRemote) {
      const state = latestRemote;
      latestRemote = null;
      const changedTheme = state.theme !== theme;
      if (changedTheme || ui.slide.dataset.renderedPage !== String(state.page + 1) ||
          ui.slide.dataset.renderedTheme !== state.theme) {
        playing = false;
        await showPage(state.page, { keepTime: true, theme: state.theme, remoteState: state });
      }
      while (currentConnection() && pageRender) await pageRender.finished.promise;
      if (!currentConnection()) return;
      // A newer state supersedes a stale one after any network/PDF wait.
      if (latestRemote) continue;
      if (ui.slide.dataset.renderedPage !== String(state.page + 1) ||
          ui.slide.dataset.renderedTheme !== state.theme) continue;
      setRemotePlayback(state);
      ui.blank.hidden = !isBlank;
      paintVector();
      if (playing) scheduleAnimation();
      setProgress();
    }
  } catch (error) {
    if (currentConnection()) errorMessage(error);
  } finally {
    if (applyingRemote === generation) applyingRemote = null;
  }
}

async function init() {
  document.body.classList.toggle("audience", audience);
  if (audience) ui.presenter.hidden = true;
  manifest = await readManifest();
  if (!manifest?.themes || !Object.keys(manifest.themes).length) {
    throw new Error("Invalid HTML5 export manifest");
  }
  const themeNames = Object.keys(manifest.themes);
  theme = themeNames.includes(query.get("theme")) ? query.get("theme") :
    themeNames.includes("light") ? "light" : themeNames[0];
  for (const name of themeNames) {
    const option = document.createElement("option");
    option.textContent = name[0].toUpperCase() + name.slice(1);
    option.value = name;
    ui.theme.append(option);
  }
  ui.theme.value = theme;
  const match = location.hash.match(/^#p=(\d+)$/);
  pageIndex = match ? Math.max(0, Number(match[1]) - 1) : 0;
  installControls();
  await showPage(pageIndex);
  if (audience) requestPairing();
}

init().catch(errorMessage);
