// The same path/keyframe model consumed by pdfpc's native Cairo renderer.
// Rendering order, cubic/quadratic paths, and interpolation follow vector_scene.vala.
function mix(a, b, amount) {
  return a + (b - a) * amount;
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
  if (!source || !Array.isArray(source.objects) || source.width <= 0 || source.height <= 0) {
    throw new Error("Invalid vector scene");
  }
  const objects = source.objects.map((item) => {
    let geometry;
    const frames = item.keyframes.map((frame) => {
      if (frame.coords !== undefined) geometry = frame.coords;
      if (!geometry) throw new Error("No first geometry for " + item.id);
      return { ...frame, coords: geometry };
    });
    const original = frames[0].coords;
    const fixed = frames.every((frame) => frame.coords.length === original.length &&
      frame.coords.every((value, i) => value === original[i]));
    return { ...item, frames, fixed, cachedPath: null, interpolated: new Float64Array(original.length) };
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
