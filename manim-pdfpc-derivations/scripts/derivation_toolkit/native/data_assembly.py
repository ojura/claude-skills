"""Transport a pinned runtime and assemble presentation data with installed Python/TeX."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import traceback
import zipfile

from ..build import build_deck
from ..catalog import Catalog, THEMES
from ..package_assets import collect_assets, stage_assets
from ..paths import get_paths, output_path
from .build import native_pin
from .package import package_identity, provenance_revision
from .payload_bundle import append, digest
from .payload_template import HERE, PAYLOAD, TEMPLATE_FORMAT

TRANSPORT_FORMAT = "derivation-data-transport-v1"
PART_BYTES = 16_000_000


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_template(catalog: Catalog, info: dict) -> None:
    if info.get("format") != TEMPLATE_FORMAT or info.get("version") != 1:
        raise ValueError("Unsupported data-only template receipt")
    if (info.get("native_repository"), info.get("native_revision")) != native_pin(catalog):
        raise ValueError("The template and project native pins differ")
    for field, source in (("policy_sha256", "payload-policy.json"),
                          ("application_launcher_sha256", "AppRun"),
                          ("payload_launcher_sha256", "PayloadAppRun"),
                          ("payload_reader_source_sha256", "payload_reader.c")):
        if info.get(field) != digest(HERE / source):
            raise ValueError(f"The template and toolkit differ: {source}")
    if (not isinstance(info.get("template_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", info["template_sha256"]) is None
            or type(info.get("template_bytes")) is not int or info["template_bytes"] < 4):
        raise ValueError("Invalid template identity in its receipt")


def prepare_transport(catalog: Catalog, template: Path, template_info: Path, *,
                      destination: Path | None = None, selection="all",
                      source_revision=None, part_bytes=PART_BYTES) -> dict:
    """Write regular data parts and a configuration for the selected project."""
    catalog.select(selection)
    identity = package_identity(catalog)
    revision = provenance_revision(catalog, source_revision)
    info = json.loads(template_info.read_text())
    validate_template(catalog, info)
    if template.is_symlink() or not template.is_file():
        raise ValueError("The template must be a regular file")
    if template.stat().st_size != info["template_bytes"] or digest(template) != info["template_sha256"]:
        raise ValueError("The template differs from its local build receipt")
    if type(part_bytes) is not int or not 0 < part_bytes <= PART_BYTES:
        raise ValueError("Transport parts must contain between 1 and 16000000 bytes")
    requested = destination or catalog.root / "build/appimage/transport"
    if os.path.lexists(Path(requested).expanduser()):
        raise ValueError(f"Preserving existing transport directory: {requested}")
    destination = output_path(requested, catalog.root)
    destination.mkdir(parents=True, exist_ok=False)
    parts = []
    with template.open("rb") as source:
        while chunk := source.read(part_bytes):
            if len(parts) == 1000:
                raise ValueError("Use larger transport parts; at most 1000 are supported")
            name = f"template.appimage.part-{len(parts):03d}"
            (destination / name).write_bytes(chunk)
            parts.append({"name": name, "bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
    configuration = {
        "format": TRANSPORT_FORMAT, "version": 1, "source_revision": revision,
        "package_id": identity, "selection": selection,
        "catalog_sha256": digest(catalog.root / "presentations.json"),
        "template": info, "template_parts": parts,
    }
    (destination / "config.json").write_text(json.dumps(configuration, indent=2) + "\n")
    return {"directory": str(destination), **configuration}


def restore_template(catalog: Catalog, source: Path, destination: Path) -> dict:
    config = json.loads((source / "config.json").read_text())
    if config.get("format") != TRANSPORT_FORMAT or config.get("version") != 1:
        raise ValueError("Unsupported data-only transport configuration")
    if (config.get("package_id") != package_identity(catalog)
            or config.get("catalog_sha256") != digest(catalog.root / "presentations.json")):
        raise ValueError("The transport configuration belongs to different project metadata")
    catalog.select(config["selection"])
    provenance_revision(catalog, config["source_revision"])
    info = config["template"]
    validate_template(catalog, info)
    parts = config["template_parts"]
    if not isinstance(parts, list) or not 0 < len(parts) <= 1000:
        raise ValueError("Invalid template part inventory")
    with destination.open("xb") as target:
        total = 0
        for index, entry in enumerate(parts):
            name = f"template.appimage.part-{index:03d}"
            if (entry.get("name") != name or type(entry.get("bytes")) is not int
                    or not 0 < entry["bytes"] <= PART_BYTES):
                raise ValueError("Invalid template transport part")
            part = source / name
            if part.is_symlink() or not part.is_file() or part.stat().st_size != entry["bytes"]:
                raise ValueError(f"Template part is not the expected regular file: {name}")
            if digest(part) != entry.get("sha256"):
                raise ValueError(f"Template part digest mismatch: {name}")
            total += entry["bytes"]
            if total > info["template_bytes"]:
                raise ValueError("Transport parts exceed the declared template size")
            with part.open("rb") as handle:
                shutil.copyfileobj(handle, target)
    if destination.stat().st_size != info["template_bytes"] or digest(destination) != info["template_sha256"]:
        raise ValueError("Template transport bytes changed")
    return config


def write_new(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as output:
        output.write(text)


def assemble(catalog: Catalog, transport: Path, *, work_dir: Path | None = None,
             output_dir: Path | None = None, inputs: Path | None = None,
             host_output_dir: Path | None = None) -> dict:
    """Compile missing Beamer inputs and append only cataloged data to the template.

    host_output_dir explicitly selects a host's compile-artifact directory, such
    as Overleaf's current directory. Ordinary products retain the build/ guard.
    """
    if output_dir is not None and host_output_dir is not None:
        raise ValueError("Choose the ordinary output directory or the host compile-artifact directory")
    work_root = output_path(work_dir or catalog.root / "build/appimage/data-work", catalog.root)
    output = (host_output_dir.expanduser().resolve() if host_output_dir is not None else
              output_path(output_dir or catalog.root / "build/appimage/assembled", catalog.root))
    output.mkdir(parents=True, exist_ok=True)
    names = ["output.appimage", "output.assets.zip", "output.assembly.json",
             "output.assembly-build-info.txt", "output.assembly-diagnostics.zip"]
    if any(os.path.lexists(output / name) for name in names):
        raise ValueError(f"Preserving existing assembly artifacts in {output}")
    # Claim this output set without a filesystem lock. Other producers stop at
    # the exclusive report creation; the data writer also publishes exclusively.
    report_handle = (output / "output.assembly.json").open("x", encoding="utf-8")
    report = {"ok": False, "started_utc": now(),
              "assembly_method": "Installed Python/TeX and data writes; no uploaded executable is invoked"}
    work = None
    try:
        work_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="data-run-", dir=work_root))
        report["work_directory"] = str(work)
        report["phase"] = "verify immutable template"
        template = work / "template.appimage"
        config = restore_template(catalog, transport.expanduser().resolve(), template)
        info = config["template"]
        revision, native = config["source_revision"], info["native_revision"]
        report.update(source_revision=revision, native_revision=native,
                      template_sha256=info["template_sha256"], catalog_sha256=config["catalog_sha256"])
        report["phase"] = "prepare document inputs"
        supplied = json.loads(inputs.read_text()) if inputs else {}
        if not isinstance(supplied, dict):
            raise ValueError("Package inputs must map catalog IDs to available theme editions")
        selected = {}
        sources = []
        assets = work / "assets"
        for deck in catalog.select(config["selection"]):
            if deck.id in supplied:
                selected[deck.id] = supplied[deck.id]
                continue
            paths = get_paths(deck, work / "authoring" / deck.id, assets / deck.id)
            build_deck(deck, list(THEMES), ["live"], paths, copy_workspace=True, isolated_cache=True)
            selected[deck.id] = {
                theme: {"pdf": str(paths.pdf(theme, "live")),
                        **({"vectors": str(paths.pdf(theme, "live").with_suffix(".vector.json"))}
                           if deck.animation is not None else {})}
                for theme in THEMES
            }
            sources.append({"deck": deck.id, "source": str(deck.source.relative_to(catalog.root)),
                            "source_sha256": digest(deck.source), "method": "fresh TeX compilation"})
        rows, asset_files = collect_assets(catalog, assets, selection=config["selection"], inputs=selected)
        report["document_inputs"] = [
            {"path": str(source), "payload_path": relative.as_posix(), "sha256": digest(source)}
            for source, relative in asset_files]
        report["compiled_sources"] = sources
        payload = work / "payload" / PAYLOAD
        stage_assets(catalog, assets, payload, selection=config["selection"], inputs=selected)
        staged = sorted(p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file())
        identity = [f"native {native}\n", f"source {revision}\n", info["template_sha256"] + "\n"]
        identity += [f"{name} {digest(payload / name)}\n" for name in staged]
        bundle_id = hashlib.sha256("".join(identity).encode()).hexdigest()
        timestamp = now()
        for name, value in (("bundle-id", bundle_id), ("source-revision", revision), ("build-timestamp", timestamp)):
            write_new(payload / name, value + "\n")
        details = [f"Source revision: {revision}", f"Build timestamp: {timestamp}",
                   f"Native repository: {info['native_repository']}", f"Native revision: {native}",
                   "Architecture: x86_64", "Build OS: data-only assembly",
                   f"Runtime build OS: {info['runtime_build_os']}",
                   f"Runtime source revision: {info['runtime_source_revision']}",
                   f"Highest required GLIBC: {info['highest_required_glibc']}",
                   f"Bundle ID: {bundle_id}", f"Template SHA256: {info['template_sha256']}",
                   f"Native binary SHA256: {info['native_binary_sha256']}",
                   f"Catalog selections: {len(rows)}", "", "Bundled inputs:",
                   *(f"{digest(payload / name)}  {name}" for name in staged)]
        build_info = "\n".join(details) + "\n"
        write_new(payload / "build-info.txt", build_info)
        write_new(output / "output.assembly-build-info.txt", build_info)
        data_root = work / "payload"
        files = {p.relative_to(data_root).as_posix(): p for p in data_root.rglob("*") if p.is_file()}
        with zipfile.ZipFile(output / "output.assets.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in sorted(files.items()):
                archive.write(path, name)
        report["phase"] = "append data to immutable template"
        result = append(template, files, output / "output.appimage",
                        json.loads((HERE / "payload-policy.json").read_text()),
                        {"source_revision": revision, "native_revision": native,
                         "template_sha256": info["template_sha256"], "bundle_id": bundle_id,
                         "build_timestamp": timestamp, "package_id": config["package_id"]})
        if result["template_sha256"] != info["template_sha256"]:
            raise ValueError("Template changed during data assembly")
        report.update(ok=True, phase="complete", bundle_id=bundle_id, **result)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc()
    finally:
        report["finished_utc"] = now()
        try:
            with zipfile.ZipFile(output / "output.assembly-diagnostics.zip", "x",
                                 compression=zipfile.ZIP_DEFLATED) as archive:
                if work is not None:
                    for path in work.rglob("*"):
                        if path.is_file() and (path.suffix == ".log" or re.fullmatch(r"pass-\d+\.txt", path.name)):
                            archive.write(path, path.relative_to(work))
        finally:
            with report_handle:
                report_handle.write(json.dumps(report, indent=2) + "\n")
                report_handle.flush()
                os.fsync(report_handle.fileno())
    return report
