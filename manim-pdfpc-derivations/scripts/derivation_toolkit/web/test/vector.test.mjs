import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { bracket, checkpointAt, prepareScene, seekCheckpoint, stateAt } from "../vector.mjs";

const source = JSON.parse(readFileSync(new URL("../../tests/fixtures/format.vector.json", import.meta.url)));
const scenes = Object.fromEntries(source.scenes.map((entry) => [entry.id, prepareScene(entry)]));

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
