// The same path/keyframe model consumed by pdfpc's native Cairo renderer.
// Rendering order, cubic/quadratic paths, and interpolation follow vector_scene.vala.
function mix(a, b, amount) {
  return a + (b - a) * amount;
}

function number(value, name, minimum = -1e9, maximum = 1e9) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new Error(name + " must be a finite number between " + minimum + " and " + maximum);
  }
  return value;
}

function object(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(name + " must be an object");
  return value;
}

function string(value, name, nonempty = false) {
  if (typeof value !== "string" || (nonempty && !value.length)) throw new Error(name + " must be a string" + (nonempty ? " with content" : ""));
  return value;
}

function array(value, name, minimum, maximum) {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    throw new Error(name + " must contain between " + minimum + " and " + maximum + " entries");
  }
  return value;
}

function numbers(value, name, length, color = false) {
  array(value, name, length, length);
  for (const component of value) number(component, name, color ? 0 : -1e9, color ? 1 : 1e9);
  return value;
}

export function bracket(keyframes, time) {
  if (!keyframes.length) throw new Error("Empty animation track");
  let lo = 0;
  let hi = keyframes.length - 1;
  while (lo < hi) {
    const mid = Math.floor((lo + hi + 1) / 2);
    if (keyframes[mid].time <= time) lo = mid;
    else hi = mid - 1;
  }
  const next = Math.min(lo + 1, keyframes.length - 1);
  const a = keyframes[lo];
  const b = keyframes[next];
  const alpha = lo === next || time <= a.time ? 0 :
    Math.max(0, Math.min(1, (time - a.time) / (b.time - a.time)));
  return { a, b, alpha };
}

export function prepareScene(source) {
  object(source, "Scene");
  string(source.id, "Scene id", true);
  number(source.width, "Scene width");
  number(source.height, "Scene height");
  number(source.duration, "Scene duration", 0, 86400);
  if (source.width <= 0 || source.height <= 0 || source.duration <= 0) {
    throw new Error("Scene dimensions and duration must be positive");
  }
  numbers(source.background, "Scene background", 4, true);
  if (source.background[3] !== 1) throw new Error("Scene background must be opaque");
  const checkpoints = array(source.checkpoints, "Checkpoints", 2, 10000);
  for (let i = 0; i < checkpoints.length; i++) {
    const point = object(checkpoints[i], "Checkpoint");
    number(point.time, "Checkpoint time", 0, source.duration);
    string(point.label, "Checkpoint label");
    if ((i === 0 && point.time !== 0) || (i > 0 && point.time <= checkpoints[i - 1].time)) {
      throw new Error("Checkpoint times must start at zero and increase");
    }
  }
  if (Math.abs(checkpoints.at(-1).time - source.duration) > 1e-6) {
    throw new Error("Final checkpoint must equal scene duration");
  }
  const identifiers = new Set();
  const objects = array(source.objects, "Scene objects", 0, 20000).map((item) => {
    object(item, "Vector object");
    string(item.id, "Object id", true);
    if (identifiers.has(item.id)) throw new Error("Duplicate object id: " + item.id);
    identifiers.add(item.id);
    const z = Object.hasOwn(item, "z") ? number(item.z, "Object z") : 0;
    const commands = string(item.commands, "Path commands", true);
    if (commands[0] !== "M") throw new Error("A path must start with M");
    const counts = { M: 2, L: 2, C: 6, Q: 4, Z: 0 };
    let coordinateCount = 0;
    for (const command of commands) {
      if (!Object.hasOwn(counts, command)) throw new Error("Unsupported path command: " + command);
      coordinateCount += counts[command];
    }
    if (coordinateCount > 200000) throw new Error("Path has too many coordinates");
    const fillRule = Object.hasOwn(item, "fill_rule") ? item.fill_rule : "nonzero";
    if (fillRule !== "nonzero" && fillRule !== "evenodd") throw new Error("Invalid fill_rule");
    let geometry;
    const frames = array(item.keyframes, "Keyframes", 1, 200000).map((frame, index) => {
      object(frame, "Keyframe");
      number(frame.time, "Keyframe time", 0, source.duration + 1e-6);
      if ((index === 0 && frame.time !== 0) || (index > 0 && frame.time <= item.keyframes[index - 1].time)) {
        throw new Error("Keyframe times must start at zero and increase");
      }
      if (Object.hasOwn(frame, "coords")) geometry = numbers(frame.coords, "Path coordinates", coordinateCount);
      if (!geometry) throw new Error("No first geometry for " + item.id);
      numbers(frame.fill, "Keyframe fill", 4, true);
      numbers(frame.stroke, "Keyframe stroke", 4, true);
      number(frame.stroke_width, "Keyframe stroke_width", 0);
      return { ...frame, coords: geometry };
    });
    const original = frames[0].coords;
    const fixed = frames.every((frame) => frame.coords.length === original.length &&
      frame.coords.every((value, i) => value === original[i]));
    return { ...item, z, fill_rule: fillRule, frames, fixed, cachedPath: null, interpolated: new Float64Array(original.length) };
  });
  objects.sort((a, b) => a.z - b.z);
  return { ...source, objects };
}

export function stateAt(object, time) {
  const { a, b, alpha } = bracket(object.frames || object.keyframes, time);
  let coords;
  if (object.fixed || alpha === 0 || a.coords === b.coords) {
    coords = a.coords;
  } else {
    coords = object.interpolated;
    for (let i = 0; i < coords.length; i++) coords[i] = mix(a.coords[i], b.coords[i], alpha);
  }
  const fill = a.fill.map((value, i) => mix(value, b.fill[i], alpha));
  const stroke = a.stroke.map((value, i) => mix(value, b.stroke[i], alpha));
  return { coords, fill, stroke, strokeWidth: mix(a.stroke_width, b.stroke_width, alpha) };
}

export function checkpointAt(scene, time) {
  let answer = 0;
  for (let i = 1; i < scene.checkpoints.length; i++) {
    if (scene.checkpoints[i].time <= time + 1e-6) answer = i;
    else break;
  }
  return answer;
}

export function seekCheckpoint(scene, time, direction) {
  const points = scene.checkpoints;
  if (direction > 0) {
    return points.find((point) => point.time > time + 0.0001)?.time ?? scene.duration;
  }
  return [...points].reverse().find((point) => point.time < time - 0.0001)?.time ?? 0;
}

export function makePath(commands, coords) {
  const path = new Path2D();
  let i = 0;
  for (const command of commands) {
    switch (command) {
      case "M": path.moveTo(coords[i++], coords[i++]); break;
      case "L": path.lineTo(coords[i++], coords[i++]); break;
      case "C":
        path.bezierCurveTo(coords[i++], coords[i++], coords[i++], coords[i++], coords[i++], coords[i++]);
        break;
      case "Q": path.quadraticCurveTo(coords[i++], coords[i++], coords[i++], coords[i++]); break;
      case "Z": path.closePath(); break;
      default: throw new Error("Unsupported vector path opcode: " + command);
    }
  }
  if (i !== coords.length) throw new Error("Path coordinate count mismatch");
  return path;
}

function cssColor(color) {
  return "rgba(" + color[0] * 255 + "," + color[1] * 255 + "," +
    color[2] * 255 + "," + color[3] + ")";
}

export function renderScene(ctx, scene, time) {
  const width = scene.width;
  const height = scene.height;
  const current = Math.max(0, Math.min(scene.duration, time));
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, width, height);
  ctx.clip();
  ctx.fillStyle = cssColor(scene.background);
  ctx.fillRect(0, 0, width, height);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  for (const object of scene.objects) {
    const state = stateAt(object, current);
    if (state.fill[3] <= 0 && (state.stroke[3] <= 0 || state.strokeWidth <= 0)) continue;
    let path = object.cachedPath;
    if (!path || !object.fixed) {
      path = makePath(object.commands, state.coords);
      if (object.fixed) object.cachedPath = path;
    }
    if (state.fill[3] > 0) {
      ctx.fillStyle = cssColor(state.fill);
      ctx.fill(path, object.fill_rule === "evenodd" ? "evenodd" : "nonzero");
    }
    if (state.stroke[3] > 0 && state.strokeWidth > 0) {
      ctx.strokeStyle = cssColor(state.stroke);
      ctx.lineWidth = state.strokeWidth;
      ctx.stroke(path);
    }
  }
  ctx.restore();
}

export function renderSceneFitted(ctx, scene, time, width, height) {
  if (!(width > 0 && height > 0 && Number.isFinite(width) && Number.isFinite(height))) {
    throw new Error("The vector region must have positive finite dimensions");
  }
  const scale = Math.min(width / scene.width, height / scene.height);
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, width, height);
  ctx.clip();
  ctx.translate((width - scene.width * scale) / 2, (height - scene.height * scale) / 2);
  ctx.scale(scale, scale);
  renderScene(ctx, scene, time);
  ctx.restore();
}
