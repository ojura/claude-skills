#!/usr/bin/env python3
"""Append bounded, checksummed data to an unchanged AppImage template.

This module uses only Python's standard library. The host doing the assembly
never executes the template or any uploaded packaging program.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import struct
import tempfile
import zlib

MAGIC = b"AIPAYLOAD-v1\0\0\0\0"
FOOTER = struct.Struct("<16sQQQ32s32s32s")
FORMAT = "appimage-data-v1"
MAX_FILES = 4096
MAX_MANIFEST = 4 * 1024 * 1024
MAX_FILE = 1024 * 1024 * 1024
MAX_TOTAL = 2 * MAX_FILE
CHUNK = 1024 * 1024


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK), b""):
            result.update(chunk)
    return result.hexdigest()


def canonical_json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def validate_metadata(value) -> None:
    if isinstance(value, str):
        if "\0" in value:
            raise ValueError("NUL is not supported in payload metadata")
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_metadata(key)
            validate_metadata(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_metadata(item)


def validate_path(name: str, policy: dict) -> None:
    if (not isinstance(name, str) or not name or len(name.encode()) > 1024
            or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or name.startswith("/") or any(p in ("", ".", "..") or len(p.encode()) > 255
                                           for p in name.split("/"))):
        raise ValueError(f"Unsafe payload path: {name!r}")
    path = PurePosixPath(name)
    if not any(name.startswith(prefix) for prefix in policy["prefixes"]):
        raise ValueError(f"Payload path is outside the template policy: {name}")
    if path.name not in policy["names"] and path.suffix not in policy["extensions"]:
        raise ValueError(f"Payload file type is outside the template policy: {name}")


def append(template: Path, files: dict[str, Path], output: Path,
           policy: dict, metadata: dict) -> dict:
    if os.path.lexists(output):
        raise ValueError(f"Preserving existing output: {output}")
    if not 0 < len(files) <= MAX_FILES:
        raise ValueError("Payload file count is outside the supported bounds")
    validate_metadata(metadata)
    if template.is_symlink() or not template.is_file():
        raise ValueError("The template must be a regular file")
    with template.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            raise ValueError("The template is not an ELF AppImage")
        source.seek(-min(FOOTER.size, template.stat().st_size), os.SEEK_END)
        if source.read(16) == MAGIC:
            raise ValueError("The template already contains a data payload")
    for name in files:
        validate_path(name, policy)
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents):
            raise ValueError(f"Payload file is also a parent directory: {name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".payload-", dir=output.parent)
    temporary_path = Path(temporary)
    manifest = {"format": FORMAT, "metadata": metadata, "files": []}
    template_hash = hashlib.sha256()
    payload_hash = hashlib.sha256()
    template_size = payload_size = total_size = 0
    try:
        with os.fdopen(descriptor, "wb") as target:
            with template.open("rb") as source:
                before = os.fstat(source.fileno())
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    template_hash.update(chunk)
                    target.write(chunk)
                    template_size += len(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("The template changed during assembly")
            for name, path in sorted(files.items()):
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"Payload input is not a regular file: {path}")
                file_hash = hashlib.sha256()
                compressor = zlib.compressobj(6)
                entry = {"path": name, "offset": payload_size, "bytes": 0,
                         "compressed_bytes": 0}
                with path.open("rb") as source:
                    before = os.fstat(source.fileno())
                    if before.st_size > MAX_FILE or total_size + before.st_size > MAX_TOTAL:
                        raise ValueError("Payload input exceeds the decompression bounds")
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        entry["bytes"] += len(chunk)
                        file_hash.update(chunk)
                        compressed = compressor.compress(chunk)
                        target.write(compressed)
                        payload_hash.update(compressed)
                        entry["compressed_bytes"] += len(compressed)
                    after = os.fstat(source.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError(f"Payload input changed during assembly: {path}")
                compressed = compressor.flush()
                target.write(compressed)
                payload_hash.update(compressed)
                entry["compressed_bytes"] += len(compressed)
                entry["sha256"] = file_hash.hexdigest()
                payload_size += entry["compressed_bytes"]
                total_size += entry["bytes"]
                manifest["files"].append(entry)
            encoded = canonical_json(manifest)
            if len(encoded) > MAX_MANIFEST:
                raise ValueError("The payload manifest exceeds its supported bound")
            target.write(encoded)
            target.write(FOOTER.pack(MAGIC, template_size, payload_size, len(encoded),
                                     template_hash.digest(), payload_hash.digest(),
                                     hashlib.sha256(encoded).digest()))
            target.flush()
            os.fsync(target.fileno())
        # Exclusive creation works in the hosted environment without links or
        # flock. A competing producer cannot overwrite this publication. The
        # complete footer is copied last, after a fully validated staging file.
        published = None
        try:
            try:
                final_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError as error:
                raise ValueError(f"Preserving output created by another producer: {output}") from error
            with os.fdopen(final_fd, "wb") as final:
                published = os.fstat(final.fileno())
                with temporary_path.open("rb") as source:
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        final.write(chunk)
                final.flush()
                os.fsync(final.fileno())
        except BaseException:
            if published is not None:
                try:
                    current = output.lstat()
                    if (current.st_dev, current.st_ino) == (published.st_dev, published.st_ino):
                        output.unlink()
                except FileNotFoundError:
                    pass
            raise
        return {"format": FORMAT, "template_bytes": template_size,
                "template_sha256": template_hash.hexdigest(),
                "payload_bytes": payload_size, "uncompressed_bytes": total_size,
                "payload_sha256": payload_hash.hexdigest(),
                "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
                "output_bytes": output.stat().st_size, "output_sha256": digest(output),
                "files": manifest["files"]}
    finally:
        temporary_path.unlink(missing_ok=True)


def inspect(path: Path) -> dict:
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size < FOOTER.size:
            raise ValueError("Missing data payload footer")
        source.seek(size - FOOTER.size)
        magic, base, payload, length, base_hash, payload_hash, manifest_hash = FOOTER.unpack(source.read())
        if magic != MAGIC or base + payload + length + FOOTER.size != size:
            raise ValueError("Invalid data payload footer")
        if not 0 < length <= MAX_MANIFEST:
            raise ValueError("Invalid manifest length")
        source.seek(base + payload)
        encoded = source.read(length)
        if hashlib.sha256(encoded).digest() != manifest_hash:
            raise ValueError("Manifest digest mismatch")
        for offset, count, expected in [(0, base, base_hash), (base, payload, payload_hash)]:
            source.seek(offset)
            checksum = hashlib.sha256()
            while count:
                chunk = source.read(min(count, CHUNK))
                if not chunk:
                    raise ValueError("Truncated payload image")
                checksum.update(chunk)
                count -= len(chunk)
            if checksum.digest() != expected:
                raise ValueError("Template or payload digest mismatch")
        manifest = json.loads(encoded)
        if manifest.get("format") != FORMAT:
            raise ValueError("Unsupported payload format")
        return {"template_bytes": base, "payload_bytes": payload,
                "manifest_sha256": manifest_hash.hex(), "manifest": manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("inspect")
    check.add_argument("image", type=Path)
    options = parser.parse_args()
    print(json.dumps(inspect(options.image), indent=2))


if __name__ == "__main__":
    main()
