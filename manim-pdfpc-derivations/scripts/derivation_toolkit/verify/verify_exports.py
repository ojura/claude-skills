#!/usr/bin/env python3
"""Regenerate into a build directory and verify committed figures against their source.

JSON/TeX metadata is byte-exact. PDF comparison follows the active object graph,
including drawing streams, glyph programs, advances, bounds and Unicode maps.
Only document dates/IDs, xref numbering and stream compression are excluded.
Small numeric drawing-operand differences are retained as release warnings;
other changes remain errors. Exact graph equality and digests are always reported.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

from pypdf import PdfReader
from pypdf.generic import (ArrayObject, BooleanObject, ByteStringObject, ContentStream,
                          DecodedStreamObject, DictionaryObject, FloatObject, IndirectObject,
                          NameObject, NullObject, NumberObject, StreamObject,
                          TextStringObject)

from ..catalog import Catalog, PACKAGE_ROOT
from ..paths import get_paths, output_path
from ..animation.export_precision import COORDINATE_DECIMALS

THEMES = ("light", "dark")
METADATA = ("palettes.tex", "steps.json", "steps.tex")
STREAM_ENCODING = {"/Length", "/Filter", "/DecodeParms"}
INFO_DATES = {"/CreationDate", "/ModDate"}
# This is a limit on each parsed drawing operand, not a PDF-number tolerance.
# Font/resource dictionaries, Unicode maps and metadata remain byte/value exact.
DRAWING_WARNING_LIMIT = Decimal(1).scaleb(-COORDINATE_DECIMALS - 2)
NUMERIC_DRAWING_OPERATORS = frozenset(
    b"cm w M d i m l c v y re G g RG rg K k SC sc SCN scn "
    b"Tc Tw Tz TL Tf Ts Td TD Tm TJ d0 d1".split())
# J, j and Tr select discrete states and therefore require exact numeric values.


class ExportMismatch(ValueError):
    """An export has an unsupported or inconsistent comparison structure."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _number(value):
    """Canonicalize an exact parsed value without rounding or a tolerance."""
    number = Decimal(str(value))
    if not number.is_finite():
        raise ExportMismatch("A PDF numeric value is not finite")
    sign, digits, exponent = number.as_tuple()
    digits = list(digits)
    if not any(digits):
        return "0"
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return ("-" if sign else "") + "".join(map(str, digits)) + "e" + str(exponent)


class _PdfGraph:
    """Canonical references preserve both sharing and cycles, without xref IDs."""

    def __init__(self, reader):
        self.reader = reader
        self.references = {}
        self.objects = []
        self.object_paths = []
        self.drawing_streams = {}
        self.info = reader.trailer.raw_get("/Info") if "/Info" in reader.trailer else None
        self.info_identity = self._identity(self.info) if isinstance(self.info, IndirectObject) else None

    @staticmethod
    def _identity(value):
        return value.idnum, value.generation

    def encode(self, value, path, *, info=False):
        if isinstance(value, IndirectObject):
            identity = self._identity(value)
            if identity not in self.references:
                index = len(self.objects)
                self.references[identity] = index
                self.objects.append(None)
                self.object_paths.append(path)
                self.objects[index] = self.encode(
                    value.get_object(), path, info=identity == self.info_identity)
            return {"reference": self.references[identity]}
        if isinstance(value, StreamObject):
            return {"kind": "stream",
                    "entries": self._dictionary(value, path, STREAM_ENCODING),
                    "data": value.get_data()}
        if isinstance(value, DictionaryObject):
            if value.get("/Type") == "/Page" and "/Contents" in value:
                self._mark_drawing_streams(value.raw_get("/Contents"), "page-content")
            if value.get("/Type") == "/Font" and value.get("/Subtype") == "/Type3":
                for stream in value["/CharProcs"].values():
                    self._mark_drawing_streams(stream, "type3-glyph")
            return {"kind": "dictionary",
                    "entries": self._dictionary(value, path, INFO_DATES if info else set())}
        if isinstance(value, ArrayObject):
            return [self.encode(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, NameObject):
            return {"kind": "name", "value": str(value)}
        if isinstance(value, TextStringObject):
            return {"kind": "text", "value": str(value)}
        if isinstance(value, ByteStringObject):
            return {"kind": "bytes", "value": bytes(value)}
        if isinstance(value, BooleanObject):
            return {"kind": "boolean", "value": value.value}
        if isinstance(value, (NumberObject, FloatObject)):
            return {"kind": "number", "value": _number(value)}
        if isinstance(value, NullObject):
            return {"kind": "null"}
        raise ExportMismatch(f"Unsupported PDF value at {path}: {type(value).__name__}")

    def _mark_drawing_streams(self, value, role):
        if isinstance(value, IndirectObject):
            target = value.get_object()
            if isinstance(target, StreamObject):
                self.drawing_streams.setdefault(self._identity(value), set()).add(role)
                return
            value = target
        if isinstance(value, ArrayObject):
            for item in value:
                self._mark_drawing_streams(item, role)

    def _dictionary(self, value, path, ignored):
        return {str(key): self.encode(value.raw_get(key), f"{path}{key}")
                for key in sorted(value, key=str) if str(key) not in ignored}

    def result(self):
        roots = {"catalog": self.encode(self.reader.trailer.raw_get("/Root"), "/Root")}
        roots["info"] = self.encode(self.info, "/Info", info=True) if self.info is not None else None
        return {"header": self.reader.pdf_header, "roots": roots,
                "objects": self.objects, "object_paths": self.object_paths}


def _read_pdf(filename):
    reader = PdfReader(filename, strict=True)
    if reader.is_encrypted:
        raise ExportMismatch("Generated figure PDFs must not be encrypted")
    encoder = _PdfGraph(reader)
    graph = encoder.result()
    roles = {encoder.references[identity]: sorted(values)
             for identity, values in encoder.drawing_streams.items()
             if identity in encoder.references}
    return graph, roles


def canonical_pdf(filename):
    return _read_pdf(filename)[0]


def _short(value):
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": digest(value)}
    if isinstance(value, list):
        return {"items": len(value)}
    if isinstance(value, dict):
        return {"keys": sorted(value)}
    return value


def _byte_context(value, offset):
    """Bound diagnostics independently of stream size, retaining exact bytes."""
    start, end = max(0, offset - 32), min(len(value), offset + 64)
    sample = value[start:end]
    return {"start": start, "end": end, "hex": sample.hex(),
            "ascii": sample.decode("ascii", errors="backslashreplace")}


def first_difference(left, right, path="$"):
    if type(left) is not type(right):
        return {"path": path, "committed": _short(left), "generated": _short(right)}
    if isinstance(left, dict):
        if set(left) != set(right):
            return {"path": path, "committed": _short(left), "generated": _short(right)}
        for key in sorted(left):
            difference = first_difference(left[key], right[key], f"{path}.{key}")
            if difference:
                return difference
    elif isinstance(left, list):
        if len(left) != len(right):
            return {"path": path, "committed": _short(left), "generated": _short(right)}
        for index, (a, b) in enumerate(zip(left, right)):
            difference = first_difference(a, b, f"{path}[{index}]")
            if difference:
                return difference
    elif isinstance(left, bytes) and left != right:
        offset = next((index for index, (a, b) in enumerate(zip(left, right)) if a != b),
                      min(len(left), len(right)))
        return {"path": path, "committed": _short(left), "generated": _short(right),
                "byte_offset": offset,
                "committed_byte": left[offset] if offset < len(left) else None,
                "generated_byte": right[offset] if offset < len(right) else None,
                "committed_context": _byte_context(left, offset),
                "generated_context": _byte_context(right, offset)}
    elif left != right:
        return {"path": path, "committed": _short(left), "generated": _short(right)}
    return None


def _graph_digest(graph):
    # Comparison itself uses decoded bytes. Hashes are only compact report data.
    encoded = json.dumps(graph, sort_keys=True, separators=(",", ":"),
                         default=lambda value: _short(value)).encode()
    return digest(encoded)


def _all_differences(left, right, path="$"):
    """Inspect every difference, so an early warning cannot hide a later error."""
    if type(left) is not type(right):
        yield path, left, right
    elif isinstance(left, dict):
        if set(left) != set(right):
            yield path, left, right
        else:
            for key in sorted(left):
                yield from _all_differences(left[key], right[key], f"{path}.{key}")
    elif isinstance(left, list):
        if len(left) != len(right):
            yield path, left, right
        else:
            for index, (a, b) in enumerate(zip(left, right)):
                yield from _all_differences(a, b, f"{path}[{index}]")
    elif left != right:
        yield path, left, right


def _operand_changes(left, right, path, *, numeric_allowed):
    numeric = (NumberObject, FloatObject)
    if isinstance(left, numeric) and isinstance(right, numeric):
        a, b = Decimal(str(left)), Decimal(str(right))
        if not a.is_finite() or not b.is_finite():
            yield {"path": path, "severity": "error", "reason": "non-finite numeric operand"}
        elif a != b:
            delta = b - a
            yield {"path": path, "committed": str(a), "generated": str(b),
                   "delta": str(delta), "kind": "numeric-operand",
                   "severity": "warning" if numeric_allowed and abs(delta) <= DRAWING_WARNING_LIMIT else "error",
                   "reason": "bounded drawing operand" if numeric_allowed and abs(delta) <= DRAWING_WARNING_LIMIT
                             else "numeric operand exceeds the drawing policy"}
        return
    if type(left) is not type(right):
        yield {"path": path, "severity": "error", "reason": "operand type changed",
               "committed": type(left).__name__, "generated": type(right).__name__}
    elif isinstance(left, (list, ArrayObject)):
        if len(left) != len(right):
            yield {"path": path, "severity": "error", "reason": "operand count changed"}
        else:
            for index, (a, b) in enumerate(zip(left, right)):
                yield from _operand_changes(a, b, f"{path}[{index}]", numeric_allowed=numeric_allowed)
    elif isinstance(left, (dict, DictionaryObject)):
        if set(left) != set(right):
            yield {"path": path, "severity": "error", "reason": "operand dictionary changed"}
        else:
            for key in sorted(left):
                # Dictionary operands carry marked-content or image metadata.
                yield from _operand_changes(left[key], right[key], f"{path}.{key}", numeric_allowed=False)
    elif isinstance(left, (TextStringObject, ByteStringObject)):
        a, b = left.original_bytes, right.original_bytes
        if a != b:
            yield {"path": path, "severity": "error", "reason": "text bytes changed",
                   "committed": _short(a), "generated": _short(b)}
    elif left != right:
        yield {"path": path, "severity": "error", "reason": "non-numeric operand changed",
               "committed": _short(left), "generated": _short(right)}


def _drawing_changes(left, right):
    operations = []
    for data in (left, right):
        stream = DecodedStreamObject()
        stream.set_data(data)
        operations.append(ContentStream(stream, None).operations)
    a, b = operations
    if len(a) != len(b):
        return [{"severity": "error", "reason": "drawing operator count changed",
                 "committed": len(a), "generated": len(b)}]
    changes = []
    for index, ((old, old_op), (new, new_op)) in enumerate(zip(a, b)):
        if old_op != new_op:
            changes.append({"operation": index, "severity": "error", "reason": "drawing operator changed",
                            "committed": old_op.decode("ascii", "backslashreplace"),
                            "generated": new_op.decode("ascii", "backslashreplace")})
            continue
        for change in _operand_changes(old, new, f"operation[{index}].operands",
                                       numeric_allowed=old_op in NUMERIC_DRAWING_OPERATORS):
            changes.append({"operation": index, "operator": old_op.decode("ascii", "backslashreplace"), **change})
    return changes


def _classify_differences(left, right, left_roles, right_roles):
    records = []
    for path, a, b in _all_differences(left, right):
        record = {"path": path, "severity": "error", "committed": _short(a), "generated": _short(b)}
        match = re.fullmatch(r"\$\.objects\[(\d+)\]\.data", path)
        if match and isinstance(a, bytes) and isinstance(b, bytes):
            index = int(match[1])
            record["object_path"] = left["object_paths"][index]
            if left_roles.get(index) and left_roles.get(index) == right_roles.get(index):
                record["stream_roles"] = left_roles[index]
                try:
                    changes = _drawing_changes(a, b)
                    record["operand_changes"] = changes
                    if not any(item["severity"] == "error" for item in changes):
                        record["severity"] = "warning"
                        record["reason"] = ("bounded numeric drawing operands" if changes
                                            else "identical parsed drawing operations")
                except Exception as error:
                    record["reason"] = f"drawing stream could not be parsed: {type(error).__name__}: {error}"
            else:
                record["reason"] = "changed stream is not matching page or Type3 drawing content"
        else:
            record["reason"] = "active PDF metadata, resource or structure changed"
        records.append(record)
    return records


def compare_pdf(committed, generated):
    left, left_roles = _read_pdf(committed)
    right, right_roles = _read_pdf(generated)
    difference = first_difference(left, right)
    if difference and (match := re.match(r"^\$\.objects\[(\d+)\]", difference["path"])):
        index = int(match[1])
        difference["object_paths"] = {
            "committed": left["object_paths"][index],
            "generated": right["object_paths"][index]}
    details = _classify_differences(left, right, left_roles, right_roles) if difference else []
    status = "error" if any(item["severity"] == "error" for item in details) else "warning" if details else "equal"
    return {"equal": difference is None, "difference": difference, "release_status": status,
            "drawing_warning_limit": str(DRAWING_WARNING_LIMIT), "classified_differences": details,
            "committed_graph_sha256": _graph_digest(left),
            "generated_graph_sha256": _graph_digest(right),
            "committed_objects": len(left["objects"]), "generated_objects": len(right["objects"])}


def declared_files(directory):
    metadata = json.loads((directory / "steps.json").read_text())
    figures = set()
    for scene in metadata["scenes"]:
        names = [scene["poster"]]
        for step in scene["steps"]:
            names.extend([step["figure"], step["full_figure"]])
        for name in names:
            if name is None:
                continue
            path = PurePosixPath(name)
            if path.name != name or path.suffix != ".pdf":
                raise ExportMismatch(f"Invalid declared figure filename: {name!r}")
            figures.add(name)
    if not figures:
        raise ExportMismatch("No figures are declared in steps.json")
    themes = metadata.get("themes", THEMES)
    manifest = metadata.get("manifest", "derivations.vector.json")
    return (set(METADATA) | {f"{theme}/{manifest}" for theme in themes}
            | {f"{theme}/{figure}" for theme in themes for figure in figures})


def compare_trees(committed, generated):
    expected = declared_files(generated)
    records, differences = [], []
    for label, directory in (("committed", committed), ("generated", generated)):
        declared = declared_files(directory)
        actual = {path.relative_to(directory).as_posix()
                  for path in directory.rglob("*") if path.is_file()}
        if declared != expected or actual != expected:
            differences.append({"file": label, "kind": "file-set", "release_status": "error",
                                "missing": sorted(expected - actual),
                                "unused": sorted(actual - expected),
                                "declared_only": sorted(declared - expected),
                                "undeclared_expected": sorted(expected - declared)})
    for relative in sorted(expected):
        old, new = committed / relative, generated / relative
        if not old.is_file() or not new.is_file():
            continue
        record = {"file": relative, "committed_sha256": digest(old.read_bytes()),
                  "generated_sha256": digest(new.read_bytes())}
        try:
            if old.suffix == ".pdf":
                record.update(kind="pdf-graph", **compare_pdf(old, new))
            else:
                equal = old.read_bytes() == new.read_bytes()
                record.update(kind="exact-bytes", equal=equal, release_status="equal" if equal else "error")
        except Exception as error:
            record.update(equal=False, release_status="error", error=f"{type(error).__name__}: {error}")
        records.append(record)
        if not record["equal"]:
            differences.append(record)
    warnings = [item for item in differences if item["release_status"] == "warning"]
    errors = [item for item in differences if item["release_status"] == "error"]
    return {"ok": not errors, "exact_match": not differences, "declared_files": len(expected),
            "figures": sum(name.endswith(".pdf") for name in expected),
            "files": records, "differences": differences, "warnings": warnings, "errors": errors}


def emit_warning_annotations(report):
    for warning in report.get("warnings", []):
        message = (f"{warning['file']}: drawing differences are within the {DRAWING_WARNING_LIMIT} "
                   "parsed-operand limit or have identical parsed operations. Exact graph differences "
                   "and hashes are retained in the export consistency report; packaging continues.")
        message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning title=PDF export numeric difference::{message}")


def external_output(value, repository):
    try:
        return output_path(value, repository)
    except ValueError as error:
        raise ExportMismatch(str(error)) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path)
    parser.add_argument("deck", nargs="?")
    parser.add_argument("--committed-dir", type=Path)
    parser.add_argument("--output-dir", type=Path,
                        help="Build directory for retained fresh export runs, logs and reports")
    args = parser.parse_args(argv)
    catalog = Catalog(args.project)
    deck = catalog.get(args.deck)
    if deck.animation is None:
        parser.error("Export consistency requires an animated deck")
    paths = get_paths(deck)
    args.committed_dir = args.committed_dir or deck.animation.assets
    try:
        output = external_output(args.output_dir or paths.review_dir / "export-consistency", catalog.root)
    except ExportMismatch as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    generated = run / "generated"
    command = [sys.executable, "-B", "-m", "derivation_toolkit", "--project", str(catalog.root),
               "export", deck.id, "--theme", "both", "--output", str(generated)]
    report = {"ok": False, "started_utc": datetime.now(timezone.utc).isoformat(),
              "committed_dir": str(args.committed_dir.resolve()),
              "generated_dir": str(generated), "command": command}
    print(f"Fresh export and consistency evidence: {run}", flush=True)
    try:
        with (run / "export.log").open("w") as log:
            import os
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(PACKAGE_ROOT.parent) + os.pathsep + environment.get("PYTHONPATH", "")
            result = subprocess.run(command, cwd=catalog.root, env=environment,
                                    stdout=log, stderr=subprocess.STDOUT)
        report["export_exit_code"] = result.returncode
        if result.returncode:
            report["error"] = f"Fresh export failed; see {run / 'export.log'}"
        else:
            report.update(compare_trees(args.committed_dir.resolve(), generated))
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    (run / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    emit_warning_annotations(report)
    print(json.dumps({"ok": report["ok"], "report": str(run / "report.json"),
                      "figures": report.get("figures"), "error": report.get("error"),
                      "warnings": len(report.get("warnings", [])), "errors": len(report.get("errors", [])),
                      "differences": report.get("differences", [])},
                     indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
