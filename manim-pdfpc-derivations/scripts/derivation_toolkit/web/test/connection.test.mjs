import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { checkpointAt, seekCheckpoint } from "../vector.mjs";

const source = readFileSync(new URL("../player.mjs", import.meta.url), "utf8")
  .replace(/^import .+;\n/gm, "")
  .replace(/\ninit\(\)\.catch\(errorMessage\);\s*$/, "\n");
const originalController = "01".repeat(16);
const replacementController = "02".repeat(16);
const settle = () => new Promise(resolve => setImmediate(resolve));
const scene = { duration: 10, checkpoints: [
  { time: 0, label: "Initial" }, { time: 5, label: "Middle" }, { time: 10, label: "Final" },
] };

async function projector() {
  const elements = new Map();
  const events = new Map();
  const gates = [];
  const errors = [];
  const commits = [];
  let animationRequests = 0;
  class Element {
    constructor(id) {
      this.id = id;
      this.style = { setProperty() {} };
      this.dataset = {};
      this.attributes = {};
      this.hidden = true;
      this.textContent = "";
      this.context = { canvas: this, setTransform() {}, clearRect() {} };
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    getBoundingClientRect() { return { width: 800, height: 600 }; }
    getContext() { return this.context; }
    cloneNode() { return new Element(this.id); }
    replaceChildren(...children) {
      for (const child of children) elements.set(child.id, child);
      commits.push(children.find(child => child.id === "pdf").paintedPage);
    }
  }
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const pending = (page, stage) => {
    const gate = gates.find(item => item.page === page && item.stage === stage && !item.entered);
    if (!gate) return Promise.resolve();
    gate.entered = true;
    return gate.promise;
  };
  const pdf = { async getPage(number) { return {
    getViewport: ({ scale }) => ({ width: 800 * scale, height: 600 * scale, scale, number }),
    streamTextContent: () => ({}),
    render({ canvasContext }) {
      canvasContext.canvas.paintedPage = number;
      return { promise: pending(number, "pdf"), cancel() {} };
    },
  }; } };
  class Channel {
    constructor(name) { this.name = name; }
    postMessage() {}
    close() { this.closed = true; }
  }
  const window = {
    opener: { closed: false, postMessage() {} },
    addEventListener: (name, callback) => events.set(name, callback),
    setInterval: () => 1, clearInterval() {}, BroadcastChannel: Channel, devicePixelRatio: 1,
  };
  const context = vm.createContext({
    configurePdfWorker() {}, checkpointAt, seekCheckpoint, prepareTimeline: value => value,
    renderSceneFitted(ctx, activeScene, at) { ctx.canvas.paintedTime = at; },
    openPdf: async () => pdf, readVectors: async () => new Map([["demo", scene]]),
    pdfjs: { TextLayer: class {
      constructor({ viewport, container }) { this.viewport = viewport; this.container = container; }
      render() { this.container.textContent = "Page " + this.viewport.number; return pending(this.viewport.number, "text"); }
      cancel() {}
    } },
    document: { getElementById: element, querySelector: element, documentElement: element("root") },
    window, BroadcastChannel: Channel, URLSearchParams,
    location: { search: "?audience=1&controller=" + originalController,
      pathname: "/presentation/index.html", protocol: "file:", origin: "file://", hash: "" },
    history: { replaceState() {} },
    getComputedStyle: () => ({ paddingLeft: "0", paddingRight: "0", paddingTop: "0", paddingBottom: "0" }),
    requestAnimationFrame: () => ++animationRequests,
    performance: { now: () => 0, timeOrigin: 0 },
    console: { error: error => errors.push(error.message) },
  });
  vm.runInContext(source, context);
  const evaluate = code => vm.runInContext(code, context);
  evaluate(`
    theme = "light";
    manifest = { title: "Fixture", logicalSlides: 3, themes: { light: {
      pdf: "light.pdf", vectors: "light.json", pages: Array.from({ length: 3 }, (_, index) => ({
        logical: index + 1, title: "Page " + (index + 1), width: 800, height: 600,
        scene: { id: "demo", rect: [0, 0, 800, 600] },
      })),
    } } };
    installPairing();
  `);
  await evaluate("showPage(0)");
  const connect = (controller = originalController) => events.get("message")({
    source: window.opener, origin: "null", data: { type: "derivation-connected",
      path: "/presentation/index.html", controller, ready: true },
  });
  connect();
  return {
    connect, evaluate, errors, commits,
    event: name => events.get(name)?.({}),
    disconnect: () => evaluate("channel.onmessage({ data: { type: 'disconnect' } })"),
    receive: values => evaluate("receiveRemote")({ type: "state", theme: "light", page: 1,
      time: 2, target: 10, speed: 1, playing: true, blank: false, sentAt: 0, ...values }),
    block(page, stage = "pdf") {
      const deferred = Promise.withResolvers();
      const gate = { ...deferred, page, stage, entered: false };
      gates.push(gate);
      return gate;
    },
    state: () => JSON.parse(evaluate(`JSON.stringify({
      page: ui.slide.dataset.renderedPage, theme: ui.slide.dataset.renderedTheme,
      pageIndex, time, playing, connected: controllerConnected,
      busy: ui.slide.attributes['aria-busy'], clock: ui.clock.textContent,
      caption: ui.progress.textContent, paintedTime: ui.vector.paintedTime,
    })`)),
    animationRequests: () => animationRequests,
  };
}

for (const stage of ["pdf", "text"]) {
  for (const change of ["disconnect", "same controller reconnect", "replacement controller", "pagehide restore"]) {
    test(`${change} cannot commit or resume an old ${stage} render`, async () => {
      const view = await projector();
      const gate = view.block(2, stage);
      const obsolete = view.receive({});
      await settle();
      assert.equal(gate.entered, true);
      if (change === "replacement controller") view.connect(replacementController);
      else if (change === "pagehide restore") {
        view.event("pagehide");
        view.event("pageshow");
        view.connect();
      } else {
        view.disconnect();
        if (change === "same controller reconnect") view.connect();
      }
      if (change !== "disconnect") {
        // A fresh generation must progress even while the old renderer has not
        // resolved cancellation. The real async consumer and renderer run here.
        await view.receive({ page: 2, time: 4 });
        assert.equal(view.state().page, "3");
        assert.equal(view.state().playing, true);
      }
      gate.resolve();
      await obsolete;
      const state = view.state();
      assert.equal(state.page, change === "disconnect" ? "1" : "3");
      assert.equal(state.playing, change !== "disconnect");
      assert.equal(state.pageIndex, Number(state.page) - 1);
      assert.equal(state.busy, "false");
      assert.ok(!view.commits.includes(2), view.commits.join(","));
      assert.deepEqual(view.errors, []);
      if (change === "disconnect") {
        assert.equal(view.animationRequests(), 0);
        view.connect();
        await view.receive({ page: 2, time: 4 });
        assert.equal(view.state().page, "3");
        assert.equal(view.state().playing, true);
      }
    });
  }
}

test("disconnect invalidates playback waiting on a resize of the displayed page", async () => {
  const view = await projector();
  const gate = view.block(1);
  const resize = view.evaluate("showPage(0, { keepTime: true })");
  await settle();
  const obsolete = view.receive({ page: 0 });
  view.disconnect();
  await obsolete;
  assert.equal(view.state().playing, false);
  assert.equal(view.animationRequests(), 0);
  view.connect();
  await view.receive({ page: 0, time: 4, playing: false, target: 4 });
  gate.resolve();
  await resize;
  assert.equal(view.state().time, 4);
  assert.equal(view.state().paintedTime, 4);
  assert.equal(view.state().playing, false);
  assert.deepEqual(view.commits, [1]);
});

test("a disconnected projector ignores late states until its opener pairs again", async () => {
  const view = await projector();
  view.disconnect();
  await view.receive({});
  assert.equal(view.state().page, "1");
  assert.equal(view.state().playing, false);
  view.connect();
  await view.receive({});
  assert.equal(view.state().page, "2");
  assert.equal(view.state().playing, true);
});

test("only errors from the current connection are displayed", async () => {
  const view = await projector();
  const staleGate = view.block(2);
  const obsolete = view.receive({});
  await settle();
  view.disconnect();
  view.connect();
  await view.receive({ page: 2 });
  staleGate.reject(new Error("Obsolete PDF render"));
  await obsolete;
  assert.deepEqual(view.errors, []);
  const currentGate = view.block(2);
  const current = view.receive({});
  await settle();
  currentGate.reject(new Error("Current PDF render"));
  await current;
  assert.deepEqual(view.errors, ["Current PDF render"]);
  assert.equal(view.state().page, "3");
});

test("clock updates coalesce during a current render without losing the newest state", async () => {
  const view = await projector();
  const gate = view.block(2);
  const rendering = view.receive({ time: 1 });
  await settle();
  await view.receive({ time: 5, playing: false, target: 5 });
  gate.resolve();
  await rendering;
  assert.equal(view.state().page, "2");
  assert.equal(view.state().time, 5);
  assert.equal(view.state().paintedTime, 5);
  assert.equal(view.state().playing, false);
  assert.deepEqual(view.commits, [1, 2]);
});
