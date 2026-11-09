"""Inspect compiled page links and source-mapped glyph bounds."""
from __future__ import annotations

import json
from pathlib import Path

from ..catalog import Catalog, Deck
from ..paths import CachePaths
from ..web.build import pdf_manifest
from .pdf_type3_bounds import validate_type3_bounds
from .pdf_text import validate_vector_text


def verify_documents(deck: Deck, paths: CachePaths, themes, modes, *,
                     pdf: Path | None = None, vectors: Path | None = None,
                     page_map: Path | None = None, assets: Path | None = None) -> dict:
    records = []
    for theme in themes:
        for mode in modes:
            document = pdf or paths.pdf(theme, mode)
            vector = vectors or document.with_suffix(".vector.json")
            if mode != "live" and pdf is None:
                vector = None
            elif not vector.is_file():
                if vectors is not None or pdf is None and deck.animation:
                    raise ValueError(f"Missing matching live timeline: {vector}")
                vector = None
            meta = pdf_manifest(document, vector, theme, page_map)
            records.append({"pdf": str(document), "theme": theme, "mode": mode,
                            "pages": len(meta["pages"]),
                            "animation_links": sum(page["scene"] is not None for page in meta["pages"]),
                            **validate_type3_bounds(document), **validate_vector_text(document)})
    figures = []
    if assets is not None:
        metadata = json.loads((assets / "steps.json").read_text())
        names = {step[key] for scene in metadata["scenes"] for step in scene["steps"]
                 for key in ("figure", "full_figure") if step[key] is not None}
        names.update(scene["poster"] for scene in metadata["scenes"])
        for theme in themes:
            for name in sorted(names):
                path = assets / theme / name
                figures.append({"pdf": str(path), **validate_type3_bounds(path),
                                **validate_vector_text(path, figure=True)})
    return {"ok": True, "deck": deck.id, "documents": records, "figures": figures}
