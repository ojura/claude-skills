"""Prepare a Focal runtime template for later data-only presentation assembly."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from ..catalog import Catalog
from ..paths import output_path
from .build import native_pin
from .payload_bundle import digest

HERE = Path(__file__).resolve().parent
PAYLOAD = Path("usr/share/derivation-presentations")
TEMPLATE_FORMAT = "derivation-data-template-v1"


def run(command, **kwargs):
    print("+ " + shlex.join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def build_template(catalog: Catalog, appdir: Path, tools: Path,
                   destination: Path | None = None) -> dict:
    """Retain a verified runtime closure and add the bounded data reader."""
    system = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    if (system.get("ID", "").strip('"') != "ubuntu"
            or system.get("VERSION_ID", "").strip('"') != "20.04" or os.uname().machine != "x86_64"):
        raise ValueError("Prepare the native template in the Ubuntu 20.04 x86_64 builder")
    origin, tools = appdir.expanduser().resolve(), tools.expanduser().resolve()
    requested = destination or catalog.root / "build/appimage/template"
    if os.path.lexists(Path(requested).expanduser()):
        raise ValueError(f"Preserving existing template directory: {requested}")
    destination = output_path(requested, catalog.root)
    origin_payload = origin / PAYLOAD
    info = (origin_payload / "build-info.txt").read_text()
    fields = dict(line.split(": ", 1) for line in info.splitlines() if ": " in line)
    repository, native = native_pin(catalog)
    if (not fields["Build OS"].startswith("Ubuntu 20.04")
            or fields["Native repository"] != repository or fields["Native revision"] != native
            or tuple(map(int, fields["Highest required GLIBC"].split("."))) > (2, 31)):
        raise ValueError("The Focal runtime identity must match the catalog's native pin")
    pins = {parts[0]: parts[1] for line in (HERE / "toolchain.lock").read_text().splitlines()
            if (parts := line.split()) and not line.startswith("#")}
    for name in ("appimagetool-x86_64.AppImage", "runtime-x86_64"):
        if digest(tools / name) != pins[name]:
            raise ValueError(f"Packaging tool hash mismatch: {name}")
    if (origin / "AppRun").read_bytes() != (HERE / "AppRun").read_bytes():
        raise ValueError("Rebuild the input AppDir with this toolkit's current application launcher")
    rows = (origin_payload / "decks.tsv").read_text().splitlines()
    if not rows or any(len(row.split("\t")) != 7 for row in rows):
        raise ValueError("The input AppDir must use the current generic catalog contract")
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / "AppDir"

    def ignore(path, names):
        return [PAYLOAD.name] if Path(path) == origin / PAYLOAD.parent else []

    shutil.copytree(origin, target, symlinks=True, ignore=ignore)
    metadata = target / "usr/share/appimage-payload"
    metadata.mkdir(parents=True)
    (metadata / "runtime-origin.txt").write_text(info)
    shutil.copyfile(HERE / "payload-policy.json", metadata / "policy.json")
    application = target / "usr/libexec/payload-application"
    application.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "AppRun", application)
    application.chmod(0o755)
    shutil.copyfile(HERE / "PayloadAppRun", target / "AppRun")
    (target / "AppRun").chmod(0o755)
    reader = target / "usr/bin/appimage-payload-reader"
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "glib-2.0", "json-glib-1.0", "zlib"], text=True))
    run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", HERE / "payload_reader.c",
         "-o", reader, "-Wl,-rpath,$ORIGIN/../lib", *flags])
    versions = subprocess.check_output(["readelf", "--version-info", str(reader)], text=True)
    required = sorted({tuple(map(int, value.split("."))) for value in re.findall(r"Name: GLIBC_([0-9.]+)", versions)})
    if not required or required[-1] > (2, 31):
        raise ValueError("The reader exceeds the Ubuntu 20.04 GLIBC baseline")
    needed = subprocess.check_output(["readelf", "-d", str(reader)], text=True)
    glibc = re.compile(r"lib(?:c|m|pthread|dl|rt|resolv|util)\.so\.")
    for library in re.findall(r"Shared library: \[([^]]+)\]", needed):
        if not glibc.match(library) and not (target / "usr/lib" / library).is_file():
            raise ValueError(f"The reader needs an unbundled library: {library}")
    checked = 0
    for path in origin.rglob("*"):
        if path.is_symlink() or not path.is_file() or origin_payload in path.parents or path == origin / "AppRun":
            continue
        if digest(path) != digest(target / path.relative_to(origin)):
            raise ValueError(f"Runtime file changed unexpectedly: {path}")
        checked += 1
    extracted = destination / "packer"
    extracted.mkdir()
    with (destination / "packer-extract.log").open("w") as log:
        run([tools / "appimagetool-x86_64.AppImage", "--appimage-extract"], cwd=extracted,
            stdout=log, stderr=subprocess.STDOUT)
    template = destination / "template.appimage"
    run([extracted / "squashfs-root/AppRun", "--runtime-file", tools / "runtime-x86_64",
         "--comp", "zstd", "--mksquashfs-opt=-processors", "--mksquashfs-opt=1",
         "--mksquashfs-opt=-no-xattrs", target, template], env={**os.environ, "ARCH": "x86_64"})
    template.chmod(0o755)
    receipt = {
        "format": TEMPLATE_FORMAT, "version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "template_sha256": digest(template), "template_bytes": template.stat().st_size,
        "native_repository": repository, "native_revision": native,
        "runtime_source_revision": fields["Source revision"],
        "runtime_build_os": fields["Build OS"], "highest_required_glibc": fields["Highest required GLIBC"],
        "reader_required_glibc": ".".join(map(str, required[-1])),
        "runtime_origin_sha256": digest(origin_payload / "build-info.txt"),
        "unchanged_runtime_file_count": checked,
        "native_binary_sha256": digest(target / "usr/bin/pdfpc"),
        "application_launcher_sha256": digest(application),
        "payload_launcher_sha256": digest(target / "AppRun"),
        "payload_reader_sha256": digest(reader), "payload_reader_source_sha256": digest(HERE / "payload_reader.c"),
        "font_template_sha256": digest(target / "etc/fonts/fonts.conf.in"),
        "policy_sha256": digest(metadata / "policy.json"),
        "appimage_runtime_sha256": pins["runtime-x86_64"],
        "packer_sha256": pins["appimagetool-x86_64.AppImage"],
    }
    (destination / "template.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return {"directory": str(destination), **receipt}
