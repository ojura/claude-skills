"""Check that emitted vector glyphs are painted, Unicode mapped and selectable."""
from __future__ import annotations

import math
from pathlib import Path
import re

import fitz


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def pdf_references(value):
    return [int(match[1]) for match in re.finditer(r"\b(\d+)\s+\d+\s+R\b", value)]


def check_type3_programs(document, fonts, label):
    programs = set()
    for font in fonts:
        kind, value = document.xref_get_key(font[0], "CharProcs")
        if kind == "xref":
            value = document.xref_object(pdf_references(value)[0])
        glyphs = pdf_references(value)
        require(glyphs, f"{label}: Type3 glyph programs are missing")
        programs.update(glyphs)
    pending = [font[0] for font in fonts]
    visited = set()
    vector_programs = set()
    while pending:
        xref = pending.pop()
        if xref in visited:
            continue
        visited.add(xref)
        value = document.xref_object(xref)
        require(not re.search(r"/Subtype\s*/Image\b", value),
                f"{label}: a Type3 glyph font embeds raster content")
        if document.xref_is_stream(xref) and (xref in programs or
                                              re.search(r"/Subtype\s*/Form\b", value)):
            content = document.xref_stream(xref)
            require(not re.search(rb"(?:^|\s)BI(?=\s)", content),
                    f"{label}: a Type3 glyph program contains an inline raster image")
            if (xref in programs and re.search(rb"(?:^|\s)m(?=\s|$)", content) and
                    re.search(rb"(?:^|\s)(?:f\*?|S|s|B\*?|b\*?)(?=\s|$)", content)):
                vector_programs.add(xref)
        pending.extend(pdf_references(value))
    require(vector_programs, f"{label}: Type3 programs do not paint vector paths")


def check_selectable_vector_text(page, label, *, only_type3=False):
    fonts = page.get_fonts(full=True)
    type3 = [font for font in fonts if font[2] == "Type3"]
    require(type3, f"{label}: selectable vector glyph fonts are missing")
    if only_type3:
        require(len(type3) == len(fonts), f"{label}: unexpected non-vector-glyph font")
        # A figure is a self-contained one-page PDF. Inspect every object so an
        # image nested in a glyph font cannot escape page-level image listings.
        require(not any(re.search(r"/Subtype\s*/Image\b", page.parent.xref_object(xref))
                        for xref in range(1, page.parent.xref_length())),
                f"{label}: a figure PDF object contains raster content")
    check_type3_programs(page.parent, type3, label)
    # Cairo's user fonts omit BaseFont; MuPDF identifies these as Unnamed-T3.
    # The trace proves that a Type3 font is actually painted, not merely declared
    # in PDF resources. Its outlines are not counted as ordinary page drawings.
    names = {font[3] or "Unnamed-T3" for font in type3}
    characters = [character for span in page.get_texttrace()
                  if span["font"] in names and span["type"] in (0, 1) and span["opacity"] > 0
                  for character in span["chars"]]
    require(characters, f"{label}: visible selectable vector text is missing")
    require(all(character[0] not in (0, 0xfffd) for character in characters),
            f"{label}: a vector glyph lost its Unicode mapping")
    require(any(not chr(character[0]).isspace() and
                all(math.isfinite(value) for value in character[3]) and
                (fitz.Rect(character[3]) & page.rect).get_area() > 0
                for character in characters),
            f"{label}: vector text has invalid or empty selection bounds")


def validate_vector_text(path: Path, *, figure=False) -> dict:
    """Check authored glyphs while allowing geometry-only scenes and ordinary document fonts."""
    pages_with_text = 0
    with fitz.open(path) as document:
        if figure:
            require(len(document) == 1, f"{path}: a figure must have one page")
            require(not any(re.search(r"/Subtype\s*/Image\b", document.xref_object(xref))
                            for xref in range(1, document.xref_length())),
                    f"{path}: an authored figure contains raster content")
        for page in document:
            fonts = page.get_fonts(full=True)
            has_vector_text = any(font[2] == "Type3" for font in fonts)
            if figure and fonts:
                require(has_vector_text, f"{path}: an authored figure has no source-mapped vector font")
            if has_vector_text:
                check_selectable_vector_text(page, f"{path}: page {page.number + 1}",
                                             only_type3=figure)
                pages_with_text += 1
    return {"selectable_vector_text_pages": pages_with_text}
