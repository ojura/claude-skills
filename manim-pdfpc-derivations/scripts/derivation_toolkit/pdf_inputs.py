"""Inspect compiled PDF links using installed readers, without Python PDF packages."""
from __future__ import annotations

import ctypes as C
from ctypes.util import find_library
from pathlib import Path
import shutil
import subprocess

from .timeline import load_timeline, validate_scene_links


class ReaderUnavailable(RuntimeError):
    pass


class _GError(C.Structure):
    _fields_ = [("domain", C.c_uint), ("code", C.c_int), ("message", C.c_char_p)]


class _GList(C.Structure):
    pass


_GList._fields_ = [("data", C.c_void_p), ("next", C.POINTER(_GList)), ("prev", C.POINTER(_GList))]


class _Rectangle(C.Structure):
    _fields_ = [(name, C.c_double) for name in ("x1", "y1", "x2", "y2")]


class _Link(C.Structure):
    _fields_ = [("area", _Rectangle), ("action", C.c_void_p)]


class _UriAction(C.Structure):
    _fields_ = [("type", C.c_int), ("title", C.c_char_p), ("uri", C.c_char_p)]


def _library(name):
    filename = find_library(name)
    if not filename:
        raise ReaderUnavailable(f"Installed {name} library was not found")
    try:
        return C.CDLL(filename)
    except OSError as error:
        raise ReaderUnavailable(str(error)) from error


def _function(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


def poppler_links(path: Path):
    """Use the same installed Poppler URI actions as the native presentation player."""
    poppler, gobject, glib = (_library(name) for name in ("poppler-glib", "gobject-2.0", "glib-2.0"))
    open_pdf = _function(poppler, "poppler_document_new_from_file",
                         [C.c_char_p, C.c_char_p, C.POINTER(C.POINTER(_GError))], C.c_void_p)
    page_count = _function(poppler, "poppler_document_get_n_pages", [C.c_void_p], C.c_int)
    get_page = _function(poppler, "poppler_document_get_page", [C.c_void_p, C.c_int], C.c_void_p)
    get_links = _function(poppler, "poppler_page_get_link_mapping", [C.c_void_p], C.POINTER(_GList))
    free_links = _function(poppler, "poppler_page_free_link_mapping", [C.POINTER(_GList)], None)
    unref = _function(gobject, "g_object_unref", [C.c_void_p], None)
    free_error = _function(glib, "g_error_free", [C.POINTER(_GError)], None)
    error = C.POINTER(_GError)()
    document = open_pdf(path.resolve().as_uri().encode("utf-8"), None, C.byref(error))
    if not document:
        message = "Cannot open PDF"
        if error:
            message = error.contents.message.decode("utf-8", "replace")
            free_error(error)
        raise ValueError(f"Cannot read PDF {path}: {message}")
    try:
        count = page_count(document)
        if count <= 0:
            raise ValueError(f"PDF contains no readable pages: {path}")
        pages = []
        for number in range(count):
            page = get_page(document, number)
            if not page:
                raise ValueError(f"Cannot read PDF page {number + 1}: {path}")
            try:
                links = get_links(page)
                try:
                    values = []
                    node = links
                    while node:
                        link = C.cast(node.contents.data, C.POINTER(_Link)).contents
                        if link.action:
                            action = C.cast(link.action, C.POINTER(_UriAction)).contents
                            if action.type == 5:  # POPPLER_ACTION_URI, public stable ABI.
                                if action.uri is None:
                                    raise ValueError(f"PDF page {number + 1} has an invalid URI action")
                                values.append(action.uri.decode("utf-8"))
                        node = node.contents.next
                    pages.append(values)
                finally:
                    if links:
                        free_links(links)
            finally:
                unref(page)
        return pages
    except UnicodeError as error:
        raise ValueError(f"PDF contains a URI that is not UTF-8: {path}") from error
    finally:
        unref(document)


def tex_links(path: Path):
    """Use installed TeX Live on hosts where Poppler's library is unavailable."""
    executable = shutil.which("texlua")
    if not executable:
        raise ReaderUnavailable("Installed texlua was not found")
    script = Path(__file__).parent / "tex/pdf-links.lua"
    result = subprocess.run([executable, str(script), str(path.resolve())],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"Cannot read PDF {path}: {result.stderr or result.stdout}")
    lines = result.stdout.splitlines()
    try:
        if not lines or not lines[0].startswith("Pages: "):
            raise ValueError("Missing reader page count")
        count = int(lines[0][7:])
        if count <= 0:
            raise ValueError("No readable PDF pages")
        pages = [[] for _ in range(count)]
        for line in lines[1:]:
            if not line.startswith("URI: "):
                raise ValueError("Unexpected PDF reader output")
            number, encoded = line[5:].split(" ", 1)
            index = int(number) - 1
            if not 0 <= index < count:
                raise ValueError("PDF reader page out of range")
            raw = bytes.fromhex(encoded)
            uri = raw.decode("utf-16") if raw.startswith((b"\xfe\xff", b"\xff\xfe")) else raw.decode("utf-8")
            pages[index].append(uri)
        return pages
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid PDF reader result for {path}: {error}") from error


def inspect_pdf(path: Path):
    """Try installed readers, falling back only when the first is unavailable."""
    try:
        return poppler_links(path)
    except ReaderUnavailable:
        try:
            return tex_links(path)
        except ReaderUnavailable as error:
            raise RuntimeError("PDF input validation requires installed Poppler GLib or TeX Live texlua") from error


def validate_compiled_input(pdf: Path, vectors: Path | None = None):
    """Validate the complete timeline and its actual PDF links before staging."""
    scenes = load_timeline(vectors)
    pages = inspect_pdf(pdf)
    validate_scene_links(pages, scenes)
    return len(pages)
