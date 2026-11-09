import * as pdfjs from "pdfjs";
import { checkpointAt, prepareScene, renderScene, seekCheckpoint } from "derivation-vector";
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
  error: byId("error"),
};
const query = new URLSearchParams(location.search);
const audience = query.get("audience") === "1";
const channel = "BroadcastChannel" in window ?
  new BroadcastChannel("derivation-html5-" + location.pathname) : null;
const documents = new Map();
const timelines = new Map();

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
let projectorRequested = false;
let animationFrame = null;
let latestRemote = null;
let applyingRemote = false;

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
    timelines.set(name, readVectors(entry.vectors).then((data) =>
      new Map(data.scenes.map((source) => [source.id, prepareScene(source)]))));
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

function positionVector(meta, activeScene, canvas, box) {
  const region = meta.scene?.rect;
  if (!region || !activeScene) {
    canvas.style.display = "none";
    return;
  }
  const left = region[0] / meta.width;
  const top = region[1] / meta.height;
  const width = (region[2] - region[0]) / meta.width;
  const height = (region[3] - region[1]) / meta.height;
  canvas.style.left = (left * 100) + "%";
  canvas.style.top = (top * 100) + "%";
  canvas.style.width = (width * 100) + "%";
  canvas.style.height = (height * 100) + "%";
  canvas.style.display = "block";
  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  const pixelWidth = Math.max(1, Math.round(box.width * width * dpr));
  const pixelHeight = Math.max(1, Math.round(box.height * height * dpr));
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
}

function drawVector(canvas, activeScene, at) {
  if (!activeScene) return;
  const ctx = canvas.getContext("2d", { alpha: false });
  ctx.setTransform(canvas.width / activeScene.width, 0, 0,
                   canvas.height / activeScene.height, 0, 0);
  renderScene(ctx, activeScene, at);
}

function paintVector() {
  if (!scene || !currentMeta().scene || ui.vector.style.display === "none") return;
  if (time === lastDisplayedTime) return;
  drawVector(ui.vector, scene, time);
  lastDisplayedTime = time;
}

function sendState(force = false) {
  if (!channel || audience) return;
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
  const requestedTheme = theme;
  const entry = activeMetadata();
  const next = Math.max(0, Math.min(entry.pages.length - 1, number));
  const changed = next !== pageIndex;
  const navigation = changed || ui.slide.dataset.renderedTheme !== requestedTheme;
  const previous = pageRender;
  const sameRequest = previous?.page === next && previous.theme === requestedTheme;
  const token = ++renderSerial;
  const request = {
    page: next, theme: requestedTheme,
    backwards: opts.backwards || (opts.keepTime && sameRequest && previous.backwards),
    remoteState: opts.remoteState || (sameRequest && previous.remoteState),
    pdfTask: null, textLayer: null, finished: Promise.withResolvers(),
  };
  pageRender = request;
  previous?.pdfTask?.cancel();
  previous?.textLayer?.cancel();
  pageIndex = next;
  if (changed && !opts.keepTime) time = 0;
  if (!opts.keepTime || navigation) playing = false;
  if (navigation) scene = null;
  if (changed) playTarget = time;
  ui.slide.setAttribute("aria-busy", "true");
  ui.loading.hidden = currentPdfPage !== null;
  ui.loading.textContent = "Rendering slide " + String(next + 1) + "…";
  setProgress();
  if (!audience && navigation) {
    history.replaceState(null, "", "#p=" + String(next + 1));
  }
  try {
    const [pdf, scenes] = await themeAssets(requestedTheme);
    if (token !== renderSerial) return;
    const page = await pdf.getPage(next + 1);
    if (token !== renderSerial) return;
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
    if (token !== renderSerial) return;
    nextText.style.setProperty("--total-scale-factor", String(viewport.scale));
    request.textLayer = new pdfjs.TextLayer({
      textContentSource: page.streamTextContent(),
      container: nextText, viewport,
    });
    await request.textLayer.render();
    request.textLayer = null;
    if (token !== renderSerial) return;
    const remoteState = audience ? latestRemote || request.remoteState : null;
    if (remoteState && (remoteState.page !== next || remoteState.theme !== requestedTheme)) return;
    scene = nextScene;
    if (remoteState) setRemotePlayback(remoteState);
    else {
      time = entryTime(scene, time, request.backwards);
      if (changed || request.backwards) playTarget = time;
    }
    positionVector(meta, scene, nextVector, box);
    drawVector(nextVector, scene, time);
    // Keep the complete old slide visible until all three new layers are ready.
    ui.slide.style.width = box.width + "px";
    ui.slide.style.height = box.height + "px";
    ui.slide.style.aspectRatio = meta.width + " / " + meta.height;
    ui.slide.replaceChildren(nextPdf, nextVector, nextText, ui.blank, ui.loading);
    ui.pdf = nextPdf;
    ui.vector = nextVector;
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
    if (token === renderSerial) {
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

function installControls() {
  ui.previous.addEventListener("click", previous);
  ui.next.addEventListener("click", next);
  ui.play.addEventListener("click", play);
  ui.restart.addEventListener("click", () => seek(0));
  ui.stepBack.addEventListener("click", () => nextCheckpoint(-1));
  ui.stepNext.addEventListener("click", () => nextCheckpoint(1));
  ui.theme.addEventListener("change", async () => {
    pause();
    theme = ui.theme.value;
    await showPage(pageIndex, { keepTime: true });
  });
  ui.speed.addEventListener("change", () => {
    pause();
    speed = Number(ui.speed.value);
    setProgress();
    sendState(true);
  });
  ui.presenterMode.addEventListener("click", () => setPresenter(ui.presenter.hidden));
  ui.fullscreen.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.body.requestFullscreen().catch(errorMessage);
  });
  ui.projector.addEventListener("click", () => {
    projectorRequested = true;
    const url = location.pathname + "?audience=1&theme=" + encodeURIComponent(theme) +
      "#p=" + String(pageIndex + 1);
    const windowRef = window.open(url, "derivation-projector-" + location.pathname,
                                 "popup,width=1280,height=960");
    if (windowRef) windowRef.focus();
    else errorMessage(new Error("Allow pop-up windows for this presentation, then click Open projector window again."));
    sendState(true);
  });
  window.addEventListener("keydown", (event) => {
    if (event.target instanceof HTMLSelectElement || event.target instanceof HTMLInputElement) return;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      if (event.ctrlKey) nextCheckpoint(1);
      else next();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      if (event.ctrlKey) nextCheckpoint(-1);
      else previous();
    } else if (event.key === " ") {
      event.preventDefault();
      next();
    } else if (event.key.toLowerCase() === "v") {
      event.preventDefault();
      playContinuous();
    } else if (event.key.toLowerCase() === "a") {
      event.preventDefault();
      play();
    } else if (event.key.toLowerCase() === "r") {
      seek(0);
    } else if (event.key.toLowerCase() === "e") {
      if (scene) seek(scene.duration);
    } else if (event.key.toLowerCase() === "f") {
      ui.fullscreen.click();
    } else if (event.key.toLowerCase() === "b") {
      toggleBlank();
    } else if (event.key.toLowerCase() === "p" && !audience) {
      setPresenter(ui.presenter.hidden);
    } else if (event.key === "Home") {
      showPage(0);
    } else if (event.key === "End") {
      showPage(activeMetadata().pages.length - 1);
    }
  });
  if (channel) channel.onmessage = ({ data }) => {
    if (data?.type === "request" && !audience) sendState(true);
    else if (data?.type === "state" && audience) receiveRemote(data);
  };
  new ResizeObserver(() => {
    if (currentPdfPage) showPage(pageIndex, { keepTime: true });
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

// One serialized state consumer and one animation-frame request per window.
// Presenter's 10 Hz clock updates must not start competing RAF loops or
// invalidate a PDF render that's still loading in the projector window.
async function receiveRemote(data) {
  latestRemote = data;
  if (applyingRemote) return;
  applyingRemote = true;
  try {
    while (latestRemote) {
      const state = latestRemote;
      latestRemote = null;
      const changedTheme = state.theme !== theme;
      if (changedTheme) theme = state.theme;
      if (changedTheme || ui.slide.dataset.renderedPage !== String(state.page + 1) ||
          ui.slide.dataset.renderedTheme !== state.theme) {
        playing = false;
        await showPage(state.page, { keepTime: true, remoteState: state });
      }
      while (pageRender) await pageRender.finished.promise;
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
    errorMessage(error);
  } finally {
    applyingRemote = false;
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
  if (audience && channel) channel.postMessage({ type: "request" });
}

init().catch(errorMessage);
