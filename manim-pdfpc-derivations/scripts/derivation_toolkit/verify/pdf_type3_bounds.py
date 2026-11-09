#!/usr/bin/env python3
"""Validate the filled Type3 glyph programs emitted by the vector PDF exporter.

This checks actual serialized paths, independently of the source bound writer.
The supported PDF operators are deliberately limited to Cairo's filled-vector
output. Unexpected geometry fails instead of receiving an incomplete estimate.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

from pypdf import PdfReader
from pypdf.generic import ContentStream, IndirectObject, NameObject, NumberObject


class Type3BoundsError(AssertionError):
    """A generated glyph has invalid, unsupported or unsafe metric geometry."""


IDENTITY = (1., 0., 0., 1., 0., 0.)
# A floating-point comparison allowance in conventional 1000-unit glyph space.
# Actual ink must have a positive outer margin, including at exact baseline 0.
BOUNDARY_EPSILON = 1e-7


def _numbers(values, count, operator):
    if len(values) != count:
        raise Type3BoundsError(f"{operator}: expected {count} numeric operands")
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise Type3BoundsError(f"{operator}: invalid numeric operand") from error
    if not all(math.isfinite(value) for value in result):
        raise Type3BoundsError(f"{operator}: nonfinite coordinate")
    return result


def _point(matrix, x, y):
    a, b, c, d, e, f = matrix
    return _numbers((a*x + c*y + e, b*x + d*y + f), 2, "transformed point")


def _multiply(a, b):
    aa, ab, ac, ad, ae, af = a
    ba, bb, bc, bd, be, bf = b
    return _numbers((aa*ba+ac*bb, ab*ba+ad*bb, aa*bc+ac*bd,
                     ab*bc+ad*bd, aa*be+ac*bf+ae, ab*be+ad*bf+af), 6, "cm")


def _cubic_axis(p0, p1, p2, p3):
    # Interior extrema are roots of the cubic's quadratic derivative.
    a = -p0 + 3*p1 - 3*p2 + p3
    b = 2*(p0 - 2*p1 + p2)
    c = p1 - p0
    roots = []
    if abs(a) < 1e-14:
        if abs(b) > 1e-14:
            roots.append(-c/b)
    else:
        discriminant = b*b - 4*a*c
        if discriminant >= 0:
            q = -.5 * (b + math.copysign(math.sqrt(discriminant), b))
            roots.extend((q/a, c/q) if q else (-b/(2*a),))
    values = [p0, p3]
    for t in roots:
        if 0 < t < 1:
            u = 1-t
            values.append(u*u*u*p0 + 3*u*u*t*p1 + 3*u*t*t*p2 + t*t*t*p3)
    return min(values), max(values)


def _line(a, b):
    return min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])


def _union(boxes):
    if not boxes:
        return None
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _program(program, reader):
    try:
        operations = ContentStream(program, reader).operations
    except Exception as error:
        raise Type3BoundsError(f"unreadable glyph program: {error}") from error
    if not operations or operations[0][1] != b"d1":
        raise Type3BoundsError("a filled Type3 program must begin with d1")
    metrics = _numbers(operations[0][0], 6, "d1")
    if metrics[2] > metrics[4] or metrics[3] > metrics[5]:
        raise Type3BoundsError("d1 has reversed bounds")
    matrix, stack, path, painted = IDENTITY, [], [], []
    current = start = None
    arities = {b"q": 0, b"Q": 0, b"cm": 6, b"m": 2, b"l": 2, b"c": 6,
               b"v": 4, b"y": 4, b"h": 0, b"re": 4, b"f": 0,
               b"f*": 0, b"F": 0, b"n": 0}
    for operands, operator in operations[1:]:
        if operator not in arities:
            raise Type3BoundsError(f"unsupported Type3 geometry operator {operator!r}")
        values = _numbers(operands, arities[operator], operator.decode("ascii"))
        if operator == b"q":
            stack.append(matrix)
        elif operator == b"Q":
            if not stack:
                raise Type3BoundsError("unbalanced glyph Q")
            matrix = stack.pop()
        elif operator == b"cm":
            matrix = _multiply(matrix, values)
        elif operator == b"m":
            current = start = _point(matrix, *values)
        elif operator in (b"l", b"c", b"v", b"y", b"h"):
            if current is None or start is None:
                raise Type3BoundsError(f"{operator!r} has no current glyph path")
            if operator in (b"l", b"h"):
                target = start if operator == b"h" else _point(matrix, *values)
                path.append(_line(current, target))
            else:
                if operator == b"c":
                    p1, p2, target = (_point(matrix, *values[i:i+2]) for i in (0, 2, 4))
                elif operator == b"v":
                    p1, p2, target = current, _point(matrix, *values[:2]), _point(matrix, *values[2:])
                else:
                    p1, target = _point(matrix, *values[:2]), _point(matrix, *values[2:])
                    p2 = target
                xs = _cubic_axis(current[0], p1[0], p2[0], target[0])
                ys = _cubic_axis(current[1], p1[1], p2[1], target[1])
                path.append((xs[0], ys[0], xs[1], ys[1]))
            current = target
        elif operator == b"re":
            x, y, width, height = values
            corners = [_point(matrix, x, y), _point(matrix, x+width, y),
                       _point(matrix, x+width, y+height), _point(matrix, x, y+height)]
            path.extend(_line(corners[i], corners[(i+1) % 4]) for i in range(4))
            current = start = corners[0]
        elif operator in (b"f", b"f*", b"F", b"n"):
            if operator != b"n":
                painted.extend(path)
            path = []
            current = start = None
    if stack or path or current is not None:
        raise Type3BoundsError("unfinished glyph graphics state or path")
    return metrics, _union(painted)


def serialized_glyph_ink(program, reader):
    """Return analytic painted bounds in the program's glyph space, or None."""
    return _program(program, reader)[1]


def _unicode_map(font, reader):
    if "/ToUnicode" not in font:
        raise Type3BoundsError("Type3 glyphs have no source Unicode map")
    result = {}
    for operands, operator in ContentStream(font["/ToUnicode"], reader).operations:
        if operator == b"endbfrange":
            raise Type3BoundsError("unexpected range-based source Unicode map")
        if operator != b"endbfchar":
            continue
        if len(operands) % 2:
            raise Type3BoundsError("incomplete source Unicode pair")
        for code_value, text_value in zip(operands[::2], operands[1::2]):
            try:
                code = code_value.original_bytes
                text = text_value.original_bytes.decode("utf-16-be")
            except (AttributeError, UnicodeError) as error:
                raise Type3BoundsError("invalid source Unicode identity") from error
            if len(code) != 1 or code[0] in result or not text:
                raise Type3BoundsError("ambiguous or empty source Unicode identity")
            result[code[0]] = text
    if not result:
        raise Type3BoundsError("empty source Unicode map")
    return result


def _glyph_names(font):
    names, code = {}, None
    try:
        differences = font["/Encoding"]["/Differences"]
        programs = font["/CharProcs"]
    except (KeyError, TypeError) as error:
        raise Type3BoundsError("missing Type3 glyph encoding or programs") from error
    for value in differences:
        if isinstance(value, NumberObject):
            code = int(value)
        elif isinstance(value, NameObject) and code is not None and 0 <= code <= 255:
            if code in names or value not in programs:
                raise Type3BoundsError("ambiguous Type3 glyph encoding")
            names[code] = value
            code += 1
        else:
            raise Type3BoundsError("unexpected Type3 glyph encoding")
    if set(names.values()) != set(programs):
        raise Type3BoundsError("a Type3 program has no source encoding")
    return names


def _encloses(box, ink, label):
    if not (box[0] < ink[0] - BOUNDARY_EPSILON and
            box[1] < ink[1] - BOUNDARY_EPSILON and
            box[2] > ink[2] + BOUNDARY_EPSILON and
            box[3] > ink[3] + BOUNDARY_EPSILON):
        raise Type3BoundsError(f"{label} does not strictly enclose serialized ink: "
                               f"bounds={list(box)}, ink={list(ink)}")


def validate_type3_bounds(filename):
    """Check every active Type3 program in a generated PDF; return counts."""
    filename = Path(filename)
    reader = PdfReader(filename, strict=True)
    counts = {"type3_fonts": 0, "glyph_programs": 0, "painted_glyphs": 0, "blank_spaces": 0}
    visited_fonts, visited_resources = set(), set()

    def resources(value, location):
        value = value.get_object()
        if id(value) in visited_resources:
            return
        visited_resources.add(id(value))
        fonts = value["/Font"] if "/Font" in value else {}
        for name, reference in fonts.items():
            font = reference.get_object()
            if font.get("/Subtype") != "/Type3":
                continue
            identity = (reference.idnum, reference.generation) if isinstance(reference, IndirectObject) else id(font)
            if identity in visited_fonts:
                continue
            visited_fonts.add(identity)
            prefix = f"{filename}: {location}, font {name}"
            try:
                font_box = _numbers(font["/FontBBox"], 4, "FontBBox")
                if font_box[0] > font_box[2] or font_box[1] > font_box[3]:
                    raise Type3BoundsError("FontBBox has reversed bounds")
                names, text = _glyph_names(font), _unicode_map(font, reader)
                if set(names) != set(text):
                    raise Type3BoundsError("glyph encoding and source Unicode identities differ")
                text_by_name = {}
                for code, glyph in names.items():
                    text_by_name.setdefault(glyph, []).append(text[code])
                counts["type3_fonts"] += 1
                for glyph, reference in font["/CharProcs"].items():
                    try:
                        metrics, ink = _program(reference.get_object(), reader)
                        counts["glyph_programs"] += 1
                        if ink is None:
                            if not all(item.isspace() for item in text_by_name[glyph]):
                                raise Type3BoundsError("zero-ink glyph is not source-mapped whitespace")
                            counts["blank_spaces"] += 1
                        else:
                            _encloses(metrics[2:], ink, "d1")
                            _encloses(font_box, ink, "FontBBox")
                            counts["painted_glyphs"] += 1
                    except Type3BoundsError as error:
                        raise Type3BoundsError(f"glyph {glyph}: {error}") from error
            except Exception as error:
                raise Type3BoundsError(f"{prefix}: {error}") from error
        objects = value["/XObject"] if "/XObject" in value else {}
        for name, reference in objects.items():
            obj = reference.get_object()
            if obj.get("/Subtype") == "/Form" and "/Resources" in obj:
                resources(obj["/Resources"], f"{location}/{name}")

    for index, page in enumerate(reader.pages, 1):
        if "/Resources" in page:
            resources(page["/Resources"], f"page {index}")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        reports = {str(path): validate_type3_bounds(path) for path in args.pdf}
    except Type3BoundsError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "files": reports}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
