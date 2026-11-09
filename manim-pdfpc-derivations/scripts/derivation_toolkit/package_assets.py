"""Validate and stage project PDFs and optional timelines for a native package."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil

from .catalog import Catalog, THEMES


def input_path(catalog: Catalog, value: str | Path) -> Path:
    """Resolve paths stored in the project input map against its project root."""
    return (catalog.root / Path(value).expanduser()).resolve()


def collect_assets(catalog: Catalog, assets: Path, *, selection="all", inputs: dict | None = None):
    """Explicit inputs map deck IDs to theme PDF/vector paths for external documents."""
    rows, files = [], []
    for deck in catalog.select(selection):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", deck.document):
            raise ValueError(f"Unsupported PDF filename: {deck.document}")
        if any(character in deck.title for character in "\t\r\n"):
            raise ValueError(f"Unsupported title in package catalog: {deck.id}")
        external = (inputs or {}).get(deck.id)
        themes = [theme for theme in THEMES if theme in external] if external is not None else list(THEMES)
        if not themes or external is not None and set(external) - set(THEMES):
            raise ValueError(f"Unsupported or empty package themes: {deck.id}")
        animation_flags = []
        for theme in themes:
            filename = f"{deck.document}-{theme}-live.pdf"
            edition = external.get(theme) if external else None
            if external is not None and not edition:
                raise ValueError(f"Missing package input for {deck.id}/{theme}")
            pdf = input_path(catalog, edition["pdf"]) if edition else assets / deck.id / filename
            vector = (input_path(catalog, edition["vectors"]) if edition.get("vectors") else None) if edition else (
                pdf.with_suffix(".vector.json") if deck.animation else None)
            animation_flags.append(vector is not None)
            for source, name in ((pdf, filename), (vector, Path(filename).with_suffix(".vector.json").name)):
                if source is None:
                    continue
                if not source.is_file() or source.stat().st_size == 0:
                    raise ValueError(f"Missing built presentation asset: {source}")
                files.append((source, Path("decks") / deck.id / name))
        if len(set(animation_flags)) != 1:
            raise ValueError(f"Package themes must agree about timeline availability: {deck.id}")
        rows.append("\t".join((deck.id, deck.document, deck.language,
                               str(int(deck.archived)), str(int(animation_flags[0])), ",".join(themes), deck.title)))
    return rows, files


def stage_assets(catalog: Catalog, assets: Path, destination: Path | None = None,
                 *, selection="all", inputs: dict | None = None):
    rows, files = collect_assets(catalog, assets, selection=selection, inputs=inputs)
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
        for source, relative in files:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o644)
        default = catalog.default if selection == "all" else catalog.get(selection).id
        (destination / "decks.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (destination / "default-deck").write_text(default + "\n", encoding="utf-8")
        (destination / "project-title").write_text(catalog.title.replace("\n", " ") + "\n", encoding="utf-8")
        (destination / "catalog.json").write_text(json.dumps(
            {"version": 1, "project": catalog.project, "default": default,
             "decks": [{"id": deck.id, "metadata": deck.metadata} for deck in catalog.select(selection)]},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"decks": len(rows), "assets": len(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--deck", default="all")
    parser.add_argument("--inputs", type=Path, help="Explicit per-deck/per-theme PDF and vector paths")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    inputs = json.loads(args.inputs.read_text()) if args.inputs else None
    print(json.dumps(stage_assets(Catalog(args.project), args.assets.resolve(), args.destination,
                                 selection=args.deck, inputs=inputs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
