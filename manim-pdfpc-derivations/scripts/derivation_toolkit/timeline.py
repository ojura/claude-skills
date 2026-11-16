"""Validate the pdfpc version-1 timeline before publishing compiled inputs.

This module uses only the Python standard library. The accepted numeric ranges,
defaults and sparse geometry rules follow pdfpc's VectorDeck/VectorScene parser.
It inspects the source without rewriting paths or keyframes.
"""
from __future__ import annotations

import json
from pathlib import Path


def _object(value, where):
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _member(value, name, where):
    if name not in value:
        raise ValueError(f"{where} is missing {name}")
    return value[name]


def _number(value, where, minimum=-1e9, maximum=1e9):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError(f"{where} must be a finite number between {minimum} and {maximum}")
    return value


def _string(value, where, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"{where} must be a {'nonempty ' if nonempty else ''}string")
    if "\x00" in value:
        raise ValueError(f"{where} must not contain NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{where} must be a well-formed Unicode string") from error
    return value


def _array(value, where, minimum, maximum):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{where} must have between {minimum} and {maximum} entries")
    return value


def _numbers(value, where, length, color=False):
    _array(value, where, length, length)
    for number in value:
        _number(number, where, 0 if color else -1e9, 1 if color else 1e9)
    return value


def validate_timeline(value):
    """Return scenes keyed by ID after validating the complete native contract."""
    root = _object(value, "Timeline")
    if _number(_member(root, "version", "Timeline"), "Timeline version") != 1:
        raise ValueError("Unsupported vector timeline version")
    scenes = {}
    for scene in _array(_member(root, "scenes", "Timeline"), "Timeline scenes", 1, 1000):
        _object(scene, "Scene")
        identifier = _string(_member(scene, "id", "Scene"), "Scene id", True)
        where = f"Scene {identifier!r}"
        if identifier in scenes:
            raise ValueError(f"Duplicate scene ID: {identifier!r}")
        width = _number(_member(scene, "width", where), where + " width")
        height = _number(_member(scene, "height", where), where + " height")
        duration = _number(_member(scene, "duration", where), where + " duration", 0, 86400)
        if width <= 0 or height <= 0 or duration <= 0:
            raise ValueError(f"{where} dimensions and duration must be positive")
        background = _numbers(_member(scene, "background", where), where + " background", 4, True)
        if background[3] != 1:
            raise ValueError(f"{where} background must be opaque")
        points = _array(_member(scene, "checkpoints", where), where + " checkpoints", 2, 10000)
        previous = -1
        for index, point in enumerate(points):
            _object(point, where + " checkpoint")
            time = _number(_member(point, "time", where), where + " checkpoint time", 0, duration)
            _string(_member(point, "label", where), where + " checkpoint label")
            if (index == 0 and time != 0) or time <= previous:
                raise ValueError(f"{where} checkpoints must start at zero and increase")
            previous = time
        if abs(previous - duration) > 1e-6:
            raise ValueError(f"{where} final checkpoint must equal duration")
        seen = set()
        for item in _array(_member(scene, "objects", where), where + " objects", 0, 20000):
            _object(item, where + " object")
            name = _string(_member(item, "id", where), where + " object id", True)
            path_where = f"{where}, object {name!r}"
            if name in seen:
                raise ValueError(f"{where} has duplicate object ID {name!r}")
            seen.add(name)
            _number(item.get("z", 0), path_where + " z")
            commands = _string(_member(item, "commands", path_where), path_where + " commands", True)
            if commands[0] != "M":
                raise ValueError(f"{path_where} path must begin with M")
            coordinate_count = 0
            for command in commands:
                count = {"M": 2, "L": 2, "C": 6, "Q": 4, "Z": 0}.get(command)
                if count is None:
                    raise ValueError(f"{path_where} has unsupported path command {command!r}")
                coordinate_count += count
                if coordinate_count > 200000:
                    raise ValueError(f"{path_where} has too many coordinates")
            if item.get("fill_rule", "nonzero") not in ("nonzero", "evenodd"):
                raise ValueError(f"{path_where} has unsupported fill_rule")
            frames = _array(_member(item, "keyframes", path_where), path_where + " keyframes", 1, 200000)
            previous = -1
            for index, frame in enumerate(frames):
                _object(frame, path_where + " keyframe")
                time = _number(_member(frame, "time", path_where), path_where + " time", 0, duration + 1e-6)
                if (index == 0 and time != 0) or time <= previous:
                    raise ValueError(f"{path_where} keyframes must start at zero and increase")
                previous = time
                if "coords" in frame:
                    _numbers(frame["coords"], path_where + " coordinates", coordinate_count)
                elif index == 0:
                    raise ValueError(f"{path_where} first keyframe must supply coordinates")
                _numbers(_member(frame, "fill", path_where), path_where + " fill", 4, True)
                _numbers(_member(frame, "stroke", path_where), path_where + " stroke", 4, True)
                _number(_member(frame, "stroke_width", path_where), path_where + " stroke_width", 0)
        scenes[identifier] = scene
    return scenes


def load_timeline(path: Path | None):
    """Load a supplied native timeline, or return no scenes for a static input."""
    if path is None:
        return {}
    def invalid_constant(value):
        raise ValueError(f"Invalid JSON numeric constant: {value}")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=invalid_constant)
        return validate_timeline(value)
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"Invalid vector timeline {path}: {error}") from error


def validate_scene_links(pages, scenes):
    """Check each actual PDF URI against the supplied validated scene table."""
    for index, links in enumerate(pages, 1):
        vectors = [uri[len("pdfpc-vector:"):] for uri in links if uri.startswith("pdfpc-vector:")]
        if len(vectors) > 1:
            raise ValueError(f"PDF page {index} contains more than one animation region")
        for identifier in vectors:
            if identifier not in scenes:
                raise ValueError(f"PDF page {index} links to unknown scene {identifier!r}; supply its matching timeline")
