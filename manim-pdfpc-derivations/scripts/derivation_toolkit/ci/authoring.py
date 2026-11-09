"""Source/export and artifact gates for the reusable project workflow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from ..catalog import Catalog, MODES, THEMES
from ..package_assets import collect_assets, explicit_editions, validate_input_map
from ..paths import get_paths


def mounted_inputs(path: Path | None, assets: Path) -> dict:
    """Relocate the workflow's /assets input paths into a downloaded artifact tree."""
    inputs = validate_input_map(json.loads(path.read_text()) if path else {}) or {}
    for editions in inputs.values():
        for edition in editions.values():
            for key in ("pdf", "vectors", "page_map"):
                if not edition.get(key):
                    continue
                source = Path(edition[key])
                try:
                    relative = source.relative_to("/assets")
                except ValueError:
                    continue
                edition[key] = str(assets / relative)
    return inputs


def compiled_inputs(catalog: Catalog, assets: Path, inputs: dict) -> dict:
    """Name every already-built native payload so assembly never starts a TeX build."""
    rows, files = collect_assets(catalog, assets, inputs=inputs)
    sources = {relative.as_posix(): str(source) for source, relative in files}
    complete = {}
    for row in rows:
        identifier, stem, _language, _archived, animated, themes, _title = row.split("\t")
        complete[identifier] = {}
        for theme in themes.split(","):
            prefix = f"decks/{identifier}/{stem}-{theme}-live"
            edition = {"pdf": sources[prefix + ".pdf"]}
            if animated == "1":
                edition["vectors"] = sources[prefix + ".vector.json"]
            complete[identifier][theme] = edition
    return complete


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("source", "artifacts", "html", "package-inputs"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--deck")
    args = parser.parse_args(argv)
    catalog = Catalog(args.project)
    if args.phase != "html":
        args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.phase == "source":
        from ..verify.verify_exports import main as verify_exports

        for deck in catalog.decks.values():
            if deck.animation is not None:
                code = verify_exports(["--project", str(catalog.root), deck.id,
                                       "--output-dir", str(args.output_dir / deck.id)])
                if code:
                    return code
        return 0
    if args.assets is None:
        parser.error(f"{args.phase} requires --assets")
    inputs = mounted_inputs(args.inputs, args.assets)
    if args.phase == "html":
        from ..web.build import build_bundle

        deck = catalog.get(args.deck)
        build_bundle(catalog, deck, output=args.output_dir,
                     pdf_dir=args.assets / deck.id, inputs=inputs)
        return 0
    if args.phase == "package-inputs":
        target = args.output_dir / "compiled-inputs.json"
        target.write_text(json.dumps(compiled_inputs(catalog, args.assets, inputs), indent=2) + "\n")
        print(target)
        return 0
    from ..verify.documents import verify_documents
    from ..verify.runtime import prepare

    reports = []
    for deck in catalog.decks.values():
        paths = get_paths(deck, build_dir=args.assets / deck.id)
        external = explicit_editions(catalog, deck, inputs)
        if external is None:
            reports.append(verify_documents(deck, paths, THEMES, MODES,
                                           assets=deck.animation.assets if deck.animation else None))
        else:
            for theme, edition in external.items():
                reports.append(verify_documents(
                    deck, paths, [theme], ["live"], pdf=edition["pdf"],
                    vectors=edition["vectors"], page_map=edition["page_map"]))
    (args.output_dir / "documents.json").write_text(json.dumps(reports, indent=2) + "\n")
    runtime_inputs = None
    if args.inputs:
        runtime_inputs = args.output_dir / "resolved-inputs.json"
        runtime_inputs.write_text(json.dumps(inputs, indent=2) + "\n")
    return prepare(SimpleNamespace(project=catalog.root, assets=args.assets, inputs=runtime_inputs,
                                   output=args.output_dir / "runtime-fixtures.json"))


if __name__ == "__main__":
    raise SystemExit(main())
