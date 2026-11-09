#!/usr/bin/env python3
"""Author, build and present the documents registered in a project catalog."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from .catalog import Catalog, MODES, THEMES
from .paths import get_paths, output_path


def _build_options(parser, *, modes=True):
    parser.add_argument("deck", nargs="?", help="Catalog deck ID; build accepts all")
    parser.add_argument("--theme", choices=(*THEMES, "both"), default="light")
    if modes:
        parser.add_argument("--mode", choices=(*MODES, "all"), default="all")
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--output-dir", type=Path)


def _paths(deck, args, multiple=False):
    output = args.output_dir or os.environ.get("DERIVATION_BUILD_DIR")
    if output and multiple:
        output = Path(output) / deck.id
    return get_paths(deck, args.cache_root, output)


def main(argv=None, *, default_project=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pdfpc_args = []
    if "--" in argv:
        separator = argv.index("--")
        pdfpc_args, argv = argv[separator + 1:], argv[:separator]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=default_project,
                        help="Directory containing presentations.json")
    commands = parser.add_subparsers(dest="command")

    listing = commands.add_parser("list", help="Show catalog IDs, languages and source files")
    listing.add_argument("--json", action="store_true", help="Print the maintained catalog as JSON")

    building = commands.add_parser("build", help="Build live, stepped and handout PDFs")
    _build_options(building)
    building.add_argument("--regenerate", action="store_true", help="Export registered scenes into the build directory first")
    building.add_argument("--fetch-tex", action="store_true", help="Fetch missing TeX packages into the build directory")
    building.add_argument("--assets-dir", type=Path, help="Use scene assets from this directory")
    queries = building.add_mutually_exclusive_group()
    queries.add_argument("--print-pdf", action="store_true", help="Print selected PDF paths without building")
    queries.add_argument("--print-output-dir", action="store_true", help="Print PDF directories without building")

    exporting = commands.add_parser("export", help="Export registered scenes and matching vector PDF figures")
    exporting.add_argument("deck", nargs="?", help="Catalog deck ID or all animated decks")
    exporting.add_argument("--theme", choices=(*THEMES, "both"), default="both")
    exporting.add_argument("--cache-root", type=Path)
    exporting.add_argument("--output-dir", "--output", type=Path, dest="output_dir")
    exporting.add_argument("--scene", help="Export one registered scene")
    exporting.add_argument("--sample-rate", type=int, default=30, help="Manim path samples per second, from 10 to 120")
    exporting.add_argument("--previews", action="store_true")
    exporting.add_argument("--preview-output", type=Path)
    exporting.add_argument("--in-authoring-environment", action="store_true", help=argparse.SUPPRESS)

    from .web.build import add_arguments
    html = commands.add_parser("html", help="Package compiled pages and vectors into a self-contained HTML file")
    add_arguments(html)

    native = commands.add_parser("native", help="Build the pinned pdfpc viewer or headless vector probe")
    native.add_argument("--cache-root", type=Path)
    native.add_argument("--probe", action="store_true")
    native.add_argument("--source", action="store_true")

    packaging = commands.add_parser("package", help="Build the portable native AppImage in the Focal builder")
    packaging.add_argument("deck", nargs="?", default="all")
    packaging.add_argument("--assets", type=Path)
    packaging.add_argument("--inputs", type=Path, help="Explicit per-deck/per-theme compiled PDF/vector inputs")
    packaging.add_argument("--cache-root", type=Path)
    packaging.add_argument("--work-dir", type=Path)
    packaging.add_argument("--output", type=Path)
    packaging.add_argument("--source-revision")

    verifying = commands.add_parser("verify", help="Check page bindings and selectable glyph bounds")
    _build_options(verifying)
    verifying.add_argument("--pdf", type=Path)
    verifying.add_argument("--vectors", type=Path)
    verifying.add_argument("--page-map", type=Path)
    verifying.add_argument("--assets", type=Path)

    running = commands.add_parser("run", help="Build and open a live deck in the native viewer")
    running.add_argument("deck", nargs="?")
    running.add_argument("--theme", choices=THEMES, default="light")
    running.add_argument("--dark", dest="theme", action="store_const", const="dark")
    running.add_argument("--light", dest="theme", action="store_const", const="light")
    running.add_argument("--cache-root", type=Path)
    running.add_argument("--output-dir", type=Path)
    running.add_argument("--print-pdf", action="store_true", help="Print the selected path without building or opening windows")
    running.add_argument("--pdf", type=Path, help="Explicit already compiled document")
    running.add_argument("--vectors", type=Path, help="Matching explicit timeline")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if pdfpc_args and args.command != "run":
        parser.error("Arguments after -- are supported by run")
    catalog = Catalog(args.project)
    if args.command == "list":
        if args.json:
            print(json.dumps(catalog.data, ensure_ascii=False, indent=2))
        else:
            for deck in catalog.decks.values():
                details = [deck.language]
                if deck.archived:
                    details.append("archive")
                if deck.animation is not None:
                    details.append("animated")
                default = " [default]" if deck.id == catalog.default else ""
                print(f"{deck.id}{default}: {deck.title} ({', '.join(details)})")
                print(f"  {deck.source.relative_to(catalog.root)}")
        return 0
    if args.command == "native":
        from .native.build import prepare_native
        source, build = prepare_native(catalog, get_paths(catalog.get(), args.cache_root), probe=args.probe)
        print(source if args.source else build / "bin" / ("vector-probe" if args.probe else "pdfpc"))
        return 0
    if args.command == "package":
        from .native.package import build_package
        print(build_package(catalog, assets=args.assets, work_dir=args.work_dir, output=args.output,
                            cache_root=args.cache_root, selection=args.deck, inputs=args.inputs,
                            source_revision=args.source_revision))
        return 0
    themes = list(THEMES) if args.theme == "both" else [args.theme]
    if args.command == "html":
        from .web.build import run
        run(args, catalog)
        return 0
    if args.command == "verify":
        from .verify.documents import verify_documents
        reports = []
        for deck in catalog.select(args.deck):
            reports.append(verify_documents(deck, _paths(deck, args, args.deck == "all"), themes,
                           list(MODES) if args.mode == "all" else [args.mode],
                           pdf=args.pdf, vectors=args.vectors, page_map=args.page_map, assets=args.assets))
        print(json.dumps({"ok": True, "decks": reports}, indent=2))
        return 0
    if args.command == "build":
        from .build import build_deck

        decks = catalog.select(args.deck)
        modes = list(MODES) if args.mode == "all" else [args.mode]
        failures = []
        for deck in decks:
            paths = _paths(deck, args, args.deck == "all")
            if args.print_output_dir:
                print(paths.build_dir)
            elif args.print_pdf:
                for theme in themes:
                    for mode in modes:
                        print(paths.pdf(theme, mode))
            else:
                assets = args.assets_dir
                if assets is not None and args.deck == "all":
                    assets = assets / deck.id
                try:
                    build_deck(deck, themes, modes, paths, regenerate=args.regenerate,
                               fetch_tex=args.fetch_tex, assets_dir=assets)
                except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
                    if args.deck != "all":
                        raise
                    failures.append(deck.id)
                    print(f"Failed {deck.id}: {error}", file=sys.stderr, flush=True)
        if failures:
            print(f"Build failed for {len(failures)} of {len(decks)} decks: "
                  + ", ".join(failures), file=sys.stderr)
            return 1
        return 0
    if args.command == "export":
        from .build import tex_environment
        from .export import export_deck, run_export

        decks = catalog.select(args.deck)
        if args.deck == "all":
            decks = [deck for deck in decks if deck.animation is not None]
        for deck in decks:
            if deck.animation is None:
                parser.error(f"{deck.id} has no registered animation provider")
            paths = get_paths(deck, args.cache_root)
            output = args.output_dir.resolve() if args.output_dir else paths.assets_dir
            if args.output_dir and args.deck == "all":
                output /= deck.id
            if output != deck.animation.assets:
                output_path(output, deck.repository_root)
            preview = output_path(args.preview_output, deck.repository_root) if args.preview_output else paths.preview_dir
            if args.in_authoring_environment:
                export_deck(deck, themes, paths, output, scene_id=args.scene,
                            sample_rate=args.sample_rate, preview_output=preview if args.previews else None)
            else:
                run_export(deck, themes, paths, output, scene_id=args.scene,
                           sample_rate=args.sample_rate, previews=args.previews,
                           preview_output=preview, environment=tex_environment(paths))
        return 0
    if args.command == "run":
        from .launch import run

        deck = catalog.get(args.deck)
        paths = _paths(deck, args)
        if args.print_pdf:
            print(paths.pdf(args.theme, "live"))
        else:
            run(deck, args.theme, paths, pdfpc_args, pdf=args.pdf, vectors=args.vectors)
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
