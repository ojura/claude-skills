#!/usr/bin/env python3
"""Embed the authored panels in an isolated copy of the real toaster report."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import subprocess
import tarfile


def git(directory, *arguments, data=None):
    return subprocess.check_output(["git", "-C", str(directory), *arguments], input=data)


def snapshot(directory: Path, paths: list[str], message: str, *, parent=None, expected_tree=None):
    """Keep the unchanged report checker's tracked-path checks inside this copy."""
    git(directory, "add", "-f", "--", *paths)
    tree = git(directory, "write-tree").decode().strip()
    if expected_tree is not None and tree != expected_tree:
        raise ValueError(f"The copied report tree differs from its committed source: {tree}")
    command = ["-c", "user.name=Derivation fixture", "-c", "user.email=derivation-fixture@local",
               "commit-tree", tree]
    if parent is not None:
        command += ["-p", parent]
    revision = git(directory, *command, data=(message + "\n").encode()).decode().strip()
    git(directory, "update-ref", "--no-deref", "HEAD", revision)
    if git(directory, "for-each-ref", "--format=%(refname)", "refs/heads").strip():
        raise ValueError("The isolated report snapshot must have no named branches")
    return {"revision": revision, "tree": tree}


def prepare(source_skill: Path, project: Path):
    source_skill = source_skill.expanduser().resolve()
    project = project.expanduser().resolve()
    if not (project / "presentations.json").is_file():
        raise ValueError("Install the toaster example in the selected project first")
    repository = Path(git(source_skill, "rev-parse", "--show-toplevel").decode().strip())
    relative = source_skill.relative_to(repository).as_posix()
    revision = git(repository, "rev-parse", "HEAD").decode().strip()
    tree = git(repository, "rev-parse", f"{revision}:{relative}").decode().strip()
    destination = project / "build/report-source"
    if destination.is_symlink() or destination.exists():
        raise FileExistsError(f"The report workspace already exists: {destination}")
    archive = git(repository, "archive", "--format=tar", f"{revision}:{relative}")
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        for member in contents:
            name = PurePosixPath(member.name)
            if name.is_absolute() or any(part in ("", ".", "..") for part in name.parts):
                raise ValueError(f"Invalid report-source path: {member.name}")
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(contents.extractfile(member).read())
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError(f"The report fixture requires a regular file: {member.name}")

    git(destination, "init", "--quiet")
    baseline = snapshot(destination, ["."], f"Snapshot report source from {revision}",
                        expected_tree=tree)
    source_report = destination / "test/REPORT.md"
    original = source_report.read_text(encoding="utf-8")
    anchor = "The BEADS v2.1 analysis scripts are available at"
    if original.count(anchor) != 1:
        raise ValueError("The report's statistical-methods insertion point has changed")
    ratios = json.loads((project / "trials.json").read_text())["ratios"]
    if len(ratios) != 2 or any(not math.isfinite(value) or value <= 0 for value in ratios):
        raise ValueError("This report illustration needs two finite positive ratios")
    product, speedup = math.prod(ratios), math.sqrt(math.prod(ratios))
    addition = (
        f"Figure\\ \\ref{{fig:geometric-mean-derivation}} illustrates the calculation "
        f"with ratios ${ratios[0]:g}$ and ${ratios[1]:g}$. Their product is ${product:g}$, "
        f"the same as two equal factors of ${speedup:g}$, so $S={speedup:g}$.\n\n"
        "```{=latex}\n"
        "\\begin{figure}[H]\n"
        "\\centering\n"
        "\\DerivationHandout{geometric-mean}\n"
        "\\caption{The geometric mean of two illustrative trial ratios. "
        "The panels connect the ratios, their product, an equal per-trial factor and the general formula.}\n"
        "\\label{fig:geometric-mean-derivation}\n"
        "\\end{figure}\n"
        "```\n\n"
    )
    report = destination / "test/REPORT-derivations.md"
    report.write_text(original.replace(anchor, addition + anchor), encoding="utf-8")
    toolkit = project / "tools/vendor/manim_pdfpc/derivation_toolkit"
    assets = project / "build/decks/toaster-demo/animations"
    header = (
        "\\def\\DerivationTheme{light}\n"
        "\\def\\DerivationPalettePrefix{toaster}\n"
        f"\\edef\\DerivationPaletteFile{{\\detokenize{{{project / 'palette.tex'}}}}}\n"
        f"\\edef\\DerivationAssets{{\\detokenize{{{assets}}}}}\n"
        f"\\input{{{toolkit / 'tex/theme.tex'}}}\n"
        f"\\input{{{toolkit / 'tex/derivations.tex'}}}\n"
    )
    (destination / "derivation-header.tex").write_text(header, encoding="utf-8")
    output_dir = project / "build/report"
    output_dir.mkdir(parents=True, exist_ok=True)
    makefile = (
        "REPORT := test/REPORT-derivations.md\n"
        "BASE := $(basename $(notdir $(REPORT)))\n"
        f"CHECK_DIR := {output_dir / 'check'}\n"
        f"OUT := {output_dir / 'REPORT-derivations.pdf'}\n"
        "PANDOC_FLAGS += --include-in-header=derivation-header.tex\n"
        ".PHONY: checked-report\n"
        "checked-report: check\n"
        '\tcp "$(CHECK_DIR)/$(BASE).pdf" "$(OUT)"\n'
    )
    (destination / "fixture.mk").write_text(makefile, encoding="utf-8")
    derived = snapshot(destination, ["test/REPORT-derivations.md", "derivation-header.tex", "fixture.mk"],
                       "Embed the geometric-mean derivation in the report fixture",
                       parent=baseline["revision"])
    receipt = {
        "source_revision": revision,
        "source_tree": tree,
        "baseline_snapshot": baseline,
        "derived_snapshot": derived,
        "source_report_sha256": hashlib.sha256(source_report.read_bytes()).hexdigest(),
        "report": str(report),
        "workspace": str(destination),
        "output": str(output_dir / "REPORT-derivations.pdf"),
        "command": ["make", "-C", str(destination), "-f", "Makefile", "-f", "fixture.mk", "checked-report"],
    }
    (output_dir / "preparation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def page_map(pdf: Path, output: Path):
    import fitz

    pdf = pdf.expanduser().resolve()
    with fitz.open(pdf) as document:
        pages = []
        for index, page in enumerate(document, 1):
            lines = [line.strip() for line in page.get_text().splitlines() if line.strip()]
            pages.append({"number": index, "logical": index,
                          "title": lines[0] if lines else f"Page {index}"})
        result = {"version": 1, "logicalSlides": len(document), "pages": pages}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return {"pdf": str(pdf), "page_map": str(output), "pages": result["logicalSlides"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--source-skill", type=Path, required=True)
    preparing.add_argument("--project", type=Path, default=Path.cwd())
    mapping = commands.add_parser("page-map")
    mapping.add_argument("--pdf", type=Path, required=True)
    mapping.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = (prepare(arguments.source_skill, arguments.project) if arguments.command == "prepare"
              else page_map(arguments.pdf, arguments.output))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
