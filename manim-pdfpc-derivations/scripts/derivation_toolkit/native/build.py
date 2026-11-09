"""Build the pinned native player without importing an author's scene runtime."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from ..catalog import Catalog
from ..paths import CachePaths, get_paths, output_path

NATIVE_ROOT = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = "https://github.com/ojura/pdfpc.git"


def native_pin(catalog: Catalog) -> tuple[str, str]:
    repository = catalog.native.get("repository", DEFAULT_REPOSITORY)
    revision = catalog.native.get("revision", (NATIVE_ROOT / "revision.txt").read_text().strip())
    if not isinstance(repository, str) or not repository or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("native.repository and native.revision must identify an exact Git source")
    return repository, revision


def prepare_native(catalog: Catalog, paths: CachePaths, *, probe=False,
                   work_dir: Path | None = None, package=False) -> tuple[Path, Path]:
    repository, revision = native_pin(catalog)
    root = output_path(work_dir, catalog.root) if work_dir else paths.native_root
    for command in ("git", "cmake", "ninja", "valac", "pkg-config"):
        if not shutil.which(command):
            raise RuntimeError(f"Missing native build command: {command}")
    source_override = os.environ.get("DERIVATION_NATIVE_SOURCE")
    source = Path(source_override).expanduser().resolve() if source_override else root / f"pdfpc-{revision}"
    build = root / f"build-{revision}{'-package' if package else '-probe' if probe else ''}"
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"build-{revision}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        def run(*args):
            subprocess.run(list(args), check=True, stdout=sys.stderr)
        existing = (source / ".git").exists()
        if not existing:
            if source.exists():
                raise ValueError(f"Preserving non-Git native source destination: {source}")
            run("git", "clone", "--quiet", "--no-checkout", repository, str(source))
        status = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain",
                                          "--untracked-files=normal"], text=True) if existing else ""
        if status:
            raise ValueError(f"Preserving locally edited native source: {source}")
        available = subprocess.run(["git", "-C", str(source), "cat-file", "-e", revision + "^{commit}"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not available:
            run("git", "-C", str(source), "fetch", "--quiet", "--depth", "1", repository, revision)
        run("git", "-C", str(source), "checkout", "--quiet", "--detach", revision)
        options = ["-DCMAKE_BUILD_TYPE=Release", f"-DBUILD_TESTING={'ON' if probe else 'OFF'}",
                   "-DMDVIEW=OFF", "-DREST=OFF", "-DMOVIES=OFF"]
        if package:
            options += ["-DCMAKE_INSTALL_PREFIX=/usr", "-DCMAKE_INSTALL_SYSCONFDIR=/etc"]
        run("cmake", "-S", str(source), "-B", str(build), "-G", "Ninja", *options)
        run("cmake", "--build", str(build), "--parallel", os.environ.get("DERIVATION_JOBS", "4"))
    return source, build


def build_viewer(catalog: Catalog, paths: CachePaths, *, probe=False) -> Path:
    _, build = prepare_native(catalog, paths, probe=probe)
    executable = build / "bin" / ("vector-probe" if probe else "pdfpc")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"Native build produced no executable: {executable}")
    return executable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--prepare-package", action="store_true")
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args(argv)
    catalog = Catalog(args.project)
    source, build = prepare_native(catalog, get_paths(catalog.get(), args.cache_root),
                                   probe=args.probe, work_dir=args.work_dir, package=args.prepare_package)
    if args.prepare_package:
        print(json.dumps({"source": str(source), "build": str(build)}))
    else:
        print(source if args.source else build / "bin" / ("vector-probe" if args.probe else "pdfpc"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
