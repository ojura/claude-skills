import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as vector from "../vector.mjs";

const source = JSON.parse(readFileSync(new URL("../../tests/fixtures/format.vector.json", import.meta.url)));
const prepare = (value) => vector.prepareTimeline(value);

test("a complete timeline retains scene IDs without changing input", () => {
  assert.equal(typeof vector.prepareTimeline, "function");
  const value = structuredClone(source);
  const scenes = prepare(value);
  assert.deepEqual([...scenes.keys()], ["format-sample"]);
  assert.equal(scenes.get("format-sample").objects.length, 1);
  assert.deepEqual(value, source);
});

test("timeline versions are numeric one and supplied native timelines are nonempty", () => {
  for (const version of [undefined, null, false, true, "1", 0, 2, NaN, Infinity]) {
    assert.throws(() => prepare({ ...source, version }), /version/i);
  }
  for (const scenes of [undefined, null, {}, [], Array(1001).fill(source.scenes[0])]) {
    assert.throws(() => prepare({ version: 1, scenes }), /scenes/i);
  }
});

test("duplicate scene IDs are rejected rather than silently replaced in a Map", () => {
  assert.throws(() => prepare({ ...source, scenes: [source.scenes[0], structuredClone(source.scenes[0])] }),
    /duplicate/i);
});

test("scene, object and checkpoint strings reject NUL and unpaired surrogates", () => {
  for (const bad of ["bad\u0000tail", "bad\ud800tail", "bad\udffftail"]) {
    for (const target of ["scene", "object", "checkpoint"]) {
      const scene = structuredClone(source.scenes[0]);
      if (target === "scene") scene.id = bad;
      else if (target === "object") scene.objects[0].id = bad;
      else scene.checkpoints[0].label = bad;
      assert.throws(() => vector.prepareScene(scene), /NUL|Unicode/, target + " " + JSON.stringify(bad));
    }
  }
});

test("valid astral Unicode and escaped identifiers survive browser preparation", () => {
  const scene = structuredClone(source.scenes[0]);
  scene.id = 'θ / 例 / 🧮 / ("A") \\ source';
  scene.objects[0].id = "符号😀";
  scene.checkpoints[0].label = "Start θ😀";
  for (const value of [scene, JSON.parse(JSON.stringify(scene))]) {
    const prepared = vector.prepareScene(value);
    assert.equal(prepared.id, scene.id);
    assert.equal(prepared.objects[0].id, scene.objects[0].id);
    assert.equal(prepared.checkpoints[0].label, scene.checkpoints[0].label);
  }
});
