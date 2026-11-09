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
    this.listeners = new Map();
  }
  setAttribute() {}
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  click() {
    if (!this.disabled) this.listeners.get("click")?.({ target: this });
  }
}

function player(activeScene, pageCount = 1) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Control());
    return elements.get(id);
  };
  const keys = new Map();
  const context = vm.createContext({
    configurePdfWorker() {}, pdfjs: {},
    checkpointAt, prepareScene, renderScene, seekCheckpoint,
    document: { getElementById: element, querySelector: element },
    window: { addEventListener: (name, callback) => keys.set(name, callback) },
    location: { search: "", pathname: "/one-page/index.html" },
    URLSearchParams,
    ResizeObserver: class { observe() {} },
    HTMLSelectElement: class {}, HTMLInputElement: class {},
    requestAnimationFrame: () => 1,
    performance: { now: () => 0, timeOrigin: 0 },
    console,
    fixtureScene: activeScene, fixturePageCount: pageCount,
    navigation: [],
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
  ].join("\n"), context);
  return {
    previous: element("previous"), next: element("next"),
    seek: (at) => vm.runInContext("seek", context)(at),
    key: (key) => keys.get("keydown")({ key, preventDefault() {} }),
    state: () => vm.runInContext("({time, playTarget, playing, isBlank, pageIndex})", context),
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
