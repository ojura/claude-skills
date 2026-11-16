#!/usr/bin/env python3
"""Prepare report sources on the host, then build the toaster consumer offline."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import prepare_report


def prepare(cache: Path, repository: str, revision: str) -> Path:
    """Fetch a declared report commit before the workflow disables networking."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("The report revision must be a full lowercase Git commit SHA")
    destination = cache.expanduser().resolve() / "report-repository"
    destination.mkdir(parents=True, exist_ok=False)
    subprocess.run(["git", "init", "--quiet", str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "fetch", "--depth=1", repository, revision], check=True)
    subprocess.run(["git", "-C", str(destination), "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
    actual = prepare_report.git(destination, "rev-parse", "HEAD").decode().strip()
    if actual != revision:
        raise ValueError(f"Fetched report revision {actual} differs from {revision}")
    if not (destination / "markdown-latex-report/test/REPORT.md").is_file():
        raise ValueError("The selected revision has no markdown-latex-report/test/REPORT.md")
    print(f"Prepared report source {revision} in {destination}", flush=True)
    return destination


def stage_project(source: Path, destination: Path) -> Path:
    """Keep the checked-out consumer read-only while its report tools write files."""
    source = source.expanduser().resolve()
    destination = destination.expanduser()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"The report project already exists: {destination}")
    destination = destination.resolve()
    if destination == source or source in destination.parents:
        raise ValueError("The writable report project must be outside the source checkout")
    for name in ("presentations.json", "tools/derivations.py", "scene.py", "trials.json"):
        if not (source / name).is_file():
            raise FileNotFoundError(f"Missing consumer input: {source / name}")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "build", "__pycache__", "*.pyc"))
    return destination


def build(source: Path, cache: Path, assets: Path) -> dict:
    """Run the original report checker and publish PDFs through the common map."""
    cache, assets = cache.expanduser().resolve(), assets.expanduser().resolve()
    source = source.expanduser().resolve()
    if assets == source or source in assets.parents:
        raise ValueError("CI artifacts must be outside the source checkout")
    report_skill = cache / "report-repository/markdown-latex-report"
    if not (report_skill / "test/REPORT.md").is_file():
        raise FileNotFoundError("Run the host prepare command before the offline report build")
    project = stage_project(source, cache / "toaster-project")
    build_root = project / "build"
    runtime = build_root / "runtime"
    environment = dict(os.environ, DERIVATION_CACHE_ROOT=str(build_root), DERIVATION_BUILD_DIR="")
    for key, name in (("HOME", "home"), ("XDG_CACHE_HOME", "xdg"),
                      ("MPLCONFIGDIR", "matplotlib"), ("TEXMFVAR", "texmf-var"),
                      ("TEXMFCONFIG", "texmf-config"), ("TEXMFCACHE", "texmf-cache")):
        path = runtime / name
        path.mkdir(parents=True, exist_ok=True)
        environment[key] = str(path)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    entry = project / "tools/derivations.py"
    for deck in ("toaster-demo", "toaster-static"):
        command = [sys.executable, "-B", str(entry), "build", deck,
                   "--theme", "both", "--mode", "all", "--output-dir", str(assets / deck)]
        if deck == "toaster-demo":
            command.append("--regenerate")
        subprocess.run(command, cwd=project, env=environment, check=True)
    report = prepare_report.prepare(report_skill, project)
    subprocess.run(report["command"], cwd=project, env=environment, check=True)
    destination = assets / "article"
    destination.mkdir(parents=True, exist_ok=True)
    pdf = destination / "REPORT-derivations.pdf"
    shutil.copy2(report["output"], pdf)
    page_map = pdf.with_suffix(".presentation.json")
    prepare_report.page_map(pdf, page_map)
    inputs = {"article": {"light": {"pdf": str(pdf), "page_map": str(page_map)}}}
    (assets / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    print(f"Checked report and twelve companion editions are in {assets}", flush=True)
    return inputs


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="phase", required=True)
    preparing = commands.add_parser("prepare", help="Fetch report sources before container execution")
    preparing.add_argument("--report-revision", required=True)
    preparing.add_argument("--report-repository", default="https://github.com/ojura/claude-skills.git")
    runner = os.environ.get("RUNNER_TEMP")
    preparing.add_argument("--cache-root", type=Path,
                           default=Path(runner) / "derivation-cache" if runner else None)
    building = commands.add_parser("build", help="Build the report and companion editions offline")
    building.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    building.add_argument("--cache-root", type=Path, default=Path("/cache"))
    building.add_argument("--assets", type=Path, default=Path("/assets"))
    args = parser.parse_args(argv)
    if args.phase == "prepare":
        if args.cache_root is None:
            parser.error("prepare requires --cache-root or the workflow's RUNNER_TEMP")
        prepare(args.cache_root, args.report_repository, args.report_revision)
    else:
        build(args.project, args.cache_root, args.assets)


if __name__ == "__main__":
    main()
