"""Repository build locations shared by every cataloged presentation."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .catalog import Catalog, Deck, MODES, THEMES


def absolute_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def output_path(value: str | Path, repository: Path) -> Path:
    """Allow repository build products under build/, or at an external location."""
    path = absolute_path(value)
    try:
        relative = path.relative_to(repository.resolve())
    except ValueError:
        return path
    if relative.parts and relative.parts[0] == "build":
        return path
    raise ValueError(f"Build output must be under repository build/ or outside the source repository: {path}")


@dataclass(frozen=True)
class CachePaths:
    cache_root: Path
    repository_root: Path
    source_dir: Path
    source_key: str
    deck_id: str
    document: str
    source_cache_dir: Path
    build_dir: Path
    latex_dir: Path
    assets_dir: Path
    texlive_dir: Path
    tex_packages_dir: Path
    manim_venv_dir: Path
    manim_media_dir: Path
    manim_work_dir: Path
    preview_dir: Path
    review_dir: Path

    @property
    def native_root(self) -> Path:
        return self.cache_root / "native"

    def native_source(self, revision: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("The native viewer revision must be a full lowercase Git commit SHA")
        return self.native_root / f"pdfpc-{revision}"

    def pdf(self, theme: str, mode: str) -> Path:
        if theme not in THEMES or mode not in MODES:
            raise ValueError(f"Unsupported PDF edition: {theme}/{mode}")
        return self.build_dir / f"{self.document}-{theme}-{mode}.pdf"


def get_paths(deck: Deck | None = None, cache_root: str | Path | None = None,
              build_dir: str | Path | None = None, *,
              source_dir: str | Path | None = None) -> CachePaths:
    deck = deck or Catalog().get()
    source = absolute_path(source_dir) if source_dir is not None else deck.source_dir
    override = cache_root or os.environ.get("DERIVATION_CACHE_ROOT")
    root = output_path(override or deck.repository_root / "build", deck.repository_root)
    checkout = deck.repository_root if source == deck.source_dir else source
    key = hashlib.sha256(os.fsencode(checkout)).hexdigest()[:16]
    per_deck = root / "decks" / deck.id
    output = build_dir or os.environ.get("DERIVATION_BUILD_DIR")
    requirements = Path(__file__).parent / "animation" / "requirements.lock"
    dependency_key = hashlib.sha256(requirements.read_bytes()).hexdigest()[:12] if requirements.is_file() else "no-lock"
    python_key = sys.implementation.cache_tag or f"python-{sys.version_info.major}.{sys.version_info.minor}"
    return CachePaths(
        cache_root=root, repository_root=deck.repository_root, source_dir=source,
        source_key=key, deck_id=deck.id, document=deck.document, source_cache_dir=per_deck,
        build_dir=output_path(output, deck.repository_root) if output else per_deck,
        latex_dir=per_deck / "latex", assets_dir=per_deck / "animations",
        texlive_dir=root / "texlive" / "tree", tex_packages_dir=root / "texlive" / "packages",
        manim_venv_dir=root / "manim" / f"venv-{python_key}-{dependency_key}",
        manim_media_dir=per_deck / "manim" / "media",
        manim_work_dir=per_deck / "manim" / "work",
        preview_dir=per_deck / "previews", review_dir=per_deck / "review",
    )


def main(argv=None, *, default_deck: str | None = None) -> int:
    queries = ["json", "pdf", "native-source", "native-root",
               "cache-root", "source-dir", "source-key", "source-cache-dir", "build-dir",
               "latex-dir", "assets-dir", "texlive-dir", "tex-packages-dir", "manim-venv-dir",
               "manim-media-dir", "manim-work-dir", "preview-dir", "review-dir"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path)
    parser.add_argument("query", choices=queries, nargs="?", default="json")
    parser.add_argument("--deck", default=default_deck)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--theme", choices=THEMES, default="light")
    parser.add_argument("--mode", choices=MODES, default="live")
    parser.add_argument("--revision")
    args = parser.parse_args(argv)
    paths = get_paths(Catalog(args.project).get(args.deck), args.cache_root, args.output_dir,
                      source_dir=args.source_dir)
    if args.query == "pdf":
        print(paths.pdf(args.theme, args.mode))
    elif args.query == "native-source":
        if not args.revision:
            parser.error("native-source requires --revision")
        print(paths.native_source(args.revision))
    elif args.query == "json":
        values = {name: str(value) for name, value in asdict(paths).items()}
        values["native_root"] = str(paths.native_root)
        print(json.dumps(values, indent=2))
    else:
        print(getattr(paths, args.query.replace("-", "_")))
    return 0
