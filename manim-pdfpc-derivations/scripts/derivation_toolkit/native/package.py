"""Build a portable Focal AppImage from already compiled project documents."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

from ..catalog import Catalog, PACKAGE_ROOT
from ..package_assets import collect_assets
from ..paths import get_paths, output_path
from .build import native_pin


def package_identity(catalog):
    identity = catalog.package.get("id", "derivation-presentations")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", identity):
        raise ValueError("package.id must be a lowercase application identifier")
    return identity


def provenance_revision(catalog, requested=None):
    revision = requested
    if revision is None:
        result = subprocess.run(["git", "-C", str(catalog.root), "rev-parse", "--verify", "HEAD"],
                                capture_output=True, text=True)
        revision = result.stdout.strip() if result.returncode == 0 else "unversioned"
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", revision):
        raise ValueError("The source revision must be a single filename-safe provenance label")
    return revision


def build_package(catalog: Catalog, *, assets: Path | None = None, work_dir: Path | None = None,
                  output: Path | None = None, cache_root: Path | None = None,
                  selection="all", inputs: Path | None = None, source_revision: str | None = None) -> Path:
    import json
    paths = get_paths(catalog.get(), cache_root)
    identity = package_identity(catalog)
    assets = (assets or paths.cache_root / "decks").expanduser().resolve()
    work_dir = output_path(work_dir or paths.cache_root / "appimage/work", catalog.root)
    output = output_path(output or paths.cache_root / "appimage" / f"{identity}.appimage", catalog.root)
    output_path(output.parent, catalog.root)
    if output.suffix.lower() != ".appimage":
        raise ValueError("The package output must have an .appimage suffix")
    collect_assets(catalog, assets, selection=selection,
                   inputs=json.loads(inputs.read_text()) if inputs else None)
    source_revision = provenance_revision(catalog, source_revision)
    repository, revision = native_pin(catalog)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PACKAGE_ROOT.parent) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = ["bash", str(PACKAGE_ROOT / "native/build-appimage.sh"), str(catalog.root),
               str(assets), str(work_dir), str(output), identity, repository, revision,
               source_revision, selection, str(inputs.resolve()) if inputs else ""]
    subprocess.run(command, check=True, env=environment)
    return output
