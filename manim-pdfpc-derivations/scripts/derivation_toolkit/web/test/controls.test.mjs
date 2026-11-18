import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { checkpointAt, prepareScene, renderScene, seekCheckpoint } from "../vector.mjs";

const source = readFileSync(new URL("../player.mjs", import.meta.url), "utf8");
const script = source.replace(/^import .+;\n/gm, "")
  .replace(/\ninit\(\)\.catch\(errorMessage\);\s*$/, "\n");

class Control {
  constructor() {
    this.disabled = false;
    this.hidden = false;
    this.style = { display: "none" };
    this.dataset = {};
    this.listeners = new Map();
  }
  setAttribute() {}
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  click() {
    if (!this.disabled) this.listeners.get("click")?.({ target: this });
  }
}

function player(activeScene, pageCount = 1, options = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Control());
    return elements.get(id);
  };
  const keys = new Map();
  const context = vm.createContext({
    configurePdfWorker() {}, pdfjs: {},
    checkpointAt, prepareScene, renderScene, seekCheckpoint,
    document: { getElementById: element, querySelector: element, documentElement: element("root"),
      body: { requestFullscreen() { context.fullscreenCalls++; return Promise.resolve(); } } },
    window: { addEventListener: (name, callback) => keys.set(name, callback),
      setInterval() { return 1; }, clearInterval() {},
      ...(options.transport ? { BroadcastChannel: options.transport } : {}) },
    BroadcastChannel: options.transport,
    location: { search: options.audience ? "?audience=1&controller=" + options.controller : "",
      pathname: "/one-page/index.html", protocol: options.protocol || "file:",
      origin: options.origin || "file://", hash: "" },
    history: { replaceState() {} },
    crypto: { getRandomValues(bytes) { bytes.fill(options.seed || 1); return bytes; } },
    URLSearchParams,
    ResizeObserver: class { observe() {} },
    HTMLSelectElement: class {}, HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    requestAnimationFrame: () => 1,
    performance: { now: () => 0, timeOrigin: 0 },
    console,
    fixtureScene: activeScene, fixturePageCount: pageCount,
    navigation: [], incoming: [], fullscreenCalls: 0,
  });
  vm.runInContext(script, context);
  vm.runInContext([
    'theme = "light";',
    "manifest = { logicalSlides: fixturePageCount, themes: { light: {",
    "  pages: Array.from({length: fixturePageCount}, (_, i) => ({logical: i + 1}))",
    "}}};",
    "scene = fixtureScene;",
    // Keep the production control handlers; this fixture omits PDF rasterization.
    "showPage = (number) => {",
    "  navigation.push(number);",
    "  pageIndex = Math.max(0, Math.min(fixturePageCount - 1, number));",
    "  ui.blank.hidden = !isBlank;",
    "  setProgress();",
    "};",
    "installControls();",
    "setProgress();",
    // Unit tests inspect routing separately from the real PDF/window suite.
    "receiveRemote = (state) => incoming.push(state);",
    "setConnection(true);",
  ].join("\n"), context);
  return {
    previous: element("previous"), next: element("next"),
    seek: (at) => vm.runInContext("seek", context)(at),
    key: (key, modifiers = {}) => keys.get("keydown")({ key, preventDefault() {}, ...modifiers }),
    event: (name, event) => keys.get(name)?.(event),
    window: context.window,
    state: () => vm.runInContext("({time, playTarget, playing, isBlank, pageIndex})", context),
    evaluate: (code) => vm.runInContext(code, context),
    controller: vm.runInContext("controllerId", context),
    channelName: element("root").dataset.presentationChannel,
    incoming: context.incoming,
    fullscreenCalls: () => context.fullscreenCalls,
    navigation: context.navigation,
  };
}

const scene = {
  duration: 10,
  checkpoints: [
    { time: 0, label: "Initial" }, { time: 5, label: "Middle" }, { time: 10, label: "Final" },
  ],
};

test("ordinary buttons traverse the teaching segments of a sole animated page", () => {
  const view = player(scene);
  assert.equal(view.previous.disabled, true);
  assert.equal(view.next.disabled, false);
  view.next.click();
  assert.equal(view.state().playing, true);
  assert.equal(view.state().playTarget, 5);
  view.seek(5);
  assert.equal(view.previous.disabled, false);
  assert.equal(view.next.disabled, false);
  view.next.click();
  assert.equal(view.state().playTarget, 10);
  view.seek(10);
  assert.equal(view.previous.disabled, false);
  assert.equal(view.next.disabled, true);
  view.previous.click();
  assert.equal(view.state().time, 5);
  view.previous.click();
  assert.equal(view.state().time, 0);
  assert.equal(view.previous.disabled, true);
  assert.equal(view.next.disabled, false);
  assert.equal(view.navigation.length, 0);
});

test("ordinary Next can unblank a sole page at its final state", () => {
  for (const activeScene of [scene, null]) {
    const view = player(activeScene);
    if (activeScene) view.seek(activeScene.duration);
    assert.equal(view.next.disabled, true);
    view.key("b");
    assert.equal(view.state().isBlank, true);
    assert.equal(view.next.disabled, false);
    view.next.click();
    assert.equal(view.state().isBlank, false);
    assert.equal(view.state().pageIndex, 0);
    assert.equal(view.state().time, activeScene?.duration || 0);
    assert.equal(view.next.disabled, true);
    assert.equal(view.navigation[0], 1);
  }
});

test("static page boundaries retain ordinary page navigation", () => {
  const single = player(null);
  assert.equal(single.previous.disabled, true);
  assert.equal(single.next.disabled, true);
  const view = player(null, 2);
  assert.equal(view.previous.disabled, true);
  assert.equal(view.next.disabled, false);
  view.next.click();
  assert.equal(view.state().pageIndex, 1);
  assert.equal(view.previous.disabled, false);
  assert.equal(view.next.disabled, true);
  view.previous.click();
  assert.equal(view.state().pageIndex, 0);
  assert.equal(view.previous.disabled, true);
  assert.equal(view.next.disabled, false);
});

function transport() {
  const groups = new Map();
  return class Channel {
    constructor(name) {
      this.name = name;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(this);
    }
    postMessage(data) {
      for (const peer of groups.get(this.name)) {
        if (peer !== this) peer.onmessage?.({ data });
      }
    }
    close() { groups.set(this.name, groups.get(this.name).filter(peer => peer !== this)); }
  };
}

test("projector actions run only in their paired presenter", () => {
  const bus = transport();
  const first = player(scene, 3, { transport: bus, seed: 1 });
  const second = player(scene, 3, { transport: bus, seed: 2 });
  const projector = player(scene, 3, { transport: bus, audience: true, controller: first.controller });
  const otherProjector = player(scene, 3, { transport: bus, audience: true, controller: second.controller });
  assert.notEqual(first.channelName, second.channelName);
  projector.key("ArrowRight", { ctrlKey: true });
  assert.equal(first.state().time, 5);
  assert.equal(second.state().time, 0);
  // The audience has no independent command mutation; rendering follows state.
  assert.equal(projector.state().time, 0);
  assert.equal(projector.incoming.at(-1).time, 5);
  assert.equal(otherProjector.incoming.length, 0);
  projector.key("b");
  assert.equal(first.state().isBlank, true);
  projector.key("a");
  projector.key("v");
  assert.equal(first.state().time, 5);
  assert.equal(first.state().playing, false);
  projector.key("ArrowRight");
  assert.equal(first.state().isBlank, false);
  assert.equal(first.state().pageIndex, 1);
  assert.equal(second.state().pageIndex, 0);
  otherProjector.key("v");
  assert.equal(second.state().playing, true);
  assert.equal(second.state().playTarget, 10);
  assert.equal(first.state().playing, false);
});

test("window-local keys and text editing shortcuts are not remote commands", () => {
  const bus = transport();
  const presenter = player(scene, 2, { transport: bus });
  const projector = player(scene, 2, { transport: bus, audience: true, controller: presenter.controller });
  for (const [key, modifiers] of [["a", { ctrlKey: true }], ["v", { ctrlKey: true }],
    ["r", { metaKey: true }], ["ArrowRight", { shiftKey: true }],
    ["ArrowLeft", { altKey: true }], ["b", { isComposing: true }],
    ["v", { target: { isContentEditable: true } }]]) {
    projector.key(key, modifiers);
  }
  assert.equal(projector.incoming.length, 0);
  assert.equal(presenter.state().playing, false);
  projector.key("f");
  assert.equal(projector.fullscreenCalls(), 1);
  assert.equal(presenter.fullscreenCalls(), 0);
  assert.equal(projector.incoming.length, 0);
});

test("malformed or unrecognized commands never mutate controller state", () => {
  const bus = transport();
  const presenter = player(scene, 2, { transport: bus });
  const sender = new bus(presenter.channelName);
  for (const message of [{ type: "command", action: "__proto__" },
    { type: "command", action: "theme", value: "missing" },
    { type: "command", action: "speed", value: "2" },
    { type: "command", action: "speed", value: 100 },
    { type: "command", action: "next", value: 1 }, { type: "state", time: 5 }]) {
    sender.postMessage(message);
  }
  assert.equal(presenter.state().time, 0);
  assert.equal(presenter.state().playing, false);
  assert.equal(presenter.navigation.length, 0);
});

test("queued keys wait for a prepared animation page instead of skipping it", async () => {
  const bus = transport();
  const presenter = player(null, 3, { transport: bus });
  const projector = player(scene, 3, { transport: bus, audience: true, controller: presenter.controller });
  presenter.evaluate(`
    let releaseRender;
    const completedPage = showPage;
    showPage = (number) => {
      completedPage(number);
      scene = null;
      pageRender = { finished: { promise: new Promise(resolve => { releaseRender = resolve; }) } };
    };
  `);
  projector.key("ArrowRight");
  projector.key("ArrowRight");
  assert.equal(presenter.state().pageIndex, 1);
  assert.equal(presenter.navigation.length, 1);
  presenter.evaluate("scene = {duration:10,checkpoints:[{time:0,label:'start'},{time:5,label:'middle'},{time:10,label:'end'}]}; pageRender = null; releaseRender();");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(presenter.navigation.length, 1);
  assert.equal(presenter.state().playing, true);
  assert.equal(presenter.state().playTarget, 5);
  assert.equal(projector.incoming.at(-1).page, 1);
});

test("a disconnected projector shows its state and does not change either timeline", () => {
  const bus = transport();
  const presenter = player(scene, 3, { transport: bus });
  const projector = player(scene, 3, { transport: bus, audience: true, controller: presenter.controller });
  presenter.event("pagehide", {});
  assert.equal(projector.evaluate("controllerConnected"), false);
  projector.key("ArrowRight", { ctrlKey: true });
  assert.equal(projector.evaluate("ui.connection.hidden"), false);
  assert.equal(presenter.state().time, 0);
  assert.equal(projector.state().time, 0);
});

test("file pairing accepts opaque message origins only from the projector's actual opener", () => {
  const bus = transport();
  const owner = player(scene, 2, { transport: bus, seed: 1 });
  const stranger = player(scene, 2, { transport: bus, seed: 2 });
  const projector = player(scene, 2, { transport: bus, audience: true, controller: owner.controller });
  projector.window.opener = owner.window;
  const reply = { type: "derivation-connected", path: "/one-page/index.html",
    controller: stranger.controller, ready: true };
  projector.event("message", { origin: "null", source: stranger.window, data: reply });
  assert.equal(projector.evaluate("controllerId"), owner.controller);
  projector.event("message", { origin: "https://other.invalid", source: owner.window, data: reply });
  assert.equal(projector.evaluate("controllerId"), owner.controller);
  // The same opener after reload supplies a new document channel.
  projector.event("message", { origin: "null", source: owner.window, data: reply });
  assert.equal(projector.evaluate("controllerId"), stranger.controller);
  assert.equal(projector.evaluate("controllerConnected"), true);
});

test("HTTP pairing requires its document origin as well as the actual opener", () => {
  const bus = transport();
  const owner = player(scene, 2, { transport: bus });
  const projector = player(scene, 2, { transport: bus, audience: true, controller: owner.controller,
    protocol: "https:", origin: "https://presentation.example" });
  projector.window.opener = owner.window;
  const replacement = "02".repeat(16);
  const data = { type: "derivation-connected", path: "/one-page/index.html", controller: replacement, ready: true };
  projector.event("message", { origin: "null", source: owner.window, data });
  assert.equal(projector.evaluate("controllerId"), owner.controller);
  projector.event("message", { origin: "https://presentation.example", source: owner.window, data });
  assert.equal(projector.evaluate("controllerId"), replacement);
});
