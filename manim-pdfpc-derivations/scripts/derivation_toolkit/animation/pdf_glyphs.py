"""Write the current Manim outlines as visible, selectable PDF font glyphs.

Text, advances and baselines come from the authored TeX layout.  The outlines
and paint style come from the same current path snapshot as the native player.
Cairo user fonts join these two parts without performing another text layout.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO
import math
from pathlib import Path
import tempfile

import cairocffi as cairo
import numpy as np
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ByteStringObject, ContentStream,
                           DictionaryObject, FloatObject, IndirectObject,
                           NameObject, NumberObject, TextStringObject)

from .tex_semantics import GlyphSnapshot, TextMappingError


TYPE3_UNITS_PER_EM = 1000


def _has_fill(path):
    return path["fill"][3] > 0


def _has_stroke(path):
    return path["stroke"][3] > 0 and path["stroke_width"] > 0


def _style(path):
    return (tuple(path["fill"]), tuple(path["stroke"]),
            path["stroke_width"], path.get("fill_rule", "nonzero"))


def _union(boxes):
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _control_bounds(points, radius=(0.0, 0.0)):
    """Enclose every cubic by its control hull, without raster quantization.

    A round stroke is the centerline's Minkowski sum with a disk.  After an
    affine transform its coordinate radii are the row norms of that transform.
    """
    points = np.asarray(points, dtype=float).reshape((-1, 2))
    if not len(points) or not np.isfinite(points).all():
        raise TextMappingError("A PDF glyph needs finite path control points")
    low, high = points.min(axis=0), points.max(axis=0)
    return (float(low[0]) - radius[0], float(low[1]) - radius[1],
            float(high[0]) + radius[0], float(high[1]) + radius[1])


def _outward_font_bounds(box):
    # Cairo writes these one-em path coordinates to three decimal places.
    # Enclose the control hull on that 1000-unit grid, then retain one outer
    # cell: an ink edge exactly on the cache boundary (notably baseline zero)
    # can otherwise lose its antialias coverage in Poppler's Cairo renderer.
    return tuple((math.floor(value * TYPE3_UNITS_PER_EM) - 1 if index < 2 else
                  math.ceil(value * TYPE3_UNITS_PER_EM) + 1) / TYPE3_UNITS_PER_EM
                 for index, value in enumerate(box))


def _intersects(bounds, crop):
    x, y, width, height = crop
    return (bounds[2] > x and bounds[3] > y and
            bounds[0] < x + width and bounds[1] < y + height)


def _canonical_point(point, decimals):
    values = tuple(round(float(value), decimals) for value in point)
    if len(values) != 2 or not np.isfinite(values).all():
        raise TextMappingError("A PDF glyph has invalid font coordinates")
    return tuple(0.0 if value == 0 else value for value in values)


def _matrix(glyph, decimals):
    x, y = (_canonical_point(vector, decimals) for vector in (glyph.em_x, glyph.em_y))
    matrix = np.array([[x[0], y[0]], [x[1], y[1]]])
    if abs(x[0] * y[1] - y[0] * x[1]) < 1e-14:
        raise TextMappingError("A PDF glyph has a singular or invalid font matrix")
    return matrix


def _inverse(matrix):
    # These two-dimensional export operations have a fixed scalar order.  A
    # BLAS/LAPACK core may otherwise change the last bit and Cairo's PDF rounding.
    a, b, c, d = map(float, matrix.flat)
    determinant = a * d - b * c
    values = (d / determinant, -b / determinant, -c / determinant, a / determinant)
    return np.array([0.0 if value == 0 else value for value in values]).reshape((2, 2))


def _apply_basis(matrix, point):
    a, b, c, d = map(float, matrix.flat)
    x, y = map(float, point)
    values = (a * x + b * y, c * x + d * y)
    return tuple(0.0 if value == 0 else value for value in values)


def _cairo_matrix(matrix):
    return cairo.Matrix(matrix[0, 0], matrix[1, 0],
                        matrix[0, 1], matrix[1, 1], 0, 0)


@dataclass
class _Glyph:
    position: int
    path: dict
    snapshot: GlyphSnapshot
    bounds: tuple[float, float, float, float]
    baseline: tuple[float, float]
    matrix: np.ndarray
    inverse: np.ndarray


@dataclass
class _Mask:
    paths: tuple[dict, ...]
    inverse: np.ndarray | None
    bounds: tuple[float, float, float, float]
    advance: float
    stroke: bool = False


@dataclass
class _Emission:
    glyph_index: int
    text: str
    font_key: tuple
    line_key: tuple
    source_indices: tuple
    base_key: tuple
    bounds: tuple[float, float, float, float]
    advance: float
    uses: int = 1


def _font_key(fonts):
    return tuple(sorted({(font.family, font.em_size, font.ascent, font.descent,
                          font.italic_angle, font.postscript_name, font.program_sha256)
                         for font in fonts}))


def _line_key(sources):
    # Keep the actual TeX run ownership separately from FontSource's whole-font
    # outline extrema. The enclosing hbox supplies this run's selection metrics.
    return tuple(sorted({(source.layout_key, source.line.box_index,
                          source.line.ascent, source.line.descent)
                         for source in sources}))


def _source_ownership(sources):
    """Retain every component and the source macro's declared semantic base."""
    if not sources or len({source.layout_key for source in sources}) != 1:
        raise TextMappingError("A PDF glyph needs one source layout")
    indices = tuple(sorted((source.layout_key, source.index) for source in sources))
    if len(set(indices)) != len(sources):
        raise TextMappingError("A PDF glyph repeats a source component")
    bases = {source.cluster_base for source in sources}
    if len(bases) != 1:
        raise TextMappingError("A composite TeX glyph has inconsistent semantic bases")
    base_index = bases.pop()
    base = [source for source in sources if source.index == base_index]
    if len(base) != 1:
        raise TextMappingError("A composite TeX glyph is missing its semantic base")
    source = base[0]
    return indices, (source.layout_key, source.index, _font_key([source.font])[0])


def _logical_vertical_bounds(lines):
    if not lines or any(not np.isfinite(line[2:]).all() or
                        line[2] < 0 or line[3] < 0 for line in lines):
        raise TextMappingError("A PDF glyph has invalid source line metrics")
    bounds = (-max(line[2] for line in lines), max(line[3] for line in lines))
    if bounds[0] >= bounds[1]:
        raise TextMappingError("A PDF glyph needs a nonempty source line box")
    return bounds


def _temporary_character(index):
    if not 1 <= index <= 65535:
        raise TextMappingError("A PDF glyph identity is outside the supported range")
    value = 0xF0000 + index - 1
    if value >= 0xFFFFE:
        value += 2  # U+FFFFE/U+FFFFF are noncharacters, not private-use scalars.
    return chr(value)


def _private_use(character):
    value = ord(character)
    return (0xE000 <= value <= 0xF8FF or 0xF0000 <= value <= 0xFFFFD
            or 0x100000 <= value <= 0x10FFFD)


def _objects(root):
    """Visit the active PDF object graph, retaining exact indirect identities."""
    seen = set()
    def visit(value):
        if isinstance(value, IndirectObject):
            key = (value.idnum, value.generation)
            if key in seen:
                return
            seen.add(key)
            value = value.get_object()
            yield key, value
        if isinstance(value, DictionaryObject):
            for child in value.values():
                yield from visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from visit(child)
    yield from visit(root)


def _unicode_entries(stream, pdf):
    """Parse Cairo's explicit single-byte ToUnicode CMap, without text guessing."""
    content = ContentStream(stream, pdf)
    entries, pending, code_space = [], None, False
    for operands, operator in content.operations:
        if operator == b"begincodespacerange":
            if operands != [1]:
                raise TextMappingError("Unexpected Type3 character-code space")
        elif operator == b"endcodespacerange":
            if (len(operands) != 2 or operands[0].original_bytes != b"\x00"
                    or operands[1].original_bytes != b"\xff"):
                raise TextMappingError("Type3 glyph identities require single-byte codes")
            operands[:] = [ByteStringObject(b"\x00"), ByteStringObject(b"\xff")]
            code_space = True
        elif operator == b"beginbfchar":
            if pending is not None or len(operands) != 1:
                raise TextMappingError("Malformed Type3 Unicode identity block")
            pending = int(operands[0])
        elif operator == b"endbfchar":
            if pending is None or len(operands) != 2 * pending:
                raise TextMappingError("Incomplete Type3 Unicode identity block")
            for offset in range(0, len(operands), 2):
                code = operands[offset].original_bytes
                if len(code) != 1:
                    raise TextMappingError("Unexpected Type3 glyph-code width")
                text = operands[offset + 1].original_bytes.decode("utf-16-be")
                entries.append((code[0], text, operands, offset + 1))
            pending = None
        elif operator in (b"beginbfrange", b"endbfrange"):
            raise TextMappingError("Cairo's explicit glyph identities require bfchar mappings")
    if not code_space or pending is not None or not entries:
        raise TextMappingError("Missing Type3 Unicode identity map")
    if len({code for code, *_ in entries}) != len(entries):
        raise TextMappingError("Duplicate Type3 character code")
    return content, entries


def _glyph_uses(pdf):
    """Count actual font/code uses, including repeated and nested Form uses."""
    uses = Counter()
    def walk(stream, resources, active_font=None, depth=0):
        if depth > 64:
            raise TextMappingError("Recursive PDF form in a vector snapshot")
        stack = []
        for operands, operator in ContentStream(stream, pdf).operations:
            if operator == b"q":
                stack.append(active_font)
            elif operator == b"Q":
                if not stack:
                    raise TextMappingError("Unbalanced PDF graphics state")
                active_font = stack.pop()
            elif operator == b"Tf":
                reference = resources["/Font"].raw_get(operands[0])
                if not isinstance(reference, IndirectObject):
                    raise TextMappingError("A Type3 font lost its indirect identity")
                active_font = (reference.idnum, reference.generation)
            elif operator in (b"Tj", b"TJ", b"'", b'"'):
                if active_font is None:
                    raise TextMappingError("PDF text has no source font")
                values = operands[0] if operator == b"TJ" else [operands[-1]]
                for value in values:
                    if isinstance(value, (ByteStringObject, TextStringObject)):
                        for code in value.original_bytes:
                            uses[(*active_font, code)] += 1
            elif operator == b"Do":
                form = resources["/XObject"][operands[0]]
                if form.get("/Subtype") == "/Form":
                    walk(form, form.get("/Resources", resources), active_font, depth + 1)
        if stack:
            raise TextMappingError("Unbalanced PDF graphics state")
    for page in pdf.pages:
        walk(page.get_contents(), page["/Resources"])
    return uses


def _stream_hashes(root):
    return {key: hashlib.sha256(value.get_data()).hexdigest()
            for key, value in _objects(root) if hasattr(value, "get_data")}


def _pdf_object_hash(value):
    data = BytesIO()
    value.write_to_stream(data)
    return hashlib.sha256(data.getvalue()).hexdigest()


def _font_dictionary_hash(font):
    return _pdf_object_hash(DictionaryObject({key: value for key, value in font.items()
        if key not in ("/FontBBox", "/FontMatrix", "/Widths", "/FontDescriptor")}))


def _type3_program_in_pdf_units(data, bounds):
    """Convert Cairo's one-em, y-down glyph program to 1000 units, y-up.

    The original drawing body stays byte-identical under the unit transform.
    Its inverse in FontMatrix preserves the complete drawing, including strokes.
    The source-owned bounds replace Cairo's separately quantized d1 box.  PDF
    requires d0/d1 to remain the first operator in each Type3 program.
    """
    header, separator, body = data.partition(b"\n")
    fields = header.split()
    if not separator or not fields or fields[-1] not in (b"d0", b"d1"):
        raise TextMappingError("A Cairo glyph program needs its initial d0/d1 metrics")
    operator, tokens = fields[-1], fields[:-1]
    if len(tokens) != (2 if operator == b"d0" else 6):
        raise TextMappingError("A Cairo glyph program has invalid metric operands")
    try:
        values = tuple(Decimal(token.decode("ascii")) for token in tokens)
    except (UnicodeDecodeError, InvalidOperation) as error:
        raise TextMappingError("A Cairo glyph program has invalid numeric metrics") from error
    if any(not value.is_finite() for value in values):
        raise TextMappingError("A Cairo glyph program has nonfinite metrics")
    converted = [values[0] * TYPE3_UNITS_PER_EM, -values[1] * TYPE3_UNITS_PER_EM]
    if operator == b"d1":
        if values[2] > values[4] or values[3] > values[5]:
            raise TextMappingError("A Cairo glyph program has reversed bounds")
        source = tuple(Decimal(str(value)) for value in bounds)
        if (not all(value.is_finite() for value in source) or
                source[0] > source[2] or source[1] > source[3]):
            raise TextMappingError("A PDF glyph has invalid source bounds")
        converted.extend((source[0] * TYPE3_UNITS_PER_EM, -source[3] * TYPE3_UNITS_PER_EM,
                          source[2] * TYPE3_UNITS_PER_EM, -source[1] * TYPE3_UNITS_PER_EM))

    def number(value):
        if value == 0:
            return b"0"
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text.encode("ascii")

    prefix = b" ".join([*(number(value) for value in converted), operator])
    prefix += f"\nq\n{TYPE3_UNITS_PER_EM} 0 0 -{TYPE3_UNITS_PER_EM} 0 0 cm\n".encode("ascii")
    result = prefix + body + b"\nQ\n"
    return result, {
        "metric_operator": operator.decode("ascii"),
        "original_metrics": [str(value) for value in values],
        "source_bounds": list(bounds),
        "converted_metrics": [str(value) for value in converted],
        "drawing_body_sha256": hashlib.sha256(body).hexdigest(),
        "before_sha256": hashlib.sha256(data).hexdigest(),
        "after_sha256": hashlib.sha256(result).hexdigest(),
    }


def _type3_descriptor(name, angle, vertical_bounds):
    """Describe the source line and current ink in 1000-unit glyph coordinates."""
    if not np.isfinite(angle):
        raise TextMappingError("A PDF font needs a finite source italic angle")
    lower, upper = vertical_bounds
    return DictionaryObject({
        NameObject("/Type"): NameObject("/FontDescriptor"),
        NameObject("/FontName"): NameObject(name),
        # These user fonts have an explicit custom encoding.  Their italic
        # property comes from the original TeX font, before scene transforms.
        NameObject("/Flags"): NumberObject(4 | (64 if angle != 0 else 0)),
        NameObject("/ItalicAngle"): FloatObject(angle),
        NameObject("/Ascent"): FloatObject(-lower * TYPE3_UNITS_PER_EM),
        NameObject("/Descent"): FloatObject(-upper * TYPE3_UNITS_PER_EM),
    })


def _encoded_glyph_names(font):
    names, code = {}, None
    for value in font["/Encoding"]["/Differences"]:
        if isinstance(value, NumberObject):
            code = int(value)
        elif isinstance(value, NameObject) and code is not None and 0 <= code <= 255:
            if code in names or value not in font["/CharProcs"]:
                raise TextMappingError("A Type3 glyph has an ambiguous source encoding")
            names[code] = value
            code += 1
        else:
            raise TextMappingError("Unexpected Cairo Type3 glyph encoding")
    return names


def _normalize_font_metadata(pdf_bytes, emissions, *, audit=None):
    """Resolve source glyphs and publish coherent 1000-unit Type3 font metadata.

    Cairo expands Type3 bounds through device-axis boxes for rotated fonts.
    Each emitted glyph therefore carries a unique temporary Unicode scalar.
    Its actual font/code mapping identifies the source record without relying on
    dictionary order, font names, text extraction, or glyph-paint order.  The
    original drawing body and inverse FontMatrix retain the visible geometry.
    """
    reader = PdfReader(BytesIO(pdf_bytes), strict=True)
    writer = PdfWriter(reader, incremental=True)
    root = writer.root_object.indirect_reference
    before_streams = _stream_hashes(root)
    uses = _glyph_uses(writer)
    seen, expected_changes, changed_cmaps = set(), set(), set()
    records, immutable_fonts, expected_maps = [], {}, {}
    expected_fonts, expected_descriptors, programs = {}, {}, {}
    for identity, font in _objects(root):
        if not isinstance(font, DictionaryObject) or font.get("/Type") != "/Font":
            continue
        if font.get("/Subtype") != "/Type3":
            raise TextMappingError("A vector snapshot contains an unexpected font type")
        if [float(value) for value in font["/FontMatrix"]] != [1, 0, 0, -1, 0, 0]:
            raise TextMappingError("Unexpected Cairo Type3 font coordinate system")
        if "/FontDescriptor" in font:
            raise TextMappingError("A Cairo user font already has unexpected descriptor metadata")
        immutable_fonts[identity] = _font_dictionary_hash(font)
        cmap_reference = font.raw_get("/ToUnicode")
        if not isinstance(cmap_reference, IndirectObject):
            raise TextMappingError("A Type3 Unicode map lost its indirect identity")
        cmap_identity = (cmap_reference.idnum, cmap_reference.generation)
        cmap, entries = _unicode_entries(font["/ToUnicode"], writer)
        glyph_names = _encoded_glyph_names(font)
        glyph_bounds = {}
        sources, lines, logical_bounds, bounds, mapping = set(), set(), set(), [], []
        metric_fonts, bases = set(), set()
        expected_maps[identity] = {}
        for code, marker, operands, offset in entries:
            emission = emissions.get(marker)
            if emission is None or marker in seen:
                raise TextMappingError("Unknown or duplicate emitted PDF glyph identity")
            if not emission.text or any(_private_use(char) for char in emission.text):
                raise TextMappingError("A published PDF glyph needs its source Unicode text")
            if (not emission.source_indices or
                    len(set(emission.source_indices)) != len(emission.source_indices) or
                    emission.base_key[:2] not in emission.source_indices or
                    emission.base_key[2] not in emission.font_key or
                    {source[0] for source in emission.source_indices} !=
                    {line[0] for line in emission.line_key}):
                raise TextMappingError("An emitted PDF glyph has an invalid semantic base")
            seen.add(marker)
            first, last = int(font["/FirstChar"]), int(font["/LastChar"])
            if not first <= code <= last:
                raise TextMappingError("Type3 glyph identity has no declared advance")
            width = float(font["/Widths"][code - first])
            if abs(width - emission.advance) > 1e-5:
                raise TextMappingError("Type3 glyph advance differs from its source identity")
            if uses.pop((*identity, code), 0) != emission.uses:
                raise TextMappingError("Type3 glyph use count differs from its source identity")
            sources.add(emission.font_key)
            metric_fonts.add(emission.base_key[2])
            bases.add(emission.base_key)
            lines.update(emission.line_key)
            logical_bounds.add(_logical_vertical_bounds(emission.line_key))
            bounds.append(emission.bounds)
            glyph_name = glyph_names.get(code)
            if glyph_name is None or glyph_name in glyph_bounds:
                raise TextMappingError("A Type3 glyph lost its source-owned bounds")
            glyph_bounds[glyph_name] = emission.bounds
            operands[offset - 1] = ByteStringObject(bytes([code]))
            operands[offset] = ByteStringObject(emission.text.encode("utf-16-be"))
            expected_maps[identity][code] = emission.text
            mapping.append({"code": code, "glyph_index": emission.glyph_index,
                            "text": emission.text, "uses": emission.uses,
                            "advance": emission.advance,
                            "source_indices": emission.source_indices,
                            "semantic_base": emission.base_key[:2]})
        if len(sources) != 1 or len(logical_bounds) != 1 or len(metric_fonts) != 1:
            raise TextMappingError("A PDF font subset mixes source fonts, line metrics or semantic bases")
        source_fonts = sources.pop()
        metric_font = metric_fonts.pop()
        logical_y1, logical_y2 = logical_bounds.pop()
        y1 = min(min(box[1] for box in bounds), logical_y1)
        y2 = max(max(box[3] for box in bounds), logical_y2)
        old_box = [float(value) for value in font["/FontBBox"]]
        x1 = min(old_box[0], min(box[0] for box in bounds))
        x2 = max(old_box[2], max(box[2] for box in bounds))
        normalized_box = [x1, y1, x2, y2]
        new_box = [TYPE3_UNITS_PER_EM * x1, -TYPE3_UNITS_PER_EM * y2,
                   TYPE3_UNITS_PER_EM * x2, -TYPE3_UNITS_PER_EM * y1]
        font[NameObject("/FontBBox")] = ArrayObject([FloatObject(value) for value in new_box])
        font[NameObject("/FontMatrix")] = ArrayObject(
            [FloatObject(value) for value in (1 / TYPE3_UNITS_PER_EM, 0, 0,
                                             1 / TYPE3_UNITS_PER_EM, 0, 0)])
        font[NameObject("/Widths")] = ArrayObject(
            [FloatObject(float(value) * TYPE3_UNITS_PER_EM) for value in font["/Widths"]])
        expected_changes.add(identity)
        if set(glyph_bounds) != set(font["/CharProcs"]):
            raise TextMappingError("A Type3 program has no source-owned glyph bounds")
        for glyph_name in font["/CharProcs"]:
            reference = font["/CharProcs"].raw_get(glyph_name)
            if not isinstance(reference, IndirectObject):
                raise TextMappingError("A Type3 glyph program lost its indirect identity")
            program_identity = (reference.idnum, reference.generation)
            if program_identity not in programs:
                program = reference.get_object()
                converted, details = _type3_program_in_pdf_units(
                    program.get_data(), glyph_bounds[glyph_name])
                program.set_data(converted)
                programs[program_identity] = (converted, details)
                expected_changes.add(program_identity)
            elif programs[program_identity][1]["source_bounds"] != list(glyph_bounds[glyph_name]):
                raise TextMappingError("A shared Type3 program has inconsistent source bounds")
        font_name = "/Manim-" + metric_font[5] + "-" + hashlib.sha256(
            repr((source_fonts, metric_font, logical_y1, logical_y2)).encode()).hexdigest()[:12]
        descriptor = _type3_descriptor(font_name, metric_font[4], (y1, y2))
        descriptor_reference = writer._add_object(descriptor)
        descriptor_identity = (descriptor_reference.idnum, descriptor_reference.generation)
        font[NameObject("/FontDescriptor")] = descriptor_reference
        expected_changes.add(descriptor_identity)
        expected_descriptors[descriptor_identity] = _pdf_object_hash(descriptor)
        # The setter invalidates the original bytes after changing parsed operands.
        cmap.operations = cmap.operations
        font["/ToUnicode"].set_data(cmap.get_data())
        expected_changes.add(cmap_identity)
        changed_cmaps.add(cmap_identity)
        expected_fonts[identity] = _pdf_object_hash(font)
        records.append({"font_object": identity, "source_fonts": source_fonts,
                        "source_lines": sorted(lines),
                        "semantic_bases": sorted(bases), "descriptor_font": metric_font,
                        "descriptor_object": descriptor_identity,
                        "descriptor_italic_angle": metric_font[4],
                        "descriptor_vertical_bounds": [y1, y2],
                        "logical_vertical_bounds": [logical_y1, logical_y2],
                        "before_bounds": old_box, "normalized_bounds": normalized_box,
                        "published_bounds": new_box,
                        "source_vertical_bounds": [y1, y2],
                        "original_horizontal_bounds": [old_box[0], old_box[2]],
                        "conservative_horizontal_bounds": [x1, x2],
                        "glyphs": mapping})
    if seen != set(emissions) or uses:
        missing = [(emissions[marker].glyph_index, emissions[marker].text)
                   for marker in set(emissions) - seen]
        raise TextMappingError("Incomplete source-to-PDF glyph identity accounting: "
                               f"missing={missing!r}, unmatched_uses={dict(uses)!r}")
    changes = {(item.idnum, item.generation) for item in writer.list_objects_in_increment()}
    if changes != expected_changes:
        raise TextMappingError("PDF metric normalization changed an unexpected object")
    output = BytesIO()
    writer.write(output)
    result = output.getvalue()
    if not result.startswith(pdf_bytes):
        raise TextMappingError("PDF metric normalization rewrote the original drawing data")
    verified = PdfReader(BytesIO(result), strict=True)
    after_streams = _stream_hashes(verified.trailer.raw_get("/Root"))
    if set(after_streams) != set(before_streams) or any(
            after_streams[key] != digest for key, digest in before_streams.items()
            if key not in changed_cmaps and key not in programs):
        raise TextMappingError("PDF metric normalization changed a drawing stream")
    for identity, (expected, _) in programs.items():
        if verified.get_object(IndirectObject(*identity, verified)).get_data() != expected:
            raise TextMappingError("PDF unit conversion changed an original glyph drawing body")
    for identity, digest in immutable_fonts.items():
        font = verified.get_object(IndirectObject(*identity, verified))
        if _font_dictionary_hash(font) != digest:
            raise TextMappingError("PDF metric normalization changed an unrelated font field")
        if _pdf_object_hash(font) != expected_fonts[identity]:
            raise TextMappingError("Published PDF glyph units differ from the exact conversion")
        _, entries = _unicode_entries(font["/ToUnicode"], verified)
        if {code: text for code, text, *_ in entries} != expected_maps[identity]:
            raise TextMappingError("Published PDF Unicode differs from its source identities")
    for identity, digest in expected_descriptors.items():
        if _pdf_object_hash(verified.get_object(IndirectObject(*identity, verified))) != digest:
            raise TextMappingError("Published PDF descriptor differs from its source font")
    if audit is not None:
        audit.update({"glyph_identities": len(emissions), "glyph_uses": sum(e.uses for e in emissions.values()),
                      "fonts": records, "changed_objects": sorted(changes),
                      "original_bytes_preserved": True,
                      "font_matrix": [.001, 0, 0, .001, 0, 0],
                      "glyph_body_transform": [1000, 0, 0, -1000, 0, 0],
                      "glyph_programs": {str(key): details for key, (_, details) in programs.items()},
                      "before_streams": {str(key): value for key, value in before_streams.items()},
                      "after_streams": {str(key): value for key, value in after_streams.items()}})
    return result


class _PdfGlyphs:
    def __init__(self, paths, crop, append_path, draw_paths):
        from .vector_scene import COORDINATE_DECIMALS

        self.paths, self.crop = paths, crop
        self.append_path, self.draw_paths = append_path, draw_paths
        # Font placement is four decimal digits finer than the shared visible
        # path coordinates.  Quantize only this PDF representation boundary;
        # current outlines and raw semantic affine validation remain unchanged.
        self.font_coordinate_decimals = COORDINATE_DECIMALS + 4
        self.entries = {}
        self.instances = {}
        self.groups = {}
        self.masks = {}
        self.emissions = {}
        self.faces = {}
        self.callbacks = []
        self.callback_errors = []
        self.next_glyph = 1
        self.matrices = {}
        self._prepare()
        # Keep every callback alive until the PDF surface has been finished.
        self.render_callback = cairo.ffi.callback(
            "cairo_user_scaled_font_render_glyph_func_t", self._render_glyph)

    def _ink_bounds(self, path):
        boxes = []
        if _has_fill(path):
            boxes.append(_control_bounds(path["coords"]))
        if _has_stroke(path):
            radius = path["stroke_width"] / 2
            boxes.append(_control_bounds(path["coords"], (radius, radius)))
        boxes = [box for box in boxes if box[2] > box[0] and box[3] > box[1]]
        return _union(boxes) if boxes else None

    @staticmethod
    def _identity(glyph, index=None):
        return (glyph.instance, glyph.source.layout_key,
                glyph.source.index if index is None else index)

    def _prepare(self):
        for position, path in enumerate(self.paths):
            glyph = path.get("glyph")
            if glyph is None or not (_has_fill(path) or _has_stroke(path)):
                continue
            if not _has_fill(path):
                # Cairo may flatten the user-font path transform without
                # scaling a stroke-only CharProc's width.  Fail before writing
                # a misleading glyph; filled glyphs can keep a separate stroke.
                raise TextMappingError("A stroke-only semantic glyph requires an explicit filled outline")
            bounds = self._ink_bounds(path)
            if bounds is None:
                continue
            matrix = _matrix(glyph, self.font_coordinate_decimals)
            # Equivalent leaf fits now have one exact canonical key.  Reusing
            # it keeps a coherent subset without depending on first-seen noise.
            # Its inverse maps every unchanged current canvas outline back to it.
            matrix = self.matrices.setdefault(tuple(matrix.flat), matrix)
            baseline = _canonical_point(glyph.baseline, self.font_coordinate_decimals)
            entry = _Glyph(position, path, glyph, bounds, baseline, matrix, _inverse(matrix))
            key = self._identity(glyph)
            if key in self.instances:
                raise TextMappingError("A Tex instance contains the same source glyph twice")
            self.entries[position] = entry
            self.instances[key] = entry

        handled = set()
        for position, entry in self.entries.items():
            if position in handled:
                continue
            glyph = entry.snapshot
            cluster = glyph.source.cluster
            if not cluster or glyph.source.index not in cluster or len(set(cluster)) != len(cluster):
                raise TextMappingError("A PDF glyph has an invalid source cluster")
            if glyph.source.cluster_base not in cluster:
                raise TextMappingError("A composite TeX glyph is missing its semantic base")
            members = [self.instances.get(self._identity(glyph, index)) for index in cluster]
            if any(member is None for member in members):
                raise TextMappingError("A composite TeX symbol requires all its visible source glyphs")
            positions = [member.position for member in members]
            if positions != list(range(min(positions), max(positions) + 1)):
                raise TextMappingError("A composite TeX symbol cannot reorder intervening paint")
            for member in members:
                other = member.snapshot
                if (other.source.cluster != cluster or
                        other.source.cluster_text != glyph.source.cluster_text or
                        other.source.cluster_base != glyph.source.cluster_base or
                        not np.allclose(other.affine, glyph.affine, rtol=0, atol=1e-8) or
                        not np.allclose(member.matrix, entry.matrix, rtol=0, atol=1e-8) or
                        _style(member.path) != _style(entry.path)):
                    raise TextMappingError("A composite TeX symbol has inconsistent transforms or style")
            # Separate fill/stroke operations preserve their original alpha and colors.
            # A compound cluster may not move a stroke past another component's fill.
            if len(members) > 1 and _has_fill(entry.path) and _has_stroke(entry.path):
                raise TextMappingError("A composite TeX symbol needs an explicit fill/stroke paint grouping")
            if len(members) > 1 and (entry.path["fill"] if _has_fill(entry.path)
                                    else entry.path["stroke"])[3] < 1:
                raise TextMappingError("A translucent composite needs component-preserving opacity")
            self.groups[positions[0]] = members
            handled.update(positions)

    def _face(self, sources, base_key):
        logical_y1, logical_y2 = _logical_vertical_bounds(_line_key(sources))
        key = (_font_key(source.font for source in sources), base_key[2], logical_y1, logical_y2)
        if key in self.faces:
            return self.faces[key]
        ascent, descent = -logical_y1, logical_y2

        @cairo.ffi.callback("cairo_user_scaled_font_init_func_t")
        def initialize(scaled_font, context, extents):
            extents.ascent = ascent
            extents.descent = descent
            extents.height = ascent + descent
            extents.max_x_advance = 1.0
            extents.max_y_advance = 0.0
            return cairo.STATUS_SUCCESS

        raw = cairo.cairo.cairo_user_font_face_create()
        cairo.cairo.cairo_user_font_face_set_init_func(raw, initialize)
        cairo.cairo.cairo_user_font_face_set_render_glyph_func(raw, self.render_callback)
        face = cairo.FontFace._from_pointer(raw, incref=False)
        self.callbacks.append(initialize)
        self.faces[key] = face
        return face

    def _register(self, mask):
        if self.next_glyph > 65535:
            raise TextMappingError("A PDF snapshot exceeds the supported glyph-index range")
        index = self.next_glyph
        self.next_glyph += 1
        self.masks[index] = mask
        return index

    def _glyph_mask(self, members):
        first, last = members[0], members[-1]
        baseline = first.baseline
        paths, boxes = [], []
        for entry in members:
            points = np.array([(float(x) - baseline[0], float(y) - baseline[1])
                               for x, y in np.asarray(entry.path["coords"]).reshape((-1, 2))])
            path = dict(entry.path, coords=points.reshape(-1).tolist())
            paths.append(path)
            normalized = np.array([_apply_basis(first.inverse, point) for point in points])
            # The curve is inside its control hull.  A round stroke stays
            # uniform in canvas space; its transformed disk has these radii.
            if not _has_fill(entry.path):
                radius = entry.path["stroke_width"] / 2
                radii = tuple(radius * math.hypot(*map(float, row)) for row in first.inverse)
                boxes.append(_control_bounds(normalized, radii))
            else:
                boxes.append(_control_bounds(normalized))
        # A source composite is one character with several original outlines.
        # Its advance ends at the final source component's actual advance.
        delta = _apply_basis(last.matrix, (last.snapshot.source.advance, 0))
        endpoint = (last.baseline[0] + delta[0], last.baseline[1] + delta[1])
        advance = _apply_basis(first.inverse,
                              (endpoint[0] - baseline[0], endpoint[1] - baseline[1]))
        if abs(float(advance[1])) > 1e-8:
            raise TextMappingError("A composite TeX symbol has inconsistent source baselines")
        return _Mask(tuple(paths), first.inverse, _outward_font_bounds(_union(boxes)), float(advance[0]),
                     stroke=not _has_fill(first.path))

    def _render_glyph(self, scaled_font, index, raw_context, extents):
        try:
            mask = self.masks[index]
            context = cairo.Context._from_pointer(raw_context, incref=True)
            if mask.paths:
                context.save()
                # Interpret the exact shared canvas coordinates, then map them
                # into this glyph's font basis.  This also retains stroke widths.
                context.transform(_cairo_matrix(mask.inverse))
                for path in mask.paths:
                    context.new_path()
                    self.append_path(context, path)
                    if mask.stroke:
                        context.set_line_width(path["stroke_width"])
                        context.set_line_join(cairo.LINE_JOIN_ROUND)
                        context.set_line_cap(cairo.LINE_CAP_ROUND)
                        context.stroke()
                    else:
                        context.fill()
                context.restore()
            x1, y1, x2, y2 = mask.bounds
            extents.x_bearing, extents.y_bearing = x1, y1
            extents.width, extents.height = x2 - x1, y2 - y1
            extents.x_advance, extents.y_advance = mask.advance, 0.0
            return cairo.STATUS_SUCCESS
        except Exception as error:
            self.callback_errors.append(error)
            return cairo.STATUS_USER_FONT_ERROR

    def _emit_run(self, context, glyphs, font_key, line_key, source_indices, base_key):
        markers, positions, clusters = [], [], []
        for index, baseline, text in glyphs:
            marker = _temporary_character(index)
            mask = self.masks[index]
            record = _Emission(index, text, font_key, line_key, source_indices, base_key,
                               mask.bounds, mask.advance)
            if marker in self.emissions:
                previous = self.emissions[marker]
                if (previous.glyph_index, previous.text, previous.font_key, previous.line_key,
                        previous.source_indices, previous.base_key, previous.bounds, previous.advance) != (
                        index, text, font_key, line_key, source_indices, base_key, mask.bounds, mask.advance):
                    raise TextMappingError("An emitted PDF glyph changed source ownership")
                previous.uses += 1
            else:
                self.emissions[marker] = record
            markers.append(marker)
            positions.append((index, *baseline))
            clusters.append((len(marker.encode("utf-8")), 1))
        # Keep real zero-ink spaces in the same run as the following visible
        # glyph, so Cairo retains them. Each still has its own glyph, advance,
        # Unicode cluster and source identity; no painted spacer is introduced.
        context.show_text_glyphs("".join(markers), positions, clusters)

    def _space(self, entry, space, baseline):
        left = self.instances.get(self._identity(entry.snapshot, space.left_index))
        if left is None or not _intersects(left.bounds, self.crop):
            return
        if not np.allclose(left.snapshot.affine, entry.snapshot.affine, rtol=0, atol=1e-8):
            raise TextMappingError("A source word space has inconsistent current transforms")
        line = entry.snapshot.source.line
        baseline = _canonical_point(baseline, self.font_coordinate_decimals)
        offsets = [_apply_basis(entry.matrix, point) for point in
                   ((0, -line.ascent), (0, line.descent),
                    (space.advance, -line.ascent), (space.advance, line.descent))]
        corners = np.array([(baseline[0] + x, baseline[1] + y) for x, y in offsets])
        low, high = corners.min(axis=0), corners.max(axis=0)
        if not _intersects((*low, *high), self.crop):
            return
        index = self._register(_Mask((), None, (0, 0, 0, 0), space.advance))
        return index, baseline, " "

    def draw(self, context):
        options = cairo.FontOptions()
        options.set_hint_style(cairo.HINT_STYLE_NONE)
        options.set_hint_metrics(cairo.HINT_METRICS_OFF)
        context.set_font_options(options)
        for position, path in enumerate(self.paths):
            if path.get("glyph") is None:
                self.draw_paths(context, [path])
                continue
            members = self.groups.get(position)
            if members is None or not _intersects(_union([m.bounds for m in members]), self.crop):
                continue
            first = members[0]
            sources = [member.snapshot.source for member in members]
            source_indices, base_key = _source_ownership(sources)
            face = self._face(sources, base_key)
            font_key = _font_key(source.font for source in sources)
            line_key = _line_key(sources)
            context.set_font_face(face)
            context.set_font_matrix(_cairo_matrix(first.matrix))
            context.set_source_rgba(*(path["fill"] if _has_fill(path) else path["stroke"]))
            glyph_run = []
            for space, baseline in first.snapshot.spaces_before:
                spacing = self._space(first, space, baseline)
                if spacing is not None:
                    glyph_run.append(spacing)
            index = self._register(self._glyph_mask(members))
            text = first.snapshot.source.cluster_text
            glyph_run.append((index, first.baseline, text))
            self._emit_run(context, glyph_run, font_key, line_key, source_indices, base_key)
            if _has_fill(path) and _has_stroke(path):
                self.draw_paths(context, [dict(path, fill=[0, 0, 0, 0])])
        if self.callback_errors:
            raise TextMappingError("Cairo could not render a semantic glyph") from self.callback_errors[0]


def write_pdf_snapshot(destination, paths, background, crop=None):
    """Publish one vector PDF after its complete semantic/geometry export succeeds."""
    from .vector_scene import WIDTH, HEIGHT, append_path, draw_paths

    crop = tuple(crop or (0, 0, WIDTH, HEIGHT))
    if len(crop) != 4 or not np.isfinite(crop).all() or crop[2] <= 0 or crop[3] <= 0:
        raise ValueError("A PDF crop needs finite coordinates and positive dimensions")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    renderer = _PdfGlyphs(list(paths), crop, append_path, draw_paths)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".glyph-", suffix=".pdf", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    surface = None
    finished = False
    try:
        surface = cairo.PDFSurface(str(temporary_path), crop[2], crop[3])
        context = cairo.Context(surface)
        context.set_source_rgba(*background)
        context.paint()
        context.rectangle(0, 0, crop[2], crop[3])
        context.clip()
        context.translate(-crop[0], -crop[1])
        renderer.draw(context)
        surface.finish()
        finished = True
        normalized = _normalize_font_metadata(temporary_path.read_bytes(), renderer.emissions)
        temporary_path.write_bytes(normalized)
        temporary_path.replace(destination)
    except Exception:
        if renderer.callback_errors:
            raise TextMappingError("Cairo could not render a semantic glyph") from renderer.callback_errors[0]
        raise
    finally:
        if surface is not None and not finished:
            try:
                surface.finish()
            except Exception:
                pass
        temporary_path.unlink(missing_ok=True)
