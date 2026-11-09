"""Build and launch a cataloged deck with the shared native viewer."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess

from .build import build_deck
from .catalog import Catalog, Deck
from .paths import CachePaths


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
    from .web.build import pdf_manifest
    pdf_manifest(source, vector if vector.is_file() else None, theme)
    target = paths.pdf(theme, "live")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source != target:
        shutil.copyfile(source, target)
    if vector.is_file() and vector != target.with_suffix(".vector.json"):
        shutil.copyfile(vector, target.with_suffix(".vector.json"))
    elif not vector.is_file():
        target.with_suffix(".vector.json").unlink(missing_ok=True)
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
