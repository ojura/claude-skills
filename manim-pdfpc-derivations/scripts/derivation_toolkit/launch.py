"""Build and launch a cataloged deck with the shared native viewer."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .build import build_deck
from .catalog import Catalog, Deck
from .paths import CachePaths
from .publication import publish_files


def prepare_document(deck: Deck, theme: str, paths: CachePaths, *,
                     pdf: Path | None = None, vectors: Path | None = None) -> Path:
    if pdf is None and deck.build.get("kind", "beamer") == "beamer":
        return build_deck(deck, [theme], ["live"], paths, fetch_tex=True)[0]
    source = (pdf or deck.source).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError(f"Missing compiled PDF: {source}")
    vector = vectors.expanduser().resolve() if vectors else source.with_suffix(".vector.json")
    if vectors and not vector.is_file():
        raise ValueError(f"Missing explicit timeline: {vector}")
    target = paths.pdf(theme, "live")
    paths.source_cache_dir.mkdir(parents=True, exist_ok=True)
    with (paths.source_cache_dir / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".document-input-", dir=target.parent) as temporary:
            staged_pdf = Path(temporary) / "document.pdf"
            staged_map = staged_pdf.with_suffix(".presentation.json")
            staged_vector = staged_pdf.with_suffix(".vector.json") if vector.is_file() else None
            shutil.copyfile(source, staged_pdf)
            source_map = source.with_suffix(".presentation.json")
            if source_map.is_file():
                shutil.copyfile(source_map, staged_map)
            if staged_vector is not None:
                shutil.copyfile(vector, staged_vector)
            from .web.build import pdf_manifest
            manifest = pdf_manifest(staged_pdf, staged_vector, theme)
            if not staged_map.is_file():
                staged_map.write_text(json.dumps({
                    "version": 1, "deck": deck.id, "title": deck.title, "theme": theme,
                    "logicalSlides": manifest["logicalSlides"], "pages": [
                        {key: page[key] for key in ("number", "logical", "title")}
                        for page in manifest["pages"]],
                }, indent=2) + "\n", encoding="utf-8")
            replacements = {target: staged_pdf, target.with_suffix(".presentation.json"): staged_map}
            sidecar = target.with_suffix(".vector.json")
            if staged_vector is not None:
                replacements[sidecar] = staged_vector
            publish_files(replacements, removals=[sidecar] if staged_vector is None else [])
    return target


def run(deck: Deck, theme: str, paths: CachePaths, pdfpc_args: list[str], *,
        pdf: Path | None = None, vectors: Path | None = None) -> None:
    pdf = prepare_document(deck, theme, paths, pdf=pdf, vectors=vectors)
    env = os.environ.copy()
    env["DERIVATION_CACHE_ROOT"] = str(paths.cache_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    from .native.build import build_viewer
    executable = build_viewer(Catalog(deck.repository_root), paths)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"The native viewer build did not return an executable: {executable}")
    revision = subprocess.run(["git", "-C", str(deck.repository_root), "rev-parse", "--short=8", "HEAD"],
                              capture_output=True, text=True, check=False)
    label = revision.stdout.strip() if revision.returncode == 0 else "source"
    dirty = subprocess.run(["git", "-C", str(deck.repository_root), "status", "--porcelain",
                            "--untracked-files=normal"], capture_output=True, text=True, check=False)
    if dirty.stdout:
        label += "+dirty"
    timestamp = datetime.fromtimestamp(pdf.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    env["PDFPC_TITLE_SUFFIX"] = f"{Catalog(deck.repository_root).title} {label} | built {timestamp}"
    os.execve(executable, [str(executable), "--no-install", *pdfpc_args, str(pdf)], env)
