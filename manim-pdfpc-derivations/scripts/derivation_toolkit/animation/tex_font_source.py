"""Observe the actual TeX font program through the installed PDF compiler.

The SVG and PDF probes use one explicitly selected renderer map.  pdfTeX's
embedded Type1 program retains the original, unrounded ItalicAngle; the PDF
descriptor itself rounds it.  Probes are refreshed once per process, while the
layout cache compares their content fingerprints before reusing source records.
"""
from __future__ import annotations

from decimal import Decimal
import fcntl
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess

from pypdf import PdfReader


class FontSourceError(ValueError):
    """A renderer font map or program has no supported source metadata."""


NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
CANONICAL_MATRIX = tuple(map(Decimal, (".001", "0", "0", ".001", "0", "0")))


@lru_cache(maxsize=16)
def select_font_map(working_directory):
    """Select a map in dvisvgm's documented order, then use it explicitly.

    Exact comparison with Manim's original outline output remains mandatory;
    a different map cannot silently change the authored geometry.
    """
    for name in ("dvisvgm.map", "ps2pk.map", "pdftex.map", "dvipdfm.map", "psfonts.map"):
        result = subprocess.run(
            ["kpsewhich", "--progname=dvisvgm", "--format=map", name],
            cwd=working_directory, capture_output=True, text=True, timeout=30)
        if result.returncode == 1 and not result.stdout.strip():
            continue
        paths = result.stdout.strip().splitlines()
        if result.returncode or len(paths) != 1:
            raise FontSourceError(f"Cannot resolve the renderer font map {name}: {result.stderr}")
        return str((Path(working_directory) / paths[0]).resolve())
    raise FontSourceError("Selectable TeX requires an installed renderer font map")


@lru_cache(maxsize=16)
def probe_glyph_unicode_map(working_directory):
    """Read pdfTeX's installed glyph-name mappings and record their source."""
    result = subprocess.run(["kpsewhich", "glyphtounicode.tex"], cwd=working_directory,
                            capture_output=True, text=True, timeout=30)
    paths = result.stdout.strip().splitlines()
    if result.returncode or len(paths) != 1:
        raise FontSourceError("Cannot resolve TeX's glyph-to-Unicode table")
    source = (Path(working_directory) / paths[0]).resolve()
    contents = source.read_bytes()
    mappings = dict(re.findall(
        r"^\\pdfglyphtounicode\{([^{}]+)\}\{([0-9A-Fa-f ]+)\}",
        contents.decode("utf8"), re.MULTILINE))
    if not mappings:
        raise FontSourceError(f"The glyph-to-Unicode table is empty: {source}")
    return {"file": str(source), "sha256": hashlib.sha256(contents).hexdigest()}, mappings


def _type1_metadata(font):
    """Read scalar source fields from the compiler-emitted Type1 clear header."""
    if font.get("/Subtype") != "/Type1" or "/FontDescriptor" not in font:
        raise FontSourceError("Selectable TeX requires a mapped Type1 source font")
    descriptor = font["/FontDescriptor"]
    if "/FontFile" not in descriptor:
        raise FontSourceError("The font probe did not embed its original Type1 program")
    stream = descriptor["/FontFile"]
    program = stream.get_data()
    length = int(stream.get("/Length1", 0))
    if length <= 0 or length > len(program):
        raise FontSourceError("The Type1 program has an invalid clear-header length")
    header = program[:length].decode("latin1")
    angles = re.findall(r"^/ItalicAngle\s+(" + NUMBER + r")\s+def\s*$", header, re.MULTILINE)
    matrices = re.findall(r"^/FontMatrix\s*\[([^\]]+)\]\s*(?:readonly\s+)?def\s*$",
                          header, re.MULTILINE)
    names = re.findall(r"^/FontName\s+/([^\s]+)\s+def\s*$", header, re.MULTILINE)
    if len(angles) != 1 or len(matrices) != 1 or len(names) != 1:
        raise FontSourceError("The Type1 source metadata is missing or ambiguous")
    tokens = matrices[0].split()
    if (len(tokens) != 6 or any(re.fullmatch(NUMBER, token) is None for token in tokens)
            or tuple(map(Decimal, tokens)) != CANONICAL_MATRIX):
        raise FontSourceError("Synthetic or noncanonical source FontMatrix needs explicit semantic support")
    angle = float(angles[0])
    if not math.isfinite(angle) or not -90 < angle < 90:
        raise FontSourceError("The source font has an unsupported ItalicAngle")
    if str(font.get("/BaseFont", "")) != "/" + names[0]:
        raise FontSourceError("The PDF font and embedded Type1 program have different identities")
    return {"italic_angle": angle,
            "postscript_name": re.sub(r"^[A-Z]{6}\+", "", names[0]),
            "program_sha256": hashlib.sha256(program).hexdigest()}


@lru_cache(maxsize=256)
def probe_font_program(family, font_map, cache_root):
    """Return source angle/name/fingerprint, refreshed once in each process.

    Every valid 8-bit TFM slot is included, so the program identity is independent
    of the expression that first uses this font.  Synthetic slant/extend maps
    are diagnosed through the compiler's actual FontMatrix instead of guessing
    their effect on the source angle.  The probe is authoring-only.
    """
    if re.fullmatch(r"[A-Za-z0-9._-]+", family) is None:
        raise FontSourceError(f"Unsupported TeX font identifier: {family!r}")
    font_map = str(Path(font_map).resolve())
    key = hashlib.sha256((family + "\n" + font_map).encode()).hexdigest()
    directory = Path(cache_root) / "tex-font-programs" / key
    directory.mkdir(parents=True, exist_ok=True)
    source = (r"\pdfoutput=1\pdfmapfile{}\pdfmapfile{source.map}" + "\n" +
              rf"\font\PSRfont={family} at 10pt" + "\n" +
              r"\newcount\PSRcode\setbox0=\hbox{\PSRfont\PSRcode=0" + "\n" +
              r"\loop\ifnum\PSRcode<256" + "\n" +
              r"\iffontchar\PSRfont\PSRcode\char\PSRcode\fi" + "\n" +
              r"\advance\PSRcode by1\repeat}\shipout\box0\bye" + "\n")
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # pdfTeX's map-file scanner stops at spaces even inside quotes. A fixed
        # local name references the same original file without copying its data.
        alias = directory / "source.map"
        if alias.is_symlink():
            if alias.resolve() != Path(font_map):
                raise FontSourceError("The font probe map reference changed unexpectedly")
        elif alias.exists():
            raise FontSourceError("The font probe map reference is not a symbolic link")
        else:
            alias.symlink_to(font_map)
        (directory / "font.tex").write_text(source)
        result = subprocess.run(
            ["pdftex", "-recorder", "-interaction=batchmode", "-halt-on-error", "font.tex"],
            cwd=directory, capture_output=True, text=True, timeout=30)
        (directory / "command.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise FontSourceError(f"TeX font metadata probe failed for {family}; see {directory}")
        reader = PdfReader(io.BytesIO((directory / "font.pdf").read_bytes()), strict=True)
        if len(reader.pages) != 1:
            raise FontSourceError("The font probe must produce exactly one page")
        fonts = reader.pages[0].get("/Resources", {}).get("/Font", {})
        if len(fonts) != 1:
            raise FontSourceError("The font probe did not resolve exactly one source font")
        metadata = _type1_metadata(next(iter(fonts.values())).get_object())
        record = {"family": family, "font_map": font_map,
                  "coverage": "all valid 8-bit TFM slots", **metadata}
        (directory / "metadata.json").write_text(json.dumps(record, indent=2) + "\n")
        return metadata
