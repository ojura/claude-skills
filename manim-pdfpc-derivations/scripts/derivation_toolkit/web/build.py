"""Package compiled PDFs and their vector timelines as a self-contained HTML presentation."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

from .bundle import embed
from ..catalog import Catalog, Deck
from ..paths import get_paths, output_path

WEB = Path(__file__).resolve().parent
PDFJS_VERSION = "5.1.91"
COPY_SOURCE = ("index.html", "player.mjs", "player.css", "vector.mjs", "resources.mjs")
MARKER = ".derivation-html5"


def source_revision(root: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unversioned"


def page_mapping(pdf_path: Path, mapping: Path | dict | None, count: int) -> tuple[list[dict], int]:
    """Use explicit page metadata, a builder sidecar, or physical page order."""
    if mapping is None:
        sidecar = pdf_path.with_suffix(".presentation.json")
        mapping = sidecar if sidecar.is_file() else None
    if isinstance(mapping, Path):
        mapping = json.loads(mapping.read_text(encoding="utf-8"))
    if mapping is None:
        return [{"number": page, "logical": page} for page in range(1, count + 1)], count
    if mapping.get("version") != 1:
        raise ValueError("Unsupported presentation page-map version")
    pages = mapping["pages"]
    if len(pages) != count or [page["number"] for page in pages] != list(range(1, count + 1)):
        raise ValueError("The page map must describe each compiled PDF page in physical order")
    logical = [page["logical"] for page in pages]
    if any(type(number) is not int or number < 1 for number in logical):
        raise ValueError("Logical page numbers must be positive integers")
    ordered = list(dict.fromkeys(logical))
    if logical != sorted(logical) or ordered != list(range(1, len(ordered) + 1)):
        raise ValueError("Logical pages must retain their authored order")
    if mapping.get("logicalSlides", len(ordered)) != len(ordered):
        raise ValueError("The declared logical page count differs from its page map")
    return pages, len(ordered)


def pdf_manifest(pdf_path: Path, vector_path: Path | None, theme: str,
                 page_map: Path | dict | None = None) -> dict:
    import fitz
    timeline = (json.loads(vector_path.read_text(encoding="utf-8")) if vector_path is not None
                else {"version": 1, "scenes": []})
    if timeline.get("version") != 1:
        raise ValueError("Unsupported vector timeline version")
    scenes = {scene["id"]: scene for scene in timeline["scenes"]}
    if len(scenes) != len(timeline["scenes"]):
        raise ValueError("Timeline scene IDs must be unique")
    pages = []
    with fitz.open(pdf_path) as document:
        mapping, logical_count = page_mapping(pdf_path, page_map, len(document))
        for page, mapped in zip(document, mapping):
            name, area = None, None
            for link in page.get_links():
                kind, uri = document.xref_get_key(link["xref"], "A/URI")
                if kind != "string" or not uri.startswith("pdfpc-vector:"):
                    continue
                if name is not None:
                    raise ValueError(f"PDF page {page.number + 1} contains more than one animation region")
                name = uri.removeprefix("pdfpc-vector:")
                if name not in scenes:
                    raise ValueError("PDF links to an unknown authored scene: " + name)
                rectangle = link["from"]
                if rectangle.is_empty or not page.rect.contains(rectangle):
                    raise ValueError(f"Animation region leaves PDF page {page.number + 1}")
                area = [round(float(coordinate), 5) for coordinate in rectangle]
                scene = scenes[name]
                if abs(rectangle.width / rectangle.height - scene["width"] / scene["height"]) > .0002:
                    raise ValueError("Scene/PDF aspect mismatch: " + name)
            lines = page.get_text().splitlines()
            pages.append({
                "number": page.number + 1, "logical": mapped["logical"],
                "title": mapped.get("title") or (lines[0].strip() if lines else f"Page {page.number + 1}"),
                "width": float(page.rect.width), "height": float(page.rect.height),
                "scene": {"id": name, "rect": area} if name else None,
            })
    return {"pdf": f"assets/{theme}.pdf", "vectors": f"assets/{theme}.vector.json.gz",
            "pages": pages, "logicalSlides": logical_count}


def pack_source(root: Path, name: str) -> Path:
    archive = root.parent / f"{name}.zip"
    temporary = archive.with_name(archive.name + ".part")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for file in sorted(root.rglob("*")):
            if file.is_file():
                output.write(file, arcname=str(Path(name) / file.relative_to(root)))
    os.replace(temporary, archive)
    return archive


def build_bundle(catalog: Catalog, deck: Deck, *, output: Path | None = None,
                 pdf_dir: Path | None = None, pdfjs_dist: Path | None = None,
                 themes=("light", "dark"), pdf: Path | None = None,
                 vectors: Path | None = None, page_map: Path | None = None,
                 archive=True, cache_root: Path | None = None) -> Path:
    paths = get_paths(deck, cache_root)
    output = output_path(output or paths.build_dir / "html5", catalog.root)
    output_path(output.parent, catalog.root)
    pdf_dir = (pdf_dir or paths.build_dir).expanduser().resolve()
    tools = paths.cache_root / "tools/html5"
    vendor = (pdfjs_dist or tools / "node_modules/pdfjs-dist").expanduser().resolve()
    if not vendor.is_dir():
        raise ValueError(f"Install the pinned web/package-lock.json dependencies under {tools}, or pass --pdfjs-dist")
    version = json.loads((vendor / "package.json").read_text())["version"]
    if version != PDFJS_VERSION:
        raise ValueError(f"Expected pdfjs-dist {PDFJS_VERSION}; found {version}")
    distribution = deck.id + "-html5"
    manifest = {"version": 1, "title": deck.title, "language": deck.language,
                "revision": source_revision(catalog.root), "themes": {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not (output / MARKER).is_file():
        raise ValueError(f"Preserving an unrelated HTML output directory: {output}")
    with tempfile.TemporaryDirectory(prefix=".derivation-html5-", dir=output.parent) as temporary:
        stage = Path(temporary) / distribution
        (stage / "assets").mkdir(parents=True)
        (stage / "vendor").mkdir()
        for name in COPY_SOURCE:
            shutil.copyfile(WEB / name, stage / name)
        for source, destination in (
            ("build/pdf.min.mjs", "pdf.mjs"), ("build/pdf.worker.min.mjs", "pdf.worker.mjs"),
            ("LICENSE", "PDFJS-LICENSE.txt"),
        ):
            shutil.copyfile(vendor / source, stage / "vendor" / destination)
        for theme in themes:
            selected_pdf = pdf.resolve() if pdf is not None else pdf_dir / f"{deck.document}-{theme}-live.pdf"
            selected_vectors = vectors.resolve() if vectors is not None else selected_pdf.with_suffix(".vector.json")
            if not selected_vectors.is_file():
                if vectors is not None or (pdf is None and deck.animation is not None):
                    raise ValueError(f"Missing matching live timeline: {selected_vectors}")
                selected_vectors = None
            if not selected_pdf.is_file() or selected_pdf.stat().st_size == 0:
                raise ValueError(f"Missing compiled presentation PDF: {selected_pdf}")
            entry = pdf_manifest(selected_pdf, selected_vectors, theme, page_map)
            shutil.copyfile(selected_pdf, stage / entry["pdf"])
            timeline = (selected_vectors.read_bytes() if selected_vectors is not None
                        else b'{"version":1,"scenes":[]}\n')
            (stage / entry["vectors"]).write_bytes(gzip.compress(timeline, compresslevel=9, mtime=0))
            manifest["themes"][theme] = entry
        layouts = [[(page["logical"], page["scene"]["id"] if page["scene"] else None)
                    for page in entry["pages"]] for entry in manifest["themes"].values()]
        if any(layout != layouts[0] for layout in layouts[1:]):
            raise ValueError("Theme editions must preserve the authored page/scene order")
        manifest["logicalSlides"] = next(iter(manifest["themes"].values()))["logicalSlides"]
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        embed(stage)
        shutil.copyfile(stage / "vendor/PDFJS-LICENSE.txt", stage / "PDFJS-LICENSE.txt")
        for directory in ("assets", "vendor"):
            shutil.rmtree(stage / directory)
        for name in (*COPY_SOURCE[1:], "manifest.json"):
            (stage / name).unlink()
        (stage / MARKER).write_text(manifest["revision"] + "\n")
        (stage / "README.txt").write_text(
            deck.title + "\n\n"
            "Open index.html directly in a recent Chrome or Firefox.\n"
            "The HTML contains the PDF editions, vector timelines and PDF.js.\n"
            "Right/Space plays the next teaching segment, then advances pages.\n"
            "When blanked, Right/Space unblanks and advances; A/V preserves the blanked position.\n"
            "Left returns to the previous step; Ctrl+Left/Right seeks within a page.\n"
            "A pauses/resumes, V plays continuously, R restarts, E shows the end.\n"
            "F is fullscreen, B blanks and pauses, P shows presenter layout.\n"
            "Projector opens a synchronized audience window.\n"
            f"Contains Mozilla PDF.js {PDFJS_VERSION}, Apache 2.0; its license is included.\n",
            encoding="utf-8",
        )
        if output.exists():
            shutil.rmtree(output)
        os.replace(stage, output)
    if archive:
        print("ZIP", pack_source(output, distribution))
    print("HTML5", output / "index.html")
    print("PDF pages", {key: len(value["pages"]) for key, value in manifest["themes"].items()})
    return output / "index.html"


def add_arguments(parser):
    parser.add_argument("deck", nargs="?")
    parser.add_argument("--pdf-dir", type=Path)
    parser.add_argument("--pdf", type=Path, help="Explicit compiled PDF, including an article")
    parser.add_argument("--vectors", type=Path, help="Matching explicit vector timeline")
    parser.add_argument("--page-map", type=Path, help="Explicit physical/logical page metadata")
    parser.add_argument("--pdfjs-dist", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--theme", choices=("both", "light", "dark"), default="both")
    parser.add_argument("--no-zip", dest="archive", action="store_false")


def run(args, catalog: Catalog):
    return build_bundle(
        catalog, catalog.get(args.deck), output=args.output, pdf_dir=args.pdf_dir,
        pdfjs_dist=args.pdfjs_dist, themes=("light", "dark") if args.theme == "both" else (args.theme,),
        pdf=args.pdf, vectors=args.vectors, page_map=args.page_map,
        archive=args.archive, cache_root=args.cache_root,
    )
