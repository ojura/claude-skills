"""Keep TeX's character layout attached to the Manim glyphs it produced.

The cached TeX source is compiled with an observational shipout trace.  Dvisvgm's
character output supplies Unicode, baselines and font metrics; the trace supplies
logical line boxes, word-space glue, ligature inputs and joining kerns. Its outline output must match
the original Manim SVG before any mapping is accepted.  No second text layout is
performed, and none of this metadata enters a native animation keyframe.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata
from xml.etree import ElementTree as ET

import numpy as np
import svgelements
from manim import MathTex, Tex, Transform, config
from manim.mobject.svg.svg_mobject import VMobjectFromSVGPath
from manim.mobject.text.tex_mobject import SingleStringMathTex

from .tex_font_source import (FontSourceError, probe_font_program,
                             probe_glyph_unicode_map, select_font_map)


class TextMappingError(ValueError):
    """The rendered glyphs cannot safely carry the declared source semantics."""


SVG = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}href"
TEX_PT_TO_SVG = 72 / 72.27
LAYOUT_VERSION = 6
# Expand typographic ligatures only, retaining the original Unicode alongside it.
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
             "ﬅ": "st", "ﬆ": "st"}
# Source macros whose logical character is drawn using several physical glyphs.
# Zero-extent shipout markers identify EACH expansion; a neighboring equals and
# arrow with an authored negative kern must not be reinterpreted as this macro.
# The final index identifies the semantic arrow's font, while every component
# retains its own source font (the extension can come from a different family).
COMPOSITES = {
    "Longrightarrow": (("=", "⇒"), ("equal", "arrowdblright"), "⟹", 1),
    "Longleftarrow": (("⇐", "="), ("arrowdblleft", "equal"), "⟸", 0),
    "longrightarrow": (("−", "→"), ("minus", "arrowright"), "⟶", 1),
    "longleftarrow": (("←", "−"), ("arrowleft", "minus"), "⟵", 0),
    "mapsto": (("↦", "→"), ("mapsto", "arrowright"), "↦", 1),
    "longmapsto": (("↦", "−", "→"), ("mapsto", "minus", "arrowright"), "⟼", 2),
}


def _composite_markers():
    definitions = []
    for name in COMPOSITES:
        definitions.append(
            (r"\let\PSRoriginal%s\%s\def\%s{\special{psrtext:begin %s}"
             r"\PSRoriginal%s\special{psrtext:end %s}}") % ((name,) * 6))
    return r"\AtBeginDocument{" + "".join(definitions) + "}"


@dataclass(frozen=True)
class FontSource:
    """Original font metrics/program identity, separate from the actual line box."""

    family: str
    em_size: float
    ascent: float
    descent: float
    italic_angle: float
    postscript_name: str
    program_sha256: str


@dataclass(frozen=True)
class LineSource:
    """The glyph's enclosing TeX hbox, with height/depth in this glyph's em.

    The box index belongs to the observed layout. Nested scripts have their own
    horizontal box and baseline; colored fragments in one line keep its box.
    """

    box_index: int
    ascent: float
    descent: float


@dataclass(frozen=True)
class SpaceSource:
    left_index: int
    baseline: tuple[float, float]
    advance: float


@dataclass(frozen=True)
class GlyphSource:
    layout_key: str
    tex: str
    arguments: tuple[str, ...]
    argument_index: int
    index: int
    unicode: str
    copy_text: str
    glyph_name: str
    font: FontSource
    line: LineSource
    baseline: tuple[float, float]
    advance: float
    spaces_before: tuple[SpaceSource, ...]
    cluster: tuple[int, ...]
    cluster_text: str
    cluster_base: int


class RenderInstance:
    """A copyable identity token shared by one actual Tex tree's glyph leaves.

    Deepcopy creates a fresh token and uses its memo to share it within that copy.
    It contains no child references, so extracting/copying one leaf is safe too.
    """


@dataclass(frozen=True)
class GlyphBinding:
    source: GlyphSource
    instance: RenderInstance
    reference_points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class GlyphSnapshot:
    source: GlyphSource
    instance: RenderInstance
    baseline: tuple[float, float]
    em_x: tuple[float, float]
    em_y: tuple[float, float]
    # Source SVG coordinates -> current Manim xy.  Kept for cluster validation.
    affine: tuple[tuple[float, float], ...]
    spaces_before: tuple[tuple[SpaceSource, tuple[float, float]], ...]


def _page_atoms(root):
    """The same ordered drawing atoms that Manim's SVG importer encounters."""
    result = []
    def visit(element):
        name = element.tag.removeprefix(SVG)
        if name in {"defs", "style", "metadata", "title", "desc"}:
            return
        if name in {"svg", "g"}:
            if "transform" in element.attrib:
                raise TextMappingError("Transformed SVG containers need an explicit semantic mapping")
            for child in element:
                visit(child)
        elif name in {"use", "text", "rect"}:
            result.append(element)
        else:
            raise TextMappingError(f"Unsupported TeX drawing atom: {name}")
    visit(root)
    return result


def _outline_signature(root):
    # Definition order is immaterial; dvisvgm may vary it between processes.
    definitions = sorted((item.attrib["id"], tuple(sorted(item.attrib.items())))
                         for item in root.findall(f".//{SVG}defs/{SVG}path"))
    return (tuple(sorted(root.attrib.items())), definitions,
            [(item.tag, tuple(sorted(item.attrib.items()))) for item in _page_atoms(root)])


def _trace_glyphs(log):
    if log.count("Completed box being shipped out") != 1:
        raise TextMappingError("A TeX object must ship exactly one page for semantic mapping")
    shipped = log.split("Completed box being shipped out", 1)[1]
    shipped = shipped.split("Here is how much", 1)[0]
    result = []
    composites = []
    open_composites = []
    pending_spaces = []
    pending_kern = 0.0
    boxes = []
    box_count = 0
    number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
    # TeX can print a font's control-byte slots literally (ff/fi/fl are 11–13
    # in OT1).  splitlines() would mistake those glyphs for line boundaries.
    for line in shipped.split("\n"):
        if re.match(r"^\.+\\special\{pdf:map(?:file|line)\b", line):
            raise TextMappingError("Per-expression font-map changes need explicit semantic support")
        # Dot depth describes the actual shipout tree, including vboxes. Pop on
        # every node, so a completed script/row cannot own a later sibling glyph.
        node = re.match(r"^(\.*)(\\.*)$", line)
        if node:
            tree_depth = len(node[1])
            while boxes and boxes[-1]["tree_depth"] >= tree_depth:
                boxes.pop()
            box = re.match(r"^\\([hv])box\((" + number + r")\+(" + number + r")\)", node[2])
            if box:
                height, depth = float(box[2]), float(box[3])
                if not np.isfinite((height, depth)).all() or min(height, depth) < 0:
                    raise TextMappingError("A TeX source box has unsupported height/depth")
                boxes.append({"box_index": box_count, "kind": box[1],
                              "tree_depth": tree_depth, "height_tex_pt": height,
                              "depth_tex_pt": depth})
                box_count += 1
                continue
        marker = re.match(r"^\.+\\special\{psrtext:(begin|end) ([A-Za-z]+)\}$", line)
        if marker:
            if marker[1] == "begin":
                open_composites.append((marker[2], len(result)))
            else:
                if not open_composites or open_composites[-1][0] != marker[2]:
                    raise TextMappingError("Unbalanced TeX composite source markers")
                name, start = open_composites.pop()
                composites.append((name, list(range(start, len(result)))))
            continue
        glyph = re.match(r"^(\.+)\\([^\s]+/[^\s]+) (.*)$", line)
        if glyph:
            depth = len(glyph[1])
            if not boxes or boxes[-1]["kind"] != "h" or boxes[-1]["tree_depth"] != depth - 1:
                raise TextMappingError("A TeX glyph has no direct horizontal source box")
            parent = boxes[-1]
            # Ordinary interword glue has stretch/shrink, unlike math kerns and
            # named muskip.  Match the horizontal list's depth, excluding page glue.
            spaces = [width for level, width in pending_spaces if level == depth]
            result.append({"depth": depth, "font": glyph[2], "node": glyph[3],
                           "spaces": spaces, "kern_before": pending_kern,
                           "line": {name: parent[name] for name in
                                    ("box_index", "height_tex_pt", "depth_tex_pt")}})
            pending_spaces = []
            pending_kern = 0.0
            continue
        space = re.match(r"^(\.+)\\glue ([+-]?[0-9.]+) plus ([0-9.]+) minus ([0-9.]+)$", line)
        if space and float(space[2]) > 0:
            pending_spaces.append((len(space[1]), float(space[2]) * TEX_PT_TO_SVG))
        kern = re.match(r"^\.+\\kern\s*([+-]?[0-9.]+)", line)
        if kern:
            pending_kern += float(kern[1]) * TEX_PT_TO_SVG
    if open_composites:
        raise TextMappingError("An authored TeX composite has no closing source marker")
    return result, composites


def _run(command, directory):
    result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
    if result.returncode:
        raise TextMappingError(f"TeX semantic mapping command failed ({command[0]}): "
                               f"{result.stdout[-1500:]}{result.stderr[-1500:]}\n"
                               f"See {directory}")


@lru_cache(maxsize=256)
def _layout(svg_name, source_bytes, svg_bytes, compiler, output_format):
    if output_format != ".dvi":
        raise TextMappingError("Selectable TeX currently requires the authored DVI layout")
    key = hashlib.sha256(str(LAYOUT_VERSION).encode() + source_bytes + svg_bytes).hexdigest()
    directory = config.get_dir("media_dir") / "tex-semantics" / key
    directory.mkdir(parents=True, exist_ok=True)
    # The layout cache is shared by light/dark builds and independent test jobs.
    # Only one compiler may own an expression's aux/log/DVI files at a time.
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            font_map = select_font_map(str(Path.cwd()))
            return _build_layout(svg_name, source_bytes, svg_bytes, compiler, key, directory, font_map)
        except FontSourceError as error:
            raise TextMappingError(str(error)) from error


def _build_layout(svg_name, source_bytes, svg_bytes, compiler, base_key, directory, font_map):
    cached = directory / "layout.json"
    unicode_source = None
    if cached.exists():
        data = json.loads(cached.read_text())
        if (data.get("version") == LAYOUT_VERSION and data.get("base_key") == base_key
                and data.get("font_map") == font_map):
            observed = {family: probe_font_program(family, font_map, str(config.get_dir("media_dir")))
                        for family in data["font_programs"]}
            expected_unicode_source = data.get("glyph_unicode_source")
            current_unicode_source = (probe_glyph_unicode_map(str(Path.cwd()))[0]
                                      if expected_unicode_source is not None else None)
            if (observed == data["font_programs"] and
                    current_unicode_source == expected_unicode_source):
                return data["key"], data["glyphs"], data["atoms"]
    stem = Path(svg_name).stem
    (directory / f"{stem}.tex").write_bytes(source_bytes)
    # The shipout trace exposes actual glyph/glue nodes. Composite expansion
    # markers have zero extent and do not change source grouping; exact outline
    # correspondence below is mandatory before accepting their mapping.
    _run([compiler, "-interaction=batchmode", "-halt-on-error", "-output-format=dvi",
          f"-jobname={stem}", _composite_markers()
          + r"\tracingoutput=1\showboxbreadth=100000\showboxdepth=1000"
          + rf"\input{{{stem}.tex}}"], directory)
    _run(["dvisvgm", f"--fontmap={font_map}", "--no-merge", "--no-styles", "--verbosity=0",
          "--output=semantic.svg", f"{stem}.dvi"], directory)
    _run(["dvisvgm", f"--fontmap={font_map}", "--no-fonts", "--verbosity=0", "--output=outline.svg",
          f"{stem}.dvi"], directory)
    original = ET.fromstring(svg_bytes)
    rebuilt = ET.parse(directory / "outline.svg").getroot()
    if _outline_signature(original) != _outline_signature(rebuilt):
        raise TextMappingError(f"TeX semantic layout changed the original outline geometry: {svg_name}")
    semantic = ET.parse(directory / "semantic.svg").getroot()
    atoms = _page_atoms(original)
    semantic_atoms = _page_atoms(semantic)
    if len(atoms) != len(semantic_atoms):
        raise TextMappingError(f"TeX atom counts differ: {svg_name}")
    trace, composites = _trace_glyphs((directory / f"{stem}.log").read_bytes().decode("utf8", errors="replace"))
    fonts = {font.attrib["id"]: font for font in semantic.findall(f".//{SVG}font")}
    font_programs = {family: probe_font_program(family, font_map, str(config.get_dir("media_dir")))
                     for family in fonts}
    key = hashlib.sha256(base_key.encode() + json.dumps(
        font_programs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    glyphs = []
    roles = []
    for original_atom, text_atom in zip(atoms, semantic_atoms):
        if original_atom.tag == SVG + "rect" and text_atom.tag == SVG + "rect":
            if original_atom.attrib != text_atom.attrib:
                raise TextMappingError("A TeX rule changed between outline and semantic output")
            roles.append(None)  # A fraction/radical rule has geometry, not a character.
            continue
        if original_atom.tag != SVG + "use" or text_atom.tag != SVG + "text":
            raise TextMappingError("A TeX drawing atom has no corresponding character or rule")
        if any(original_atom.attrib[name] != text_atom.attrib[name] for name in ("x", "y")):
            raise TextMappingError("A TeX glyph baseline differs from its original outline")
        value = "".join(text_atom.itertext())
        font = fonts[text_atom.attrib["font-family"]]
        face = font.find(SVG + "font-face")
        matches = [item for item in font.findall(SVG + "glyph") if item.attrib.get("unicode") == value]
        if len(matches) != 1:
            raise TextMappingError(f"Ambiguous TeX Unicode glyph mapping: {value!r}")
        glyph = matches[0]
        glyph_name = glyph.attrib.get("glyph-name", "")
        if len(value) == 1 and unicodedata.category(value) == "Co":
            unicode_source, mappings = probe_glyph_unicode_map(str(Path.cwd()))
            # TeX's whole-glyph size variants retain the base glyph's character.
            # Extension pieces such as bracketlefttp remain unsupported unless
            # their table entry identifies an ordinary Unicode character.
            sized = re.fullmatch(r"(.+?)(?:big|Big|bigg|Bigg)", glyph_name)
            encoded = mappings.get(glyph_name, mappings.get(sized[1], "") if sized else "")
            try:
                value = "".join(chr(int(code, 16)) for code in encoded.split())
            except ValueError as error:
                raise TextMappingError(f"Unsupported Unicode mapping for TeX glyph {glyph_name!r}") from error
        if not value or any(unicodedata.category(char) in {"Cc", "Co", "Cs", "Cn"} for char in value):
            raise TextMappingError(f"TeX glyph {glyph_name!r} has no supported Unicode mapping: {value!r}")
        units = float(face.attrib["units-per-em"])
        index = len(glyphs)
        roles.append(index)
        glyphs.append({"unicode": value, "copy_text": LIGATURES.get(value, value),
                       "glyph_name": glyph_name,
                       "font": {"family": text_atom.attrib["font-family"],
                                "em_size": float(text_atom.attrib["font-size"]),
                                "ascent": float(face.attrib["ascent"]) / units,
                                "descent": float(face.attrib["descent"]) / units,
                                **font_programs[text_atom.attrib["font-family"]]},
                       "baseline": [float(text_atom.attrib[name]) for name in ("x", "y")],
                       "advance": float(glyph.attrib["horiz-adv-x"]) / units,
                       "spaces": [], "cluster": [index], "cluster_text": LIGATURES.get(value, value),
                       "cluster_base": index})
    if len(glyphs) != len(trace):
        raise TextMappingError(f"TeX node/glyph counts differ: {len(trace)} vs {len(glyphs)}")
    line_baselines = {}
    for index, (glyph, node) in enumerate(zip(glyphs, trace)):
        line = node["line"]
        em_size = glyph["font"]["em_size"]
        if not np.isfinite(em_size) or em_size <= 0:
            raise TextMappingError("A TeX glyph has no finite positive source em")
        glyph["line"] = {"box_index": line["box_index"],
                         "ascent": line["height_tex_pt"] * TEX_PT_TO_SVG / em_size,
                         "descent": line["depth_tex_pt"] * TEX_PT_TO_SVG / em_size}
        # The TeX dimensions are relative to the immediate hbox baseline. Verify
        # that correspondence against the independently observed SVG placement.
        baseline = line_baselines.setdefault(line["box_index"], glyph["baseline"][1])
        if abs(baseline - glyph["baseline"][1]) > 1e-6:
            raise TextMappingError("A TeX horizontal source box has inconsistent glyph baselines")
        if glyph["unicode"] in LIGATURES:
            match = re.search(r"\(ligature ([^)]*)\)$", node["node"])
            if match is None or match[1] != glyph["copy_text"]:
                raise TextMappingError(f"Typographic ligature source mismatch: {node['node']}")
        if index and node["spaces"]:
            cursor = glyph["baseline"][0] - sum(node["spaces"])
            for width in node["spaces"]:
                glyph["spaces"].append({"left_index": index - 1,
                                         "baseline": [cursor, glyph["baseline"][1]],
                                         "advance": width / glyph["font"]["em_size"]})
                cursor += width
    completed_composites = {}
    for name, indices in composites:
        expected_unicode, expected_names, copy_text, base_index = COMPOSITES[name]
        components = [glyphs[index] for index in indices]
        if (tuple(item["unicode"] for item in components) != expected_unicode or
                tuple(item["glyph_name"] for item in components) != expected_names):
            raise TextMappingError(f"Unsupported physical glyphs for TeX composite {name}")
        for first, second, second_index in zip(components, components[1:], indices[1:]):
            # The mapsto tail is a zero-width glyph at the following shaft's
            # origin. Long-arrow extensions instead join through a negative kern.
            joined = (first["advance"] == 0 and first["baseline"] == second["baseline"]
                      if first["glyph_name"] == "mapsto" else
                      trace[second_index]["kern_before"] < 0)
            if (first["baseline"][1] != second["baseline"][1] or
                    first["font"]["em_size"] != second["font"]["em_size"] or
                    not joined or second["spaces"]):
                raise TextMappingError(f"Inconsistent joining geometry for TeX composite {name}")
        for component, index in zip(components, indices):
            cluster = tuple(component["cluster"])
            if cluster == (index,):
                continue
            # Shipout closes inner markers first. Only longmapsto's complete
            # longrightarrow expansion may be absorbed by its enclosing marker.
            # A partial cluster or a neighbouring operator keeps its ownership.
            if (name != "longmapsto" or cluster != tuple(indices[1:]) or
                    completed_composites.get(cluster) != "longrightarrow"):
                raise TextMappingError(f"Inconsistent source ownership for TeX composite {name}")
        for component in components:
            component["cluster"] = indices
            component["cluster_text"] = copy_text
            component["cluster_base"] = indices[base_index]
        completed_composites[tuple(indices)] = name
    if unicode_source is not None:
        key = hashlib.sha256(key.encode() + json.dumps(
            unicode_source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    data = {"version": LAYOUT_VERSION, "base_key": base_key, "key": key,
            "font_map": font_map, "font_programs": font_programs,
            "glyph_unicode_source": unicode_source,
            "glyphs": glyphs, "atoms": roles}
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as temporary:
        json.dump(data, temporary, ensure_ascii=False, separators=(",", ":"))
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(cached)
    return key, glyphs, roles


def bind_tex(mobject):
    """Validate source correspondence, then attach or retain leaf-owned metadata."""
    svg = Path(mobject.file_name)
    svg_bytes = svg.read_bytes()
    key, data, roles = _layout(str(svg), svg.with_suffix(".tex").read_bytes(), svg_bytes,
                               mobject.tex_template.tex_compiler,
                               mobject.tex_template.output_format)
    leaves = mobject.family_members_with_points()
    if len(leaves) != len(roles):
        raise TextMappingError(f"Manim glyph count differs from its TeX SVG: {mobject.tex_string}")
    bound = [hasattr(leaf, "_tex_glyph") or hasattr(leaf, "_tex_rule_source") for leaf in leaves]
    if any(bound) and not all(bound):
        raise TextMappingError("A TeX tree mixes bound and unbound source glyphs")
    # Reparse the exact original SVG through the importer's XML preparation.
    # A leaf retains its original path_obj through copy/affine transforms. Match
    # that provenance before using traversal order, including on the first bind.
    tree = mobject.modify_xml_tree(ET.ElementTree(ET.fromstring(svg_bytes)))
    parsed = svgelements.SVG.parse(io.BytesIO(ET.tostring(tree.getroot())))
    originals = [shape for shape in parsed.elements()
                 if isinstance(shape, (svgelements.Path, svgelements.Rect))]
    if len(originals) != len(roles):
        raise TextMappingError("The original SVG importer atom count changed")
    # MathTex regroups the original leaves by argument; preserve that observed
    # membership, including colored prose fragments, instead of recompiling them.
    arguments = tuple(mobject.tex_strings)
    membership = {id(leaf): index for index, group in enumerate(mobject.submobjects)
                  for leaf in group.family_members_with_points()}
    instance = RenderInstance()
    prepared = []
    existing_instances = set()
    for leaf, index, original in zip(leaves, roles, originals):
        if index is None:
            rule = (key, mobject.tex_string)
            if all(bound) and getattr(leaf, "_tex_rule_source", None) != rule:
                raise TextMappingError("An existing TeX rule has different source ownership")
            prepared.append((leaf, "_tex_rule_source", rule))
            continue
        if not hasattr(leaf, "path_obj") or not isinstance(original, svgelements.Path):
            raise TextMappingError("A TeX character lost its SVG path provenance")
        original.approximate_arcs_with_quads()
        if (leaf.path_obj.d() != original.d() or
                leaf.path_obj.transform != original.transform or
                leaf.path_obj.values.get("id") != original.values.get("id")):
            raise TextMappingError("TeX glyph order or SVG source provenance changed")
        glyph = data[index]
        source = GlyphSource(
            key, mobject.tex_string, arguments, membership[id(leaf)], index,
            glyph["unicode"], glyph["copy_text"], glyph["glyph_name"],
            FontSource(**glyph["font"]), LineSource(**glyph["line"]),
            tuple(glyph["baseline"]), glyph["advance"],
            tuple(SpaceSource(item["left_index"], tuple(item["baseline"]), item["advance"])
                  for item in glyph["spaces"]),
            tuple(glyph["cluster"]), glyph["cluster_text"], glyph["cluster_base"])
        reference = VMobjectFromSVGPath(original, **mobject.path_string_config).points
        reference_points = tuple(tuple(point[:2]) for point in reference)
        _affine(reference_points, leaf.points)
        if all(bound):
            existing = getattr(leaf, "_tex_glyph", None)
            if existing is None or existing.source != source or existing.reference_points != reference_points:
                raise TextMappingError("An existing TeX glyph has different source ownership")
            existing_instances.add(existing.instance)
        else:
            prepared.append((leaf, "_tex_glyph", GlyphBinding(source, instance, reference_points)))
    if len(existing_instances) > 1:
        raise TextMappingError("A TeX tree mixes glyphs from different render instances")
    # Validation is complete before the first write, so a rejected bind cannot
    # partially relabel the tree. Rebinding retains the existing binding objects.
    if not all(bound):
        for leaf, name, binding in prepared:
            setattr(leaf, name, binding)
    return mobject


def _affine(reference, current):
    source = np.asarray(reference, dtype=float)
    target = np.asarray(current, dtype=float)[:, :2]
    if source.shape != target.shape or len(source) < 3 or not np.isfinite(target).all():
        raise TextMappingError("A TeX glyph changed topology or has invalid geometry")
    source_center, target_center = source.mean(axis=0), target.mean(axis=0)
    linear, _, rank, _ = np.linalg.lstsq(source - source_center, target - target_center, rcond=None)
    if rank != 2 or abs(float(np.linalg.det(linear))) < 1e-14:
        raise TextMappingError("A visible TeX glyph has a singular affine transform")
    offset = target_center - source_center @ linear
    error = float(np.max(np.abs(source @ linear + offset - target)))
    if error > 1e-7 * max(1.0, float(np.max(np.abs(target)))):
        raise TextMappingError(f"A TeX glyph was reshaped instead of affinely transformed (error {error:g})")
    return np.vstack((linear, offset))


def snapshot_glyph(mobject, *, point_transform=None):
    """Capture this leaf; the snapshot owner supplies its common canvas map.

    Without a map the result stays in Manim coordinates, which is also useful
    when validating a newly bound object before it enters a scene.
    """
    binding = getattr(mobject, "_tex_glyph", None)
    if binding is None:
        return None
    if (float(mobject.get_fill_opacity()) <= 0 and
            (float(mobject.get_stroke_opacity()) <= 0 or float(mobject.get_stroke_width()) <= 0)):
        return None
    affine = _affine(binding.reference_points, mobject.points)
    source = binding.source
    def transformed(point):
        value = np.asarray([*point, 1.0]) @ affine
        return tuple(float(coordinate) for coordinate in
                     (point_transform(value) if point_transform else value))
    baseline = transformed(source.baseline)
    # Cairo's font coordinates have y down, as in the original SVG.
    em_x = tuple(np.asarray(transformed((source.baseline[0] + source.font.em_size,
                                        source.baseline[1]))) - baseline)
    em_y = tuple(np.asarray(transformed((source.baseline[0],
                                        source.baseline[1] + source.font.em_size))) - baseline)
    return GlyphSnapshot(source, binding.instance, baseline, em_x, em_y,
                         tuple(tuple(row) for row in affine),
                         tuple((space, transformed(space.baseline)) for space in source.spaces_before))


def _signature(mobject):
    return [None if (binding := getattr(leaf, "_tex_glyph", None)) is None else
            (binding.source.layout_key, binding.source.index, binding.source.copy_text)
            for leaf in mobject.family_members_with_points()]


def validate_text_tree(mobjects):
    """Do not silently turn a future ordinary Tex call back into anonymous paths."""
    seen = set()
    for top in mobjects:
        for member in top.get_family():
            if id(member) in seen:
                continue
            seen.add(id(member))
            if isinstance(member, SingleStringMathTex):
                if any(not hasattr(leaf, "_tex_glyph") and not hasattr(leaf, "_tex_rule_source")
                       for leaf in member.family_members_with_points()):
                    raise TextMappingError(
                        "Unbound TeX in a snapshot; use SemanticTex/SemanticMathTex or bind_tex() "
                        f"for {member.tex_string!r}")


def validate_text_transform(animation):
    """Require an explicit semantic transition for a different-text morph."""
    if isinstance(animation, Transform):
        target = animation.target_mobject
        if target is not None:
            before, after = _signature(animation.mobject), _signature(target)
            if any(item is not None for item in before + after) and before != after:
                raise TextMappingError("Transform changes TeX content/topology; author an explicit semantic transition")
    for child in getattr(animation, "animations", ()):
        validate_text_transform(child)


class _SemanticText:
    def become(self, mobject, *args, **kwargs):
        before, after = _signature(self), _signature(mobject)
        if any(item is not None for item in before + after) and before != after:
            raise TextMappingError("become changes TeX content/topology; author an explicit semantic transition")
        return super().become(mobject, *args, **kwargs)


class SemanticMathTex(_SemanticText, MathTex):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        bind_tex(self)


class SemanticTex(_SemanticText, Tex):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        bind_tex(self)
