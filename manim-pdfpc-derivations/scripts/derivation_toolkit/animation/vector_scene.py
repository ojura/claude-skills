"""Evaluate Manim animations and export their vector paths for pdfpc and PDF.

The Scene animation lifecycle is retained; its raster renderer and movie writer
are never called. Each path has sparse, complete keyframes. pdfpc interpolates
these cubic coordinates at the display refresh rate.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import json
import math
from pathlib import Path

import cairo
import numpy as np
from manim import Scene, VMobject, config
from .export_precision import COORDINATE_DECIMALS
from .layout import Canvas, DEFAULT_CANVAS, HandoutLayout
from .tex_semantics import snapshot_glyph, validate_text_transform, validate_text_tree


WIDTH, HEIGHT, SCALE = DEFAULT_CANVAS.width, DEFAULT_CANVAS.height, DEFAULT_CANVAS.scale


def rgba(color, alpha=1.0):
    return [round(float(v), 6) for v in (*color.to_rgb(), alpha)]


def canvas_points(points, canvas=DEFAULT_CANVAS):
    """One coordinate conversion for both visible paths and semantic metrics."""
    return canvas.points(points)


def vector_path(mobject, canvas=DEFAULT_CANVAS):
    """Keep glyph subpaths in one path so their holes use the winding rule."""
    commands = []
    coordinates = []
    for subpath in mobject.get_subpaths():
        if len(subpath) < 4:
            continue
        points = canvas.points(subpath)
        commands.append("M")
        coordinates.extend(points[0, :2])
        for i in range(0, len(points), 4):
            curve = points[i:i + 4]
            if len(curve) == 4:
                commands.append("C")
                coordinates.extend(curve[1:, :2].reshape(-1))
        if np.linalg.norm(points[0] - points[-1]) < 1e-5:
            commands.append("Z")
    return "".join(commands), [round(float(v), COORDINATE_DECIMALS) for v in coordinates]


def state_of(mobject, canvas=DEFAULT_CANVAS):
    commands, coordinates = vector_path(mobject, canvas)
    return commands, {
        "coords": coordinates,
        "fill": rgba(mobject.get_fill_color(), mobject.get_fill_opacity()),
        "stroke": rgba(mobject.get_stroke_color(), mobject.get_stroke_opacity()),
        "stroke_width": round(float(mobject.get_stroke_width()), 5),
    }


def hidden(state):
    result = deepcopy(state)
    result["fill"][3] = 0.0
    result["stroke"][3] = 0.0
    return result


class VectorScene(Scene):
    """A deterministic Manim scene with a vector recorder instead of frames."""

    scene_id = "scene"
    canvas = DEFAULT_CANVAS
    handout_layout = HandoutLayout()

    def __init__(self, palette, sample_rate=30):
        if not isinstance(self.canvas, Canvas):
            raise TypeError("A vector scene's canvas must be a Canvas")
        if not isinstance(self.handout_layout, HandoutLayout):
            raise TypeError("A vector scene's handout_layout must be a HandoutLayout")
        self.canvas.configure()
        self.palette = palette
        self.sample_rate = sample_rate
        self.vector_time = 0.0
        self.tracks = {}
        self.checkpoints = []
        self.handout_states = []
        self.last_states = {}
        self.recorded_mobjects = {}
        self.object_orders = {}
        self.object_layers = {}
        self._scene_orders = {}
        self._ordering_requested = False
        super().__init__()

    def add(self, *mobjects):
        # Manim registers an animation's new owner here, including introducers
        # begun later by Succession. Its existing children keep their paint
        # order even though Scene.add moves their new group in the scene list.
        # Re-adding an already-present object is an authored order operation,
        # so that permutation remains subject to record()'s rejection.
        before = self._path_members() if self.object_orders else None
        family = self.get_mobject_family_members() if before is not None else ()
        registration = (before is not None and not self._ordering_requested and
                        all(item not in family for item in mobjects))
        result = super().add(*mobjects)
        if registration:
            self._remember_registration(before)
        return result

    def _change_scene_order(self, operation, *mobjects):
        previous = self._ordering_requested
        self._ordering_requested = True
        try:
            return operation(*mobjects)
        finally:
            self._ordering_requested = previous

    def bring_to_front(self, *mobjects):
        return self._change_scene_order(super().bring_to_front, *mobjects)

    def bring_to_back(self, *mobjects):
        return self._change_scene_order(super().bring_to_back, *mobjects)

    def add_foreground_mobjects(self, *mobjects):
        return self._change_scene_order(super().add_foreground_mobjects, *mobjects)

    def record(self):
        now = round(self.vector_time, 6)
        active = {}
        paint_order = []
        seen = set()
        for top in self.mobjects:
            for mobject in top.family_members_with_points():
                if id(mobject) in seen:
                    continue
                seen.add(id(mobject))
                if not isinstance(mobject, VMobject):
                    raise TypeError("Only Manim vector mobjects can enter a vector scene")
                commands, state = state_of(mobject, self.canvas)
                if not commands:
                    continue
                # Topology changes get separate tracks. Our teaching scenes use
                # fixed-topology moves; this also handles Manim's arrow internals.
                key = (id(mobject), commands, len(state["coords"]))
                self.recorded_mobjects[id(mobject)] = mobject
                self._scene_orders.setdefault(id(mobject), len(self._scene_orders))
                if id(mobject) not in self.object_orders:
                    self.object_orders[id(mobject)] = len(self.object_orders)
                    self.object_layers[id(mobject)] = float(mobject.z_index)
                elif self.object_layers[id(mobject)] != float(mobject.z_index):
                    raise ValueError("Vector scenes require a fixed z_index for every path")
                active[key] = state
                if key not in self.tracks:
                    track = {
                        "id": f"path-{len(self.tracks):04d}",
                        "z": float(mobject.z_index) * 100000 + self.object_orders[id(mobject)],
                        "commands": commands,
                        "fill_rule": "nonzero",
                        "keyframes": [],
                    }
                    self.tracks[key] = track
                    if now > 0:
                        track["keyframes"].append({"time": 0.0, **hidden(state)})
                        if now > 1 / self.sample_rate:
                            track["keyframes"].append({
                                "time": round(now - 1 / self.sample_rate, 6),
                                **hidden(state),
                            })
                self._append_state(key, state, now)
                if state["fill"][3] > 0 or (state["stroke"][3] > 0 and state["stroke_width"] > 0):
                    paint_order.append((float(mobject.z_index), self._scene_orders[id(mobject)]))
        # Author changes to visible traversal order are unsupported. Automatic
        # animation grouping updates only _scene_orders; object_orders remains
        # the common fixed drawing order for timelines and full snapshots.
        ranks = sorted(paint_order, key=lambda item: item[0])
        if ranks != sorted(ranks):
            raise ValueError("Vector scenes cannot reorder visible paths within one z_index")
        for key in self.last_states.keys() - active.keys():
            state = hidden(self.last_states[key])
            self._append_state(key, state, now)
            active[key] = state
        self.last_states = active

    def _path_members(self):
        return list({id(member): member for top in self.mobjects
                     for member in top.family_members_with_points()}.values())

    def _remember_registration(self, before):
        """Accept Manim's ownership regrouping without changing path occlusion.

        Registering a new group reparents its existing children so updaters
        and removal apply to that group. Its scene traversal can therefore
        change around other same-layer objects. Apply only that registration
        permutation to the expected traversal. An earlier
        authored reorder of hidden paths must still fail when they reappear.
        Absent objects retain their previous slots and ordering history.
        """
        after = self._path_members()
        if [id(member) for member in before] == [id(member) for member in after]:
            return
        layers = {}
        for member in before:
            identifier = id(member)
            self._scene_orders.setdefault(identifier, len(self._scene_orders))
            layers.setdefault(float(member.z_index), []).append(identifier)
        expected = {}
        for identifiers in layers.values():
            expected.update(zip(identifiers, sorted(identifiers, key=self._scene_orders.__getitem__)))
        members = [expected.get(id(member), id(member)) for member in after]
        for identifier in members:
            self._scene_orders.setdefault(identifier, len(self._scene_orders))
        active = set(members)
        replacement = iter(members)
        order = [next(replacement) if identifier in active else identifier
                 for identifier in self._scene_orders]
        self._scene_orders = {identifier: rank for rank, identifier in enumerate(order)}

    def _append_state(self, key, state, now):
        frames = self.tracks[key]["keyframes"]
        entry = {"time": now, **state}
        if frames and frames[-1]["time"] == now:
            frames[-1] = entry
        elif len(frames) >= 2 and all(
            frames[-1][name] == frames[-2][name] == state[name]
            for name in state
        ):
            # Keep the first and last sample of a hold, so interpolation never
            # starts moving a path before the Manim animation begins.
            frames[-1] = entry
        else:
            frames.append(entry)

    def play(self, *animations, **kwargs):
        self.record()
        self.animations = self.compile_animations(*animations, **kwargs)
        # Match Manim's scene setup: a newly animated group must own its existing
        # children before cleanup can remove it, and unadded animations must appear.
        self.add_mobjects_from_animations(self.animations)
        for animation in self.animations:
            animation._setup_scene(self)
            animation.begin()
            # FadeIn/FadeOut create their actual same-content target in begin();
            # validate that target before any animation interpolation is sampled.
            validate_text_transform(animation)
        self.record()
        duration = max(animation.run_time for animation in self.animations)
        steps = max(1, math.ceil(duration * self.sample_rate))
        start = self.vector_time
        self.last_t = 0.0
        for step in range(1, steps + 1):
            elapsed = duration * step / steps
            self.update_to_time(elapsed)
            self.vector_time = start + elapsed
            self.record()
        for animation in self.animations:
            animation.finish()
            animation.clean_up_from_scene(self)
        self.update_mobjects(0)
        self.record()
        return self

    def wait(self, duration=0.3, **_kwargs):
        self.record()
        self.vector_time += float(duration)
        self.record()
        return self

    def checkpoint(self, label, caption, crop=None, handout=None, pause=True):
        """Save a static teaching state, optionally making it a live pause."""
        self.record()
        now = round(self.vector_time, 6)
        if pause:
            self.checkpoints.append({"time": now, "label": label})
        if handout is None:
            handout = self.current_paths()
        else:
            handout = paths_from_mobjects(handout, self.canvas)
        self.handout_states.append({
            "time": now, "caption": caption,
            "crop": crop or self.canvas.crop, "paths": handout,
            "full_paths": self.current_paths(),
        })

    def current_paths(self):
        return paths_from_mobjects(self.mobjects, self.canvas, order=self.object_orders)

    def export(self):
        self.record()
        objects = []
        for track in self.tracks.values():
            if all(frame["fill"][3] == 0 and frame["stroke"][3] == 0
                   for frame in track["keyframes"]):
                continue
            frames = track["keyframes"]
            if len(frames) == 2 and all(frames[0][k] == frames[1][k]
                                        for k in frames[0] if k != "time"):
                track["keyframes"] = frames[:1]
            objects.append(track)
        return {
            "id": self.scene_id, "width": self.canvas.width, "height": self.canvas.height,
            "background": rgba(self.camera.background_color),
            "duration": round(self.vector_time, 6),
            "checkpoints": self.checkpoints, "objects": objects,
        }


def paths_from_mobjects(mobjects, canvas=DEFAULT_CANVAS, *, order=None):
    mobjects = list(mobjects)
    validate_text_tree(mobjects)
    result = []
    seen = set()
    members = []
    for top in mobjects:
        for mobject in top.family_members_with_points():
            if id(mobject) not in seen:
                seen.add(id(mobject))
                members.append(mobject)
    ranks = order or {}
    for mobject in sorted(members, key=lambda item: (
            item.z_index, ranks.get(id(item), math.inf))):
        commands, state = state_of(mobject, canvas)
        if commands:
            item = {"commands": commands, "fill_rule": "nonzero", **state}
            glyph = snapshot_glyph(mobject, point_transform=canvas.points)
            if glyph is not None:
                item["glyph"] = glyph
            result.append(item)
    return result


def state_at(track, time):
    frames = track["keyframes"]
    index = max(0, bisect_right([f["time"] for f in frames], time) - 1)
    first = dict(frames[index])
    first["coords"] = next(frame["coords"] for frame in reversed(frames[:index + 1])
                           if "coords" in frame)
    if index == len(frames) - 1:
        return first
    second = dict(frames[index + 1])
    second.setdefault("coords", first["coords"])
    alpha = (time - first["time"]) / (second["time"] - first["time"])
    alpha = min(1.0, max(0.0, alpha))
    result = {"time": time}
    for key in ("coords", "fill", "stroke"):
        result[key] = [a + (b - a) * alpha for a, b in zip(first[key], second[key])]
    result["stroke_width"] = first["stroke_width"] + alpha * (second["stroke_width"] - first["stroke_width"])
    return result


def scene_paths_at(scene, time):
    return [{**track, **state_at(track, time)}
            for track in sorted(scene["objects"], key=lambda item: item["z"])]


def append_path(context, item):
    """Append the common cubic geometry without changing the paint source."""
    coords = iter(item["coords"])
    for command in item["commands"]:
        if command == "M":
            context.move_to(next(coords), next(coords))
        elif command == "L":
            context.line_to(next(coords), next(coords))
        elif command == "C":
            context.curve_to(*(next(coords) for _ in range(6)))
        elif command == "Z":
            context.close_path()
        else:
            raise ValueError(f"Unsupported path command {command}")
    context.set_fill_rule(cairo.FILL_RULE_EVEN_ODD if item.get("fill_rule") == "evenodd"
                          else cairo.FILL_RULE_WINDING)


def draw_paths(context, paths):
    for item in paths:
        if item["fill"][3] <= 0 and (item["stroke"][3] <= 0 or item["stroke_width"] <= 0):
            continue
        context.new_path()
        append_path(context, item)
        if item["fill"][3] > 0:
            context.set_source_rgba(*item["fill"])
            context.fill_preserve()
        if item["stroke"][3] > 0 and item["stroke_width"] > 0:
            context.set_source_rgba(*item["stroke"])
            context.set_line_width(item["stroke_width"])
            context.set_line_join(cairo.LINE_JOIN_ROUND)
            context.set_line_cap(cairo.LINE_CAP_ROUND)
            context.stroke()
        else:
            context.new_path()


def write_snapshot(destination, paths, background, crop=None, png_scale=None, *, canvas=DEFAULT_CANVAS):
    crop = crop or canvas.crop
    x, y, width, height = crop
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not png_scale:
        from .pdf_glyphs import write_pdf_snapshot
        write_pdf_snapshot(destination, paths, background, crop)
        return
    if png_scale:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                     round(width * png_scale), round(height * png_scale))
    context = cairo.Context(surface)
    if png_scale:
        context.scale(png_scale, png_scale)
    context.set_source_rgba(*background)
    context.paint()
    context.translate(-x, -y)
    draw_paths(context, paths)
    if png_scale:
        surface.write_to_png(str(destination))
    surface.finish()


def write_json(destination, value):
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, dict) and "scenes" in value:
        value = deepcopy(value)
        for scene in value["scenes"]:
            for track in scene.get("objects", []):
                previous = None
                for frame in track["keyframes"]:
                    if "coords" not in frame:
                        continue
                    coordinates = frame["coords"]
                    if coordinates == previous:
                        del frame["coords"]
                    else:
                        previous = coordinates
    Path(destination).write_text(json.dumps(value, separators=(",", ":")) + "\n")
