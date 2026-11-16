import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { bracket, checkpointAt, prepareScene, renderSceneFitted, seekCheckpoint, stateAt } from "../vector.mjs";

const source = JSON.parse(readFileSync(new URL("../../tests/fixtures/format.vector.json", import.meta.url)));
const scenes = Object.fromEntries(source.scenes.map((entry) => [entry.id, prepareScene(entry)]));
const options = JSON.parse(readFileSync(new URL("../../tests/fixtures/format-options.vector.json", import.meta.url)));

test("omitted layers default to zero among explicit foreground and background layers", () => {
  const original = structuredClone(options.scenes[0]);
  const scene = prepareScene(original);
  assert.deepEqual(scene.objects.map((item) => [item.id, item.z]), [
    ["background-layer", -1], ["default-layer", 0], ["foreground", 1],
  ]);
  assert.deepEqual(original, options.scenes[0], "Preparation must not change the supplied timeline");
});

test("objects with omitted and explicit zero layers retain their input order", () => {
  const scene = prepareScene(options.scenes[1]);
  assert.deepEqual(scene.objects.map((item) => item.id), ["first-default", "explicit-zero", "last-default"]);
  assert.deepEqual(scene.objects.map((item) => item.z), [0, 0, 0]);
});

test("sparse quadratic geometry retains its coordinates and interpolated style", () => {
  const scene = prepareScene(options.scenes[2]);
  assert.equal(scene.objects[0].fixed, true);
  assert.equal(scene.objects[1].fixed, false);
  const state = stateAt(scene.objects[0], 1);
  assert.deepEqual(state.coords, options.scenes[2].objects[0].keyframes[0].coords);
  assert.equal(state.strokeWidth, 2);
  assert.equal(state.fill[3], 0.75);
});

test("missing or invalid mandatory stroke widths fail before interpolation", () => {
  for (const value of [undefined, null, "2", false, -1, NaN, Infinity, 1e9 + 1]) {
    const scene = structuredClone(options.scenes[2]);
    const frame = scene.objects[0].keyframes[1];
    if (value === undefined) delete frame.stroke_width;
    else frame.stroke_width = value;
    assert.throws(() => prepareScene(scene), /stroke_width/, String(value));
  }
});

test("explicit layers must be numbers within the native format's finite range", () => {
  for (const value of [null, "1", false, NaN, Infinity, -Infinity, 1e9 + 1, -1e9 - 1]) {
    const scene = structuredClone(options.scenes[0]);
    scene.objects[0].z = value;
    assert.throws(() => prepareScene(scene), /z/, String(value));
  }
});

test("shared native/browser fixture preserves authored checkpoints", () => {
  assert.deepEqual(Object.keys(scenes), ["format-sample"]);
  assert.deepEqual(scenes["format-sample"].checkpoints.map((x) => x.time), [0, 0.8, 2]);
  for (const scene of Object.values(scenes)) {
    assert.equal(scene.checkpoints.at(-1).time, scene.duration);
    assert.equal(checkpointAt(scene, scene.duration), scene.checkpoints.length - 1);
    assert.equal(seekCheckpoint(scene, 0, 1), scene.checkpoints[1].time);
    assert.equal(seekCheckpoint(scene, scene.duration, -1), scene.checkpoints.at(-2).time);
    assert.ok(scene.objects.length > 0);
  }
});

test("interpolation preserves frame endpoints and intermediate color states", () => {
  for (const scene of Object.values(scenes)) {
    let moved = false;
    for (const object of scene.objects) {
      const frames = object.frames;
      assert.equal(frames[0].time, 0);
      for (let i = 1; i < frames.length; i++) {
        assert.ok(frames[i].time > frames[i - 1].time);
      }
      const first = stateAt(object, 0);
      const last = stateAt(object, scene.duration);
      assert.deepEqual([...first.coords], frames[0].coords);
      assert.deepEqual([...last.coords], frames.at(-1).coords);
      if (!object.fixed && frames.length > 1) moved = true;
      if (frames.length > 1) {
        const a = frames[0], b = frames[1];
        const middle = stateAt(object, (a.time + b.time) / 2);
        assert.ok(Math.abs(middle.fill[3] - (a.fill[3] + b.fill[3]) / 2) < 1e-7);
      }
    }
    assert.ok(moved, "scene must contain moving geometry");
  }
});

test("binary search clamps to last state and uses correct interpolation interval", () => {
  const frames = [{ time: 0 }, { time: 2 }, { time: 6 }];
  assert.deepEqual(bracket(frames, -1), { a: frames[0], b: frames[1], alpha: 0 });
  assert.equal(bracket(frames, 1).alpha, 0.5);
  assert.equal(bracket(frames, 4).alpha, 0.5);
  assert.equal(bracket(frames, 50).alpha, 0);
  assert.equal(bracket(frames, 50).a, frames[2]);
});

test("scene fitting uses a centered uniform scale and restores its context", () => {
  for (const [width, height, offset, factor] of [
    [120, 80, [0, 0], 1], [120, 120, [0, 20], 1],
    [240, 80, [60, 0], 1], [240, 160, [0, 0], 2],
  ]) {
    const calls = [];
    const context = Object.fromEntries(["save", "restore", "beginPath", "rect", "clip", "translate", "scale", "fillRect"]
      .map((name) => [name, (...args) => calls.push([name, ...args])]));
    const scene = prepareScene({ ...structuredClone(options.scenes[0]), objects: [] });
    renderSceneFitted(context, scene, 1, width, height);
    assert.deepEqual(calls.filter((item) => item[0] === "translate"), [["translate", ...offset]]);
    assert.deepEqual(calls.filter((item) => item[0] === "scale"), [["scale", factor, factor]]);
    assert.deepEqual(calls.filter((item) => item[0] === "fillRect"), [["fillRect", 0, 0, 120, 80]]);
    assert.equal(calls.filter((item) => item[0] === "save").length, 2);
    assert.equal(calls.filter((item) => item[0] === "restore").length, 2);
  }
});
